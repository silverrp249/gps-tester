"""Audit mode: a black-box security-posture check of a tracking server.

The tool is only a client - it never reads the server's files, database or
logs. Every conclusion here is *inferred from how the server responds* to
traffic we send, so findings are reported as present / absent / inconclusive
with the evidence that led to them, never as certainty about the server's
internals.

Checks, in order:

1. reachability    - can we open a session and is login acknowledged?
2. imei_validation - does the server treat an unregistered IMEI differently
                     from a known-registered one (``--registered-imei``)?
3. rate_limiting   - does the server start refusing connections as the
                     connection rate climbs?
4. robustness      - the standard malformed-input suite, summarised.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from gps_tester.core.runner import RunConfig, run_validation
from gps_tester.core.stats import Stats
from gps_tester.core.transport import Connection, ConnectionClosed
from gps_tester.generators.route import RouteSimulator, make_imei
from gps_tester.protocols.base import Device, Protocol

PASS, WARN, FAIL = "pass", "warn", "fail"
Progress = Callable[[Dict], None]


@dataclass
class SessionOutcome:
    """What a server did with one device's session, observed from outside."""
    connected: bool = False
    login_ack: Optional[bool] = None   # None = protocol has no login ack to observe
    stayed_open: Optional[bool] = None
    error: str = ""

    @property
    def rejected(self) -> bool:
        if not self.connected:
            return True
        return self.login_ack is False or self.stayed_open is False

    @property
    def accepted(self) -> bool:
        return self.connected and not self.rejected

    def describe(self) -> str:
        if not self.connected:
            return f"connection refused ({self.error or 'no reason given'})"
        parts = []
        if self.login_ack is True:
            parts.append("login acknowledged")
        elif self.login_ack is False:
            parts.append("login not acknowledged")
        parts.append("session kept open" if self.stayed_open else "session dropped")
        if self.error:
            parts.append(self.error)
        return ", ".join(parts)


async def _session_outcome(cfg: RunConfig, protocol: Protocol, imei: str,
                           hold: float = 2.5) -> SessionOutcome:
    """Open a session for ``imei``, hold it briefly, and report what happened."""
    out = SessionOutcome()
    conn = Connection(cfg.host, cfg.port, cfg.transport, cfg.timeout)
    device = Device(imei)
    route = RouteSimulator(cfg.lat, cfg.lon, random.Random(0))
    try:
        await conn.open()
        out.connected = True
        login = protocol.login(device)
        if login is not None:
            await conn.send(login.data)
            if login.expect_response:
                reply = await conn.recv(cfg.timeout)
                out.login_ack = bool(reply is not None and protocol.is_ack(login, reply))
        end = time.monotonic() + hold
        while time.monotonic() < end:
            await conn.send(protocol.location(device, route.step(1.0)).data)
            await asyncio.sleep(0.5)
        await conn.recv(0.3)          # raises ConnectionClosed if the peer closed
        out.stayed_open = True
    except ConnectionClosed as exc:
        out.stayed_open = False
        out.error = str(exc) or "closed by server"
    except OSError as exc:
        if out.connected:
            out.stayed_open = False
        out.error = str(exc) or exc.__class__.__name__
    finally:
        await conn.close()
    return out


async def _check_reachability(cfg: RunConfig, protocol: Protocol, stats: Stats,
                              report: Progress) -> bool:
    imei = make_imei(cfg.imei_base, 0)
    outcome = await _session_outcome(cfg, protocol, imei, hold=1.0)
    if not outcome.connected:
        report(stats.add_finding("reachability", "Server reachable", FAIL, "unreachable",
               f"Could not connect to {cfg.transport}://{cfg.host}:{cfg.port} - {outcome.error}. "
               "The rest of the audit was skipped."))
        return False
    report(stats.add_finding("reachability", "Server reachable", PASS, "ok",
           f"Connected and completed a session. {outcome.describe()}."))
    return True


async def _check_imei_validation(cfg: RunConfig, protocol: Protocol, stats: Stats,
                                 registered_imei: Optional[str], report: Progress) -> None:
    random_imei = make_imei(str(random.randint(10 ** 13, 10 ** 14 - 1)), random.randint(1, 9999))
    unknown = await _session_outcome(cfg, protocol, random_imei)
    evidence = [f"unregistered IMEI {random_imei}: {unknown.describe()}"]

    if registered_imei:
        known = await _session_outcome(cfg, protocol, registered_imei)
        evidence.insert(0, f"registered IMEI {registered_imei}: {known.describe()}")
        if known.rejected:
            report(stats.add_finding("imei_validation", "Device authentication (IMEI)", WARN,
                   "inconclusive",
                   "The IMEI you gave as registered was also rejected, so the comparison is not "
                   "meaningful. Check that it is really enrolled and active on the server.", evidence))
        elif unknown.accepted:
            report(stats.add_finding("imei_validation", "Device authentication (IMEI)", FAIL, "absent",
                   "The server accepted an unregistered IMEI exactly as it accepted your registered "
                   "device. It does not appear to authenticate devices by IMEI - any device can push "
                   "data. Reject unknown IMEIs at the point of ingestion.", evidence))
        else:
            report(stats.add_finding("imei_validation", "Device authentication (IMEI)", PASS, "present",
                   "The registered device was accepted while the unregistered IMEI was rejected - the "
                   "server distinguishes known devices from unknown ones.", evidence))
        return

    if unknown.accepted:
        report(stats.add_finding("imei_validation", "Device authentication (IMEI)", WARN, "likely-absent",
               "The server accepted a random, almost-certainly-unregistered IMEI and kept the session "
               "open. This usually means it does not authenticate devices. For a definitive result, "
               "re-run with --registered-imei <a real device> so the two can be compared.", evidence))
    else:
        report(stats.add_finding("imei_validation", "Device authentication (IMEI)", WARN, "inconclusive",
               "The server rejected a random IMEI, which may mean it authenticates devices - but "
               "without a known-registered IMEI to compare against, this cannot be confirmed. Re-run "
               "with --registered-imei <a real device>.", evidence))


async def _attempt_connect(cfg: RunConfig) -> bool:
    conn = Connection(cfg.host, cfg.port, cfg.transport, connect_timeout=cfg.timeout)
    try:
        await conn.open()
        return True
    except OSError:
        return False
    finally:
        await conn.close()


async def _check_rate_limiting(cfg: RunConfig, stats: Stats, report: Progress,
                               levels=(10, 25, 50, 100, 200)) -> None:
    if cfg.transport != "tcp":
        report(stats.add_finding("rate_limiting", "Connection rate limiting", WARN, "not-tested",
               "Rate limiting is only probed over TCP; this run used UDP."))
        return
    evidence, throttled_at = [], None
    baseline_ok = False
    for level in levels:
        results = await asyncio.gather(*(_attempt_connect(cfg) for _ in range(level)))
        ok = sum(results)
        ratio = ok / level
        evidence.append(f"{level} connections at once: {ok} accepted ({ratio:.0%})")
        if level == levels[0] and ratio >= 0.9:
            baseline_ok = True
        if baseline_ok and ratio < 0.7 and throttled_at is None:
            throttled_at = level
        await asyncio.sleep(0.5)

    if throttled_at is not None:
        report(stats.add_finding("rate_limiting", "Connection rate limiting", PASS, "present",
               f"The server began refusing connections around {throttled_at} simultaneous attempts. "
               "Something is capping the connection rate (a rate limiter, a connection cap, or the OS "
               "backlog). Confirm it is a deliberate limit, not the server hitting its ceiling.", evidence))
    elif not baseline_ok:
        report(stats.add_finding("rate_limiting", "Connection rate limiting", WARN, "inconclusive",
               "Even the smallest burst saw many refusals, so no clean baseline could be established "
               "to judge rate limiting from.", evidence))
    else:
        report(stats.add_finding("rate_limiting", "Connection rate limiting", WARN, "absent",
               f"The server accepted every burst up to {levels[-1]} simultaneous connections without "
               "refusing any. No connection-rate limiting was observed - a single source can open "
               "connections freely. Consider a firewall rule, fail2ban, or an application-level cap.",
               evidence))


async def _check_robustness(cfg: RunConfig, protocol: Protocol, stats: Stats,
                            stop: asyncio.Event, report: Progress,
                            registered_imei: Optional[str] = None) -> None:
    # If the server authenticates by IMEI, use the enrolled one so our own test
    # sessions are accepted - otherwise every case would look unresponsive.
    sub = RunConfig(**{**cfg.__dict__, "count": cfg.count or 40, "fixed_imei": registered_imei})
    await run_validation(sub, protocol, stats, stop)
    counts = stats.outcome_counts()
    evidence = [f"{name}: {counts[name]}" for name in counts if counts[name]]
    if counts.get("unresponsive"):
        report(stats.add_finding("robustness", "Malformed-input resilience", FAIL, "issue",
               f"{counts['unresponsive']} malformed case(s) left the server unresponsive to valid "
               "traffic afterwards - a likely crash or hang. See the case table for the exact frames.",
               evidence))
    elif counts.get("accepted"):
        report(stats.add_finding("robustness", "Malformed-input resilience", WARN, "weak-validation",
               f"The server stayed up, but acknowledged {counts['accepted']} frame(s) it should have "
               "rejected (e.g. a bad checksum). Input validation is incomplete.", evidence))
    else:
        report(stats.add_finding("robustness", "Malformed-input resilience", PASS, "ok",
               f"All {len([c for c in stats.cases if not c.outcome == 'skipped'])} malformed cases were "
               "handled without downtime or disconnects.", evidence))


async def run_audit(cfg: RunConfig, protocol: Protocol, stats: Stats, stop: asyncio.Event,
                    registered_imei: Optional[str] = None, report: Optional[Progress] = None) -> None:
    report = report or (lambda finding: None)
    try:
        if not await _check_reachability(cfg, protocol, stats, report):
            return
        for step in (
            _check_imei_validation(cfg, protocol, stats, registered_imei, report),
            _check_rate_limiting(cfg, stats, report),
            _check_robustness(cfg, protocol, stats, stop, report, registered_imei),
        ):
            if stop.is_set():
                break
            await step
    finally:
        stats.finish()
