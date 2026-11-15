"""Edge-case and malformed-input generator for robustness testing.

Three layers of cases are produced for every protocol:

1. *range*      - a well-formed frame carrying impossible values
2. *structure*  - a valid frame damaged at byte level (truncated, oversized...)
3. protocol-specific cases from ``Protocol.extra_fuzz_cases``

Range cases a protocol cannot faithfully carry (a field it does not transmit,
or a value its field width would clamp back into the valid range) are marked
``skip`` instead of being sent, so the report shows *why* they were excluded
rather than silently dropping them or sending a harmless valid-looking frame.

When more cases are requested than the catalogue holds, seeded random
mutations of a valid frame fill the rest, so runs are reproducible.
"""
from __future__ import annotations

import random
import struct
from dataclasses import replace
from datetime import datetime, timezone
from typing import List, Optional

from gps_tester.protocols.base import Device, FuzzCase, Position, Protocol

UTC = timezone.utc

RANGE_CASES = [
    ("lat_above_max", "Latitude 91.5 (valid range is -90..90)", {"lat": 91.5}),
    ("lat_below_min", "Latitude -123.4", {"lat": -123.4}),
    ("lon_above_max", "Longitude 181.0 (valid range is -180..180)", {"lon": 181.0}),
    ("lon_below_min", "Longitude -540.0", {"lon": -540.0}),
    ("coords_huge", "Coordinates 99999.0 / 99999.0", {"lat": 99999.0, "lon": 99999.0}),
    ("null_island", "Coordinates exactly 0, 0", {"lat": 0.0, "lon": 0.0}),
    ("coords_nan", "NaN coordinates", {"lat": float("nan"), "lon": float("nan")}),
    ("speed_negative", "Negative speed -50 km/h", {"speed": -50.0}),
    ("speed_huge", "Speed 99999 km/h", {"speed": 99999.0}),
    ("speed_int32_overflow", "Speed 2^31 km/h", {"speed": float(2 ** 31)}),
    ("speed_infinite", "Infinite speed", {"speed": float("inf")}),
    ("course_above_360", "Heading 720 degrees", {"course": 720.0}),
    ("course_negative", "Heading -90 degrees", {"course": -90.0}),
    ("satellites_zero", "Zero satellites with a 'valid' fix", {"satellites": 0}),
    ("satellites_255", "255 satellites", {"satellites": 255}),
    ("altitude_negative", "Altitude -10000 m", {"altitude": -10000.0}),
    ("altitude_huge", "Altitude 100000 m", {"altitude": 100000.0}),
    ("timestamp_epoch", "Timestamp 1970-01-01", {"timestamp": datetime(1970, 1, 1, tzinfo=UTC)}),
    ("timestamp_2038", "Timestamp just past the 32-bit rollover (2038-01-19)",
     {"timestamp": datetime(2038, 1, 19, 3, 14, 8, tzinfo=UTC)}),
    ("timestamp_far_future", "Timestamp 2099-12-31", {"timestamp": datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC)}),
    ("odometer_overflow", "Odometer above 32 bits", {"odometer": float(2 ** 40)}),
]

ENCODE_ERRORS = (ValueError, OverflowError, TypeError, struct.error)


def _representable(protocol: Protocol, changes: dict) -> Optional[str]:
    """Return None if the protocol can faithfully carry ``changes``, else why not.

    A range test only means something if the protocol actually transmits the
    field and does not clamp the value back into the valid range. Otherwise the
    server would receive an ordinary-looking frame and the test proves nothing.
    """
    for field, value in changes.items():
        if field not in protocol.LOCATION_FIELDS:
            return f"a {protocol.name} location frame has no {field} field"
        limit = protocol.FIELD_LIMITS.get(field)
        if limit is not None and isinstance(value, (int, float)):
            lo, hi = limit
            if not (lo <= value <= hi):   # NaN/inf also land here
                return (f"{field}={value:g} would be clamped to the {protocol.name} "
                        f"field width ({lo:g}..{hi:g}), so the server would see a valid value")
    return None


def _flip(data: bytes, index: int, mask: int = 0xFF) -> bytes:
    index %= len(data)
    return data[:index] + bytes([data[index] ^ mask]) + data[index + 1:]


def _structural(valid: bytes, rng: random.Random) -> List[FuzzCase]:
    half = max(1, len(valid) // 2)
    return [
        FuzzCase("truncated_half", "Only the first half of a valid frame (short length)", valid[:half]),
        FuzzCase("truncated_last_byte", "Valid frame missing its last byte", valid[:-1]),
        FuzzCase("header_only", "Only the first 3 bytes of a frame", valid[:3]),
        FuzzCase("single_null_byte", "A single 0x00 byte", b"\x00"),
        FuzzCase("all_zeros", "Frame-sized block of 0x00", b"\x00" * len(valid)),
        FuzzCase("all_ff", "Frame-sized block of 0xFF", b"\xFF" * len(valid)),
        FuzzCase("corrupt_first_byte", "First byte inverted", _flip(valid, 0)),
        FuzzCase("corrupt_middle_byte", "One payload byte inverted", _flip(valid, half)),
        FuzzCase("random_garbage", "256 random bytes", rng.randbytes(256)),
        FuzzCase("oversized_frame", "Valid frame followed by 64 KiB of random data",
                 valid + rng.randbytes(64 * 1024)),
        FuzzCase("duplicate_frames", "Same frame three times in one TCP segment", valid * 3),
        FuzzCase("frame_plus_fragment", "Valid frame followed by half a frame", valid + valid[:half]),
        FuzzCase("leading_garbage", "16 random bytes before a valid frame", rng.randbytes(16) + valid),
        FuzzCase("partial_send_disconnect", "Half a frame, then the connection is reset",
                 valid[:half], "connection", disconnect=True),
        FuzzCase("send_then_disconnect", "Valid frame, then the connection is reset immediately",
                 valid, "connection", disconnect=True),
        FuzzCase("login_then_disconnect", "Connection reset right after login, nothing sent",
                 b"", "connection", disconnect=True),
    ]


def _mutate(valid: bytes, rng: random.Random) -> bytes:
    data = bytearray(valid)
    op = rng.choice(("bitflip", "truncate", "insert", "delete", "overwrite"))
    if op == "bitflip":
        for _ in range(rng.randint(1, 8)):
            data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
    elif op == "truncate":
        del data[rng.randrange(1, len(data)):]
    elif op == "insert":
        at = rng.randrange(len(data) + 1)
        data[at:at] = rng.randbytes(rng.randint(1, 64))
    elif op == "delete" and len(data) > 2:
        start = rng.randrange(len(data) - 1)
        del data[start:start + rng.randint(1, max(1, len(data) // 4))]
    else:
        start = rng.randrange(len(data))
        chunk = rng.randbytes(rng.randint(1, 8))
        data[start:start + len(chunk)] = chunk
    return bytes(data) or b"\x00"


def build_cases(protocol: Protocol, device: Device, position: Position,
                rng: random.Random, count: Optional[int] = None) -> List[FuzzCase]:
    """Return ``count`` test cases (the whole catalogue when ``count`` is None)."""
    cases: List[FuzzCase] = []
    for name, description, changes in RANGE_CASES:
        reason = _representable(protocol, changes)
        if reason is None:
            try:
                frame = protocol.location(device, replace(position, **changes))
            except ENCODE_ERRORS as exc:   # value cannot even be encoded for this protocol
                reason = f"value cannot be encoded for {protocol.name} ({exc})"
        if reason is not None:
            cases.append(FuzzCase(name, description, b"", "range", skip=True, skip_reason=reason))
            continue
        cases.append(FuzzCase(name, description, frame.data, "range"))

    valid = protocol.location(device, position).data
    cases += _structural(valid, rng)
    cases += protocol.extra_fuzz_cases(device, position)

    if count is None:
        return cases
    number = 0
    while len(cases) < count:
        number += 1
        cases.append(FuzzCase(f"random_mutation_{number:04d}",
                              "Seeded random mutation of a valid frame", _mutate(valid, rng), "random"))
    return cases[:count]
