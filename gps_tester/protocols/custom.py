"""User-defined protocol loaded from a JSON/YAML template.

See ``examples/custom_device.yaml`` for a documented template. Templates use
Python ``str.format`` placeholders; the available fields are listed in
:data:`FIELD_HELP`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from gps_tester.core.checksums import ALGORITHMS
from gps_tester.protocols.base import Device, Frame, Position, Protocol
from gps_tester.protocols.tk103 import to_nmea

FIELD_HELP = {
    "imei": "device IMEI (15 digits, string)",
    "imei_bcd": "IMEI as 16 hex digits (BCD, left padded with 0)",
    "index": "device index (int)", "seq": "message sequence number (int, 16 bit)",
    "lat": "latitude, decimal degrees (float)", "lon": "longitude, decimal degrees (float)",
    "lat_abs": "absolute latitude (float)", "lon_abs": "absolute longitude (float)",
    "ns": "N or S", "ew": "E or W",
    "lat_nmea": "latitude as ddmm.mmmm", "lon_nmea": "longitude as dddmm.mmmm",
    "lat_e7": "latitude * 1e7 as unsigned 32-bit int (two's complement)",
    "lon_e7": "longitude * 1e7 as unsigned 32-bit int (two's complement)",
    "speed": "speed km/h (float)", "speed_int": "speed km/h (int)", "speed_knots": "speed in knots (float)",
    "course": "heading degrees (float)", "course_int": "heading degrees (int)",
    "altitude": "metres (float)", "altitude_int": "metres (int)",
    "satellites": "satellites in view (int)", "ignition": "1 or 0", "odometer": "metres (int)",
    "timestamp": "unix seconds (int)", "timestamp_ms": "unix milliseconds (int)",
    "date": "YYMMDD", "time": "HHMMSS", "datetime": "ISO-8601 UTC",
    "year": "int", "month": "int", "day": "int", "hour": "int", "minute": "int", "second": "int",
    "checksum": "checksum of everything rendered before this placeholder",
}


def build_fields(device: Device, p: Position) -> Dict[str, Any]:
    t = p.timestamp
    return {
        "imei": device.imei, "imei_bcd": device.imei.rjust(16, "0")[-16:],
        "index": device.index, "seq": device.next_seq(),
        "lat": p.lat, "lon": p.lon, "lat_abs": abs(p.lat), "lon_abs": abs(p.lon),
        "ns": "N" if p.lat >= 0 else "S", "ew": "E" if p.lon >= 0 else "W",
        "lat_nmea": to_nmea(p.lat, 2), "lon_nmea": to_nmea(p.lon, 3),
        "lat_e7": int(round(p.lat * 1e7)) & 0xFFFFFFFF, "lon_e7": int(round(p.lon * 1e7)) & 0xFFFFFFFF,
        "speed": p.speed, "speed_int": int(p.speed), "speed_knots": p.speed / 1.852,
        "course": p.course, "course_int": int(p.course),
        "altitude": p.altitude, "altitude_int": int(p.altitude),
        "satellites": int(p.satellites), "ignition": int(p.ignition), "odometer": int(p.odometer),
        "timestamp": int(t.timestamp()), "timestamp_ms": int(t.timestamp() * 1000),
        "date": f"{t:%y%m%d}", "time": f"{t:%H%M%S}", "datetime": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "year": t.year, "month": t.month, "day": t.day,
        "hour": t.hour, "minute": t.minute, "second": t.second,
    }


class CustomProtocol(Protocol):
    name = "custom"
    description = "Template-driven protocol loaded from a JSON/YAML file"

    def __init__(self, config: Dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ValueError("Custom protocol config must be a mapping")
        self.config = config
        self.label = str(config.get("name", "custom"))
        self.default_transport = str(config.get("transport", "tcp")).lower()
        if self.default_transport not in ("tcp", "udp"):
            raise ValueError("transport must be 'tcp' or 'udp'")
        self.encoding = str(config.get("encoding", "text")).lower()
        if self.encoding not in ("text", "hex"):
            raise ValueError("encoding must be 'text' or 'hex'")
        self.host: Optional[str] = config.get("host")
        self.port: Optional[int] = int(config["port"]) if config.get("port") else None

        checksum = config.get("checksum") or {}
        algorithm = str(checksum.get("algorithm", "none")).lower()
        if algorithm != "none" and algorithm not in ALGORITHMS:
            raise ValueError(f"Unknown checksum algorithm {algorithm!r}. "
                             f"Use one of: none, {', '.join(sorted(ALGORITHMS))}")
        self._checksum = ALGORITHMS.get(algorithm)
        wide = algorithm.startswith("crc16")
        default_format = ("{:04x}" if wide else "{:02x}") if self.encoding == "hex" else \
                         ("{:04X}" if wide else "{:02X}")
        self._checksum_format = str(checksum.get("format", default_format))
        self._checksum_skip = int(checksum.get("skip", 0))

        self.messages: Dict[str, Dict[str, Any]] = config.get("messages") or {}
        if "location" not in self.messages or "template" not in self.messages["location"]:
            raise ValueError("Custom protocol needs messages.location.template")
        # Fail early on broken templates instead of in the middle of a test run.
        probe = Device("860000000000000")
        for kind in self.messages:
            self._frame(kind, probe, Position(24.7136, 46.6753))

    # ------------------------------------------------------------- loading
    @classmethod
    def from_file(cls, path: str) -> "CustomProtocol":
        text = Path(path).read_text(encoding="utf-8")
        if path.lower().endswith(".json"):
            return cls(json.loads(text))
        import yaml
        return cls(yaml.safe_load(text))

    # ------------------------------------------------------------ encoding
    def _encode(self, text: str) -> bytes:
        if self.encoding == "hex":
            try:
                return bytes.fromhex(re.sub(r"\s+", "", text))
            except ValueError as exc:
                raise ValueError(f"Template did not render to valid hex: {text!r}") from exc
        return text.encode("utf-8")

    def _render(self, template: str, fields: Dict[str, Any]) -> bytes:
        if "{checksum}" in template:
            if self._checksum is None:
                raise ValueError("Template uses {checksum} but checksum.algorithm is 'none'")
            head, tail = template.split("{checksum}", 1)
            head = head.format(**fields)
            value = self._checksum(self._encode(head)[self._checksum_skip:])
            return self._encode(head + self._checksum_format.format(value) + tail.format(**fields))
        return self._encode(template.format(**fields))

    def _frame(self, kind: str, device: Device, position: Optional[Position] = None) -> Optional[Frame]:
        message = self.messages.get(kind)
        if not message:
            return None
        position = position or Position(0.0, 0.0, timestamp=datetime.now(timezone.utc))
        try:
            data = self._render(str(message["template"]), build_fields(device, position))
        except (KeyError, IndexError) as exc:
            raise ValueError(f"messages.{kind}.template uses an unknown field: {exc}") from exc
        return Frame(data, kind, bool(message.get("expect_response", False)))

    # ------------------------------------------------------------ protocol
    def login(self, device: Device) -> Optional[Frame]:
        return self._frame("login", device)

    def location(self, device: Device, position: Position) -> Frame:
        return self._frame("location", device, position)

    def heartbeat(self, device: Device) -> Optional[Frame]:
        return self._frame("heartbeat", device)

    def _expected(self, kind: str) -> Optional[bytes]:
        expect = (self.messages.get(kind) or {}).get("expect")
        return self._encode(str(expect)) if expect else None

    def is_ack(self, frame: Frame, response: bytes) -> bool:
        expected = self._expected(frame.kind)
        return expected in response if expected else bool(response)

    def server_ack(self, data: bytes) -> Optional[bytes]:
        """Mock-server helper: reply with ``mock_reply`` of the first message
        whose ``mock_match`` prefix matches the incoming data."""
        for message in self.messages.values():
            match, reply = message.get("mock_match"), message.get("mock_reply")
            if match and reply and data.startswith(self._encode(str(match))):
                return self._encode(str(reply))
        return None
