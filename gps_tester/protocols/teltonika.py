"""Teltonika Codec 8 over TCP (FMB920, FMB120 and the rest of the FM family).

Session: the device sends ``<2-byte length><IMEI ascii>`` and the server
answers ``01``. Then AVL packets follow::

    00000000 | DATA_LEN(4) | 08 | N | AVL records | N | CRC-16/IBM(4)

and the server answers with the number of accepted records (4 bytes).
"""
from __future__ import annotations

import struct
from typing import List, Optional, Sequence

from gps_tester.core.checksums import crc16_ibm
from gps_tester.protocols.base import Device, Frame, FuzzCase, Position, Protocol, register

CODEC8 = 0x08


def avl_record(p: Position, priority: int = 1) -> bytes:
    ts_ms = int(p.timestamp.timestamp() * 1000) & 0xFFFFFFFFFFFFFFFF
    gps = struct.pack(
        ">IIHHBH",
        int(round(p.lon * 1e7)) & 0xFFFFFFFF,   # two's complement for negative values
        int(round(p.lat * 1e7)) & 0xFFFFFFFF,
        int(p.altitude) & 0xFFFF,
        int(p.course) & 0xFFFF,
        int(p.satellites) & 0xFF,
        int(p.speed) & 0xFFFF,
    )
    moving = 1 if p.speed > 0 else 0
    io_1b = [(239, int(p.ignition)), (240, moving), (21, 4)]   # ignition, movement, GSM signal
    io_2b = [(66, 12600)]                                      # external voltage (mV)
    io_4b = [(16, int(p.odometer) & 0xFFFFFFFF)]               # total odometer (m)
    io = bytes([0, len(io_1b) + len(io_2b) + len(io_4b)])      # event IO id, total count
    io += bytes([len(io_1b)]) + b"".join(struct.pack(">BB", i, v & 0xFF) for i, v in io_1b)
    io += bytes([len(io_2b)]) + b"".join(struct.pack(">BH", i, v & 0xFFFF) for i, v in io_2b)
    io += bytes([len(io_4b)]) + b"".join(struct.pack(">BI", i, v) for i, v in io_4b)
    io += bytes([0])                                           # no 8-byte IO elements
    return struct.pack(">QB", ts_ms, priority) + gps + io


def avl_packet(records: Sequence[bytes], codec: int = CODEC8, count: Optional[int] = None) -> bytes:
    n = (len(records) if count is None else count) & 0xFF
    body = bytes([codec, n]) + b"".join(records) + bytes([n])
    return b"\x00" * 4 + struct.pack(">I", len(body)) + body + struct.pack(">I", crc16_ibm(body))


@register
class TeltonikaProtocol(Protocol):
    name = "teltonika"
    description = "Teltonika Codec 8 (FMB920, FMB120, ...) with GPS + IO elements"
    # lat/lon are signed 32-bit (deg * 1e7); altitude/course/speed are 16-bit,
    # satellites 8-bit, odometer a 32-bit IO element.
    FIELD_LIMITS = {"lat": (-214.0, 214.0), "lon": (-214.0, 214.0),
                    "speed": (0.0, 65535.0), "course": (0.0, 65535.0),
                    "altitude": (0.0, 65535.0), "satellites": (0.0, 255.0),
                    "odometer": (0.0, float(2 ** 32 - 1))}

    def login(self, device: Device) -> Frame:
        imei = device.imei.encode("ascii")
        return Frame(struct.pack(">H", len(imei)) + imei, "login", True)

    def location(self, device: Device, position: Position) -> Frame:
        return Frame(avl_packet([avl_record(position)]), "location", True)

    def is_ack(self, frame: Frame, response: bytes) -> bool:
        if frame.kind == "login":
            return response[:1] == b"\x01"
        return (len(response) >= 4 and len(frame.data) > 9
                and int.from_bytes(response[:4], "big") == frame.data[9] > 0)

    def server_ack(self, data: bytes) -> Optional[bytes]:
        if len(data) >= 3 and int.from_bytes(data[:2], "big") == len(data) - 2 and data[2:].isdigit():
            return b"\x01"
        if len(data) >= 12 and data[:4] == b"\x00" * 4:
            length = int.from_bytes(data[4:8], "big")
            body, crc = data[8:8 + length], data[8 + length:12 + length]
            valid = (len(body) >= 3 and len(crc) == 4 and body[0] == CODEC8 and body[1] == body[-1]
                     and int.from_bytes(crc, "big") == crc16_ibm(body))
            return struct.pack(">I", body[1] if valid else 0)
        return None

    def extra_fuzz_cases(self, device: Device, position: Position) -> List[FuzzCase]:
        record = avl_record(position)
        good = avl_packet([record])
        bad_crc = good[:-4] + struct.pack(">I", (crc16_ibm(good[8:-4]) ^ 0xFFFF))
        huge_len = good[:4] + struct.pack(">I", 0xFFFFFFFF) + good[8:]
        return [
            FuzzCase("teltonika_bad_crc", "AVL packet with an invalid CRC-16", bad_crc, "checksum",
                     must_reject=True),
            FuzzCase("teltonika_count_mismatch", "Header says 5 records, packet carries 1",
                     avl_packet([record], count=5), must_reject=True),
            FuzzCase("teltonika_zero_records", "AVL packet with zero records", avl_packet([])),
            FuzzCase("teltonika_huge_data_length", "Data length field set to 0xFFFFFFFF", huge_len),
            FuzzCase("teltonika_zero_data_length", "Data length field set to 0",
                     good[:4] + b"\x00" * 4 + good[8:]),
            FuzzCase("teltonika_unknown_codec", "Unknown codec id 0x7F", avl_packet([record], codec=0x7F),
                     must_reject=True),
            FuzzCase("teltonika_bad_preamble", "Preamble is not four zero bytes", b"\xDE\xAD\xBE\xEF" + good[4:]),
            FuzzCase("teltonika_truncated_record", "Record cut in the middle of the IO element",
                     avl_packet([record[:-6]])),
            FuzzCase("teltonika_255_records", "255 records in a single packet", avl_packet([record] * 255)),
            FuzzCase("teltonika_second_login", "IMEI login frame sent again mid-session",
                     self.login(device).data),
        ]
