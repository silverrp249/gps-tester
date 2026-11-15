"""Base classes every protocol plugin builds on.

To add a protocol: subclass :class:`Protocol`, decorate it with
``@register`` and import the module from ``gps_tester/protocols/__init__.py``.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple, Type


@dataclass
class Position:
    """One GPS fix. Speed is km/h, course is degrees, altitude/odometer are metres."""
    lat: float
    lon: float
    speed: float = 0.0
    course: float = 0.0
    altitude: float = 0.0
    satellites: int = 10
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ignition: bool = True
    odometer: float = 0.0


@dataclass
class Device:
    """A simulated tracker."""
    imei: str
    index: int = 0
    seq: int = 0

    def next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0xFFFF
        return self.seq


@dataclass
class Frame:
    """Bytes ready to go on the wire."""
    data: bytes
    kind: str = "location"          # login | location | heartbeat
    expect_response: bool = False   # does the protocol define a server reply?


@dataclass
class FuzzCase:
    """One robustness test: a payload sent after a normal login."""
    name: str
    description: str
    payload: bytes
    category: str = "structure"     # range | structure | checksum | connection | random
    disconnect: bool = False        # abort the connection right after sending
    must_reject: bool = False       # an ACK for this frame means missing validation
    skip: bool = False              # not sent: not representable in this protocol
    skip_reason: str = ""           # why it was skipped (shown in the report)


class Protocol(abc.ABC):
    name: str = ""
    description: str = ""
    default_transport: str = "tcp"

    #: Fields the location frame actually carries. A range test for a field
    #: that is not here would produce a frame identical to a valid one, so it
    #: is skipped. Text protocols carry everything; binary ones override this.
    LOCATION_FIELDS: Set[str] = {
        "lat", "lon", "speed", "course", "altitude", "satellites", "timestamp", "odometer",
    }
    #: field -> (min, max) range of values transmitted without wrap/clamp.
    #: A test value outside this range would be silently reduced to an in-range
    #: value by the field width, so it does not actually test anything and is
    #: skipped. Absent field = carried with full fidelity (e.g. text formats).
    FIELD_LIMITS: Dict[str, Tuple[float, float]] = {}

    def login(self, device: Device) -> Optional[Frame]:
        """Frame sent once after connecting, or ``None`` if the protocol has no login."""
        return None

    @abc.abstractmethod
    def location(self, device: Device, position: Position) -> Frame:
        """Encode one position report."""

    def heartbeat(self, device: Device) -> Optional[Frame]:
        return None

    def is_ack(self, frame: Frame, response: bytes) -> bool:
        """Does ``response`` acknowledge ``frame``? Default: any reply counts."""
        return bool(response)

    def server_ack(self, data: bytes) -> Optional[bytes]:
        """Reply a real server would send for ``data`` (used by the mock server)."""
        return None

    def extra_fuzz_cases(self, device: Device, position: Position) -> List[FuzzCase]:
        """Protocol-specific malformed frames (bad CRC, wrong length field, ...)."""
        return []


_REGISTRY: Dict[str, Type[Protocol]] = {}


def register(cls: Type[Protocol]) -> Type[Protocol]:
    _REGISTRY[cls.name] = cls
    return cls


def available_protocols() -> Dict[str, Type[Protocol]]:
    return dict(_REGISTRY)


def get_protocol(name: str) -> Protocol:
    try:
        return _REGISTRY[name.lower()]()
    except KeyError:
        raise ValueError(
            f"Unknown protocol {name!r}. Available: {', '.join(sorted(_REGISTRY))}") from None
