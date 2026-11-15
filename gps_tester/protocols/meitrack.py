"""Meitrack plain-text protocol (MVT/T-series).

    $$<flag><len>,<IMEI>,AAA,<event>,<lat>,<lon>,<yymmddHHMMSS>,A,<sats>,<gsm>,
    <speed>,<course>,<hdop>,<alt>,<odometer>,<runtime>,<mcc|mnc|lac|cell>,
    <io>,<analog>,*<checksum>\r\n

``len`` counts from the first comma to ``\r\n``; the checksum is the 8-bit
sum of everything from ``$$`` up to and including ``*``.
"""
from __future__ import annotations

from typing import List

from gps_tester.core.checksums import sum8
from gps_tester.protocols.base import Device, Frame, FuzzCase, Position, Protocol, register


def wrap(flag: str, body: str) -> bytes:
    """``body`` starts with ',' and ends with '*'."""
    head = f"$${flag}{len(body) + 4}{body}"
    return f"{head}{sum8(head.encode()):02X}\r\n".encode()


@register
class MeitrackProtocol(Protocol):
    name = "meitrack"
    description = "Meitrack plain text '$$<flag><len>,<imei>,AAA,...' with checksum"

    def location(self, device: Device, position: Position) -> Frame:
        p = position
        flag = chr(ord("A") + device.next_seq() % 26)
        body = (
            f",{device.imei},AAA,35,{p.lat:.6f},{p.lon:.6f},{p.timestamp:%y%m%d%H%M%S},A,"
            f"{int(p.satellites)},20,{p.speed:.0f},{p.course:.0f},1.0,{p.altitude:.0f},"
            f"{p.odometer:.0f},3600,420|1|1234|ABCD,{'0400' if p.ignition else '0000'},"
            f"0001|0002||0A5F|0100,*"
        )
        return Frame(wrap(flag, body), "location", False)

    def extra_fuzz_cases(self, device: Device, position: Position) -> List[FuzzCase]:
        valid = self.location(device, position).data
        imei = device.imei
        return [
            FuzzCase("meitrack_bad_checksum", "Wrong checksum byte", valid[:-4] + b"ZZ\r\n", "checksum",
                     must_reject=True),
            FuzzCase("meitrack_wrong_length", "Length field larger than the message",
                     valid[:3] + b"9999" + valid[valid.index(b","):]),
            FuzzCase("meitrack_empty_fields", "All data fields empty", wrap("A", f",{imei},AAA,,,,,,,,,,,,,,,,,*")),
            FuzzCase("meitrack_missing_fields", "Message ends after the command", wrap("A", f",{imei},AAA,*")),
            FuzzCase("meitrack_no_crlf", "Missing CRLF terminator", valid[:-2]),
            FuzzCase("meitrack_non_numeric", "Letters in numeric fields",
                     wrap("A", f",{imei},AAA,xx,lat,lon,notadate,A,s,g,fast,north,h,a,o,r,m,i,a,*"), "range"),
        ]
