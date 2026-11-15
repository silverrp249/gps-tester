"""The two execution modes: load simulation and robustness validation."""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from gps_tester.core.stats import CaseResult, Stats
from gps_tester.core.transport import Connection
from gps_tester.generators.fuzz import build_cases
from gps_tester.generators.route import RouteSimulator, make_imei
from gps_tester.protocols.base import Device, Frame, Position, Protocol

NET_ERRORS = (OSError, asyncio.TimeoutError)


@dataclass
class RunConfig:
    host: str
    port: int
    transport: str = "tcp"
    devices: int = 10
    interval: float = 5.0
    duration: float = 0.0          # seconds, 0 = until Ctrl+C
    count: Optional[int] = None    # validate: number of cases
    timeout: float = 5.0           # seconds to wait for a server reply
    ramp_up: float = 5.0           # seconds over which devices connect
    heartbeat_every: int = 10      # send a heartbeat every N location frames
    case_delay: float = 0.05
    imei_base: str = "86123400000000"
    fixed_imei: Optional[str] = None   # use this exact IMEI instead of generating one
    lat: float = 24.7136           # Riyadh
    lon: float = 46.6753
    seed: Optional[int] = None


async def _exchange(conn: Connection, protocol: Protocol, frame: Frame, stats: Stats,
                    timeout: float, who: str) -> None:
    """Send one frame and, if the protocol defines a reply, wait for and time it."""
    started = time.perf_counter()
    await conn.send(frame.data)
    stats.record_sent(len(frame.data), frame.expect_response)
    if not frame.expect_response:
        return
    response = await conn.recv(timeout)
    if response is None:
        stats.record_timeout(f"{who} {frame.kind}:")
    elif protocol.is_ack(frame, response):
        stats.record_ack(time.perf_counter() - started)
    else:
        stats.record_bad_response(f"{who} {frame.kind}:")


# ======================================================================
# Simulation / load mode
# ======================================================================
async def _device_loop(cfg: RunConfig, protocol: Protocol, stats: Stats, index: int) -> None:
    rng = random.Random(None if cfg.seed is None else cfg.seed + index)
    device = Device(make_imei(cfg.imei_base, index), index)
    route = RouteSimulator(cfg.lat, cfg.lon, rng)
    who = f"device {device.imei}"
    await asyncio.sleep(cfg.ramp_up * index / max(1, cfg.devices))
    backoff = 1.0
    while True:
        conn = Connection(cfg.host, cfg.port, cfg.transport)
        connected = False
        try:
            await conn.open()
            connected = True
            stats.record_open()
            login = protocol.login(device)
            if login is not None:
                await _exchange(conn, protocol, login, stats, cfg.timeout, who)
            backoff, sent, last = 1.0, 0, time.monotonic()
            while True:
                now = time.monotonic()
                position = route.step(max(now - last, cfg.interval if sent == 0 else 0.0))
                last = now
                await _exchange(conn, protocol, protocol.location(device, position), stats, cfg.timeout, who)
                sent += 1
                if cfg.heartbeat_every and sent % cfg.heartbeat_every == 0:
                    heartbeat = protocol.heartbeat(device)
                    if heartbeat is not None:
                        await _exchange(conn, protocol, heartbeat, stats, cfg.timeout, who)
                await asyncio.sleep(cfg.interval * rng.uniform(0.9, 1.1))
        except NET_ERRORS as exc:
            message = str(exc) or exc.__class__.__name__
            if connected:
                stats.record_disconnect(who, message)
            else:
                stats.record_connect_error(who, message)
        finally:
            if connected:
                stats.record_close()
            await conn.close()
        await asyncio.sleep(backoff)          # reconnect like a real tracker would
        backoff = min(backoff * 2, 30.0)


async def run_simulation(cfg: RunConfig, protocol: Protocol, stats: Stats, stop: asyncio.Event) -> None:
    tasks = [asyncio.create_task(_device_loop(cfg, protocol, stats, i)) for i in range(cfg.devices)]
    try:
        await asyncio.wait_for(stop.wait(), cfg.duration if cfg.duration > 0 else None)
    except asyncio.TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stats.finish()


# ======================================================================
# Robustness / input-validation mode
# ======================================================================
async def _probe(cfg: RunConfig, protocol: Protocol, device: Device) -> Tuple[bool, str]:
    """Is the server still serving a brand-new, perfectly valid session?"""
    error = ""
    for attempt in range(2):
        conn = Connection(cfg.host, cfg.port, cfg.transport)
        try:
            await conn.open()
            frame = protocol.login(device)
            if frame is None or not frame.expect_response:
                heartbeat = protocol.heartbeat(device)
                if frame is not None:
                    await conn.send(frame.data)
                frame = heartbeat if heartbeat is not None and heartbeat.expect_response else None
            if frame is None:
                return True, "connect ok"       # protocol defines no reply we could wait for
            await conn.send(frame.data)
            if await conn.recv(cfg.timeout) is not None:
                return True, "valid session acknowledged"
            error = "no reply to a valid frame"
        except NET_ERRORS as exc:
            error = str(exc) or exc.__class__.__name__
        finally:
            await conn.close()
        if attempt == 0:
            await asyncio.sleep(1.0)
    return False, error


async def _run_case(cfg: RunConfig, protocol: Protocol, stats: Stats, device: Device,
                    result: CaseResult, case) -> None:
    who = f"case {result.name}:"
    conn = Connection(cfg.host, cfg.port, cfg.transport)
    dropped, response = "", None
    try:
        try:
            await conn.open()
            stats.record_open()
            login = protocol.login(device)
            if login is not None:
                await conn.send(login.data)
                stats.record_sent(len(login.data))
                if login.expect_response:
                    await conn.recv(cfg.timeout)
        except NET_ERRORS as exc:
            result.outcome = "unresponsive"
            result.detail = f"could not open a normal session before the test: {exc or exc.__class__.__name__}"
            stats.record_connect_error(who, result.detail)
            return

        started = time.perf_counter()
        try:
            if case.payload:
                await conn.send(case.payload)
                stats.record_sent(len(case.payload), not case.disconnect)
            if case.disconnect:
                conn.abort()
            else:
                response = await conn.recv(cfg.timeout)
                if response is None:
                    stats.timeouts += 1
                else:
                    result.latency_ms = round((time.perf_counter() - started) * 1000, 3)
                    stats.record_ack(time.perf_counter() - started)
        except NET_ERRORS as exc:
            dropped = str(exc) or exc.__class__.__name__
    finally:
        if conn.is_open:
            stats.record_close()
        await conn.close()

    alive, probe_detail = await _probe(cfg, protocol, device)
    if not alive:
        result.outcome = "unresponsive"
        result.detail = f"server stopped serving valid sessions afterwards ({probe_detail})"
        stats.event("unresponsive", f"{who} {result.detail}")
    elif dropped:
        result.outcome = "disconnected"
        result.detail = f"server dropped the connection ({dropped}); new sessions still work"
        stats.record_disconnect(who, dropped)
    elif response is not None and case.must_reject and _acked(protocol, case.payload, response):
        result.outcome = "accepted"
        result.detail = "server acknowledged a frame it should have rejected"
        stats.event("accepted", f"{who} invalid frame was acknowledged")
    elif case.disconnect:
        result.detail = "server survived the abrupt client disconnect"
    elif response is not None:
        result.detail = f"server replied ({len(response)} bytes) and kept working"
    else:
        result.detail = "server ignored the frame and kept working"


def _acked(protocol: Protocol, payload: bytes, response: bytes) -> bool:
    try:
        return protocol.is_ack(Frame(payload, "location", True), response)
    except Exception:  # noqa: BLE001 - payload is deliberately malformed
        return False


async def run_validation(cfg: RunConfig, protocol: Protocol, stats: Stats, stop: asyncio.Event) -> None:
    rng = random.Random(cfg.seed if cfg.seed is not None else 1337)
    device = Device(cfg.fixed_imei or make_imei(cfg.imei_base, 0), 0)
    position = RouteSimulator(cfg.lat, cfg.lon, rng).step(1.0)
    cases = build_cases(protocol, device, position, rng, cfg.count)
    stats.total_cases = len(cases)
    try:
        for index, case in enumerate(cases, 1):
            if stop.is_set():
                break
            result = CaseResult(index, case.name, case.category, case.description, len(case.payload),
                                case.payload[:96].hex() + ("..." if len(case.payload) > 96 else ""))
            if case.skip:
                result.outcome = "skipped"
                result.detail = case.skip_reason
                stats.cases.append(result)
                continue
            await _run_case(cfg, protocol, stats, device, result, case)
            stats.cases.append(result)
            await asyncio.sleep(cfg.case_delay)
    finally:
        stats.finish()
