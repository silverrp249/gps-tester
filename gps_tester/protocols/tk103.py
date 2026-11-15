"""TK103 / GPS103 plain-text protocol (Xexun, Coban and clones).

    login      ##,imei:359586015829802,A;                      -> LOAD
    heartbeat  359586015829802;                                -> ON
    location   imei:359586015829802,tracker,YYMMDDhhmm,,F,hhmmss.000,A,
               ddmm.mmmm,N,dddmm.mmmm,E,<knots>,<course>;
"""
from __future__ import annotations

from typing import List, Optional

from gps_tester.protocols.base import Device, Frame, FuzzCase, Position, Protocol, register


def to_nmea(value: float, degree_digits: int) -> str:
    """Decimal degrees -> (d)ddmm.mmmm."""
    value = abs(value)
    degrees = int(value)
    return f"{degrees:0{degree_digits}d}{(value - degrees) * 60:07.4f}"


@register
class TK103Protocol(Protocol):
    name = "tk103"
    description = "TK103/GPS103 plain text 'imei:xxxx,tracker,...' (Xexun, Coban)"
    # The tracker sentence carries position, speed and course only.
    LOCATION_FIELDS = {"lat", "lon", "speed", "course", "timestamp"}

    def login(self, device: Device) -> Frame:
        return Frame(f"##,imei:{device.imei},A;".encode(), "login", True)

    def heartbeat(self, device: Device) -> Frame:
        return Frame(f"{device.imei};".encode(), "heartbeat", True)

    def location(self, device: Device, position: Position) -> Frame:
        p, t = position, position.timestamp
        text = (
            f"imei:{device.imei},tracker,{t:%y%m%d%H%M},,F,{t:%H%M%S}.000,A,"
            f"{to_nmea(p.lat, 2)},{'N' if p.lat >= 0 else 'S'},"
            f"{to_nmea(p.lon, 3)},{'E' if p.lon >= 0 else 'W'},"
            f"{p.speed / 1.852:.2f},{p.course:.2f};"
        )
        return Frame(text.encode(), "location", False)

    def is_ack(self, frame: Frame, response: bytes) -> bool:
        return (b"LOAD" if frame.kind == "login" else b"ON") in response

    def server_ack(self, data: bytes) -> Optional[bytes]:
        if data.startswith(b"##"):
            return b"LOAD"
        if data.endswith(b";") and data[:-1].isdigit():
            return b"ON"
        return None

    def extra_fuzz_cases(self, device: Device, position: Position) -> List[FuzzCase]:
        imei = device.imei
        valid = self.location(device, position).data.decode()

        def case(name: str, desc: str, text: str, category: str = "structure") -> FuzzCase:
            return FuzzCase(name, desc, text.encode("utf-8", "surrogatepass"), category)

        return [
            case("tk103_empty_fields", "All fields after the keyword are empty",
                 f"imei:{imei},tracker,,,,,,,,,,,;"),
            case("tk103_missing_fields", "Message ends right after the keyword", f"imei:{imei},tracker;"),
            case("tk103_empty_imei", "Empty IMEI field", valid.replace(imei, "")),
            case("tk103_long_imei", "IMEI of 5000 digits", valid.replace(imei, "9" * 5000)),
            case("tk103_non_numeric_coords", "Letters where coordinates are expected",
                 f"imei:{imei},tracker,2601011200,,F,120000.000,A,abcd.efgh,N,ijklm.nopq,E,x,y;", "range"),
            case("tk103_no_terminator", "Message without the ';' terminator", valid.rstrip(";")),
            case("tk103_extra_fields", "200 unexpected extra fields", valid.rstrip(";") + ",x" * 200 + ";"),
            case("tk103_unknown_keyword", "Unknown keyword instead of 'tracker'",
                 valid.replace(",tracker,", ",%s%n%x,")),
            case("tk103_control_chars", "NUL and control characters inside fields",
                 valid.replace(",tracker,", ",tra\x00ck\r\ner,")),
            case("tk103_invalid_hemisphere", "Hemisphere letters X/Y", valid.replace(",N,", ",X,").replace(",E,", ",Y,"),
                 "range"),
            FuzzCase("tk103_non_utf8", "Invalid UTF-8 bytes inside the message",
                     f"imei:{imei},tracker,".encode() + b"\xff\xfe\xc3\x28\x80;"),
        ]
