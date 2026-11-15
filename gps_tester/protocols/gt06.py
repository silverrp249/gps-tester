"""GT06 / CRX binary protocol (Concox, Jimi and compatible trackers).

Frame layout::

    78 78 | LEN | PROTO | CONTENT ... | SERIAL(2) | CRC-ITU(2) | 0D 0A

``LEN`` counts PROTO..CRC. The CRC covers LEN..SERIAL.
"""
from __future__ import annotations

import struct
from typing import List, Optional

from gps_tester.core.checksums import crc16_x25
from gps_tester.protocols.base import Device, Frame, FuzzCase, Position, Protocol, register

LOGIN, LOCATION, HEARTBEAT = 0x01, 0x12, 0x13


def packet(proto: int, content: bytes, serial: int) -> bytes:
    body = bytes([(len(content) + 5) & 0xFF, proto]) + content + struct.pack(">H", serial & 0xFFFF)
    return b"\x78\x78" + body + struct.pack(">H", crc16_x25(body)) + b"\r\n"


@register
class GT06Protocol(Protocol):
    name = "gt06"
    description = "GT06/CRX binary protocol (Concox, Jimi) with CRC-ITU"
    # The 0x12 location packet has no altitude or odometer field. Speed is one
    # byte, satellites four bits, course ten bits, lat/lon a 32-bit scaled int.
    LOCATION_FIELDS = {"lat", "lon", "speed", "course", "satellites", "timestamp"}
    FIELD_LIMITS = {"lat": (-2386.0, 2386.0), "lon": (-2386.0, 2386.0),
                    "speed": (0.0, 255.0), "course": (0.0, 1023.0), "satellites": (0.0, 15.0)}

    def login(self, device: Device) -> Frame:
        imei_bcd = bytes.fromhex(device.imei.rjust(16, "0")[-16:])
        return Frame(packet(LOGIN, imei_bcd, device.next_seq()), "login", True)

    def location(self, device: Device, position: Position) -> Frame:
        return Frame(packet(LOCATION, self._location_content(position), device.next_seq()),
                     "location", False)  # GT06 servers do not acknowledge 0x12

    def heartbeat(self, device: Device) -> Frame:
        # terminal info, voltage level, GSM signal, alarm/language
        return Frame(packet(HEARTBEAT, bytes([0x45, 0x06, 0x04, 0x00, 0x02]), device.next_seq()),
                     "heartbeat", True)

    @staticmethod
    def _location_content(p: Position) -> bytes:
        t = p.timestamp
        when = bytes([t.year % 100, t.month, t.day, t.hour, t.minute, t.second])
        gps_info = 0xC0 | (int(p.satellites) & 0x0F)            # 12 bytes of GPS data + satellites
        lat = int(round(abs(p.lat) * 1_800_000)) & 0xFFFFFFFF  # minutes * 30000
        lon = int(round(abs(p.lon) * 1_800_000)) & 0xFFFFFFFF
        status = (int(p.course) & 0x03FF) | 0x1000              # bit12: GPS fix valid
        if p.lat >= 0:
            status |= 0x0400                                    # bit10: north latitude
        if p.lon < 0:
            status |= 0x0800                                    # bit11: west longitude
        lbs = struct.pack(">HBH", 420, 1, 0x1234) + (0x00ABCD).to_bytes(3, "big")  # MCC MNC LAC CellID
        return (when + bytes([gps_info]) + struct.pack(">II", lat, lon)
                + bytes([int(p.speed) & 0xFF]) + struct.pack(">H", status) + lbs)

    def is_ack(self, frame: Frame, response: bytes) -> bool:
        return (len(response) >= 10 and response[:2] == b"\x78\x78"
                and len(frame.data) > 3 and response[3] == frame.data[3])

    def server_ack(self, data: bytes) -> Optional[bytes]:
        out, i = b"", 0
        while i + 5 <= len(data):
            if data[i:i + 2] != b"\x78\x78":
                i += 1
                continue
            end = i + 3 + data[i + 2] + 2
            if data[i + 2] < 5 or end > len(data):
                break
            body, crc = data[i + 2:end - 4], data[end - 4:end - 2]
            if crc == struct.pack(">H", crc16_x25(body)) and body[1] in (LOGIN, HEARTBEAT):
                out += packet(body[1], b"", int.from_bytes(body[-2:], "big"))
            i = end
        return out or None

    def extra_fuzz_cases(self, device: Device, position: Position) -> List[FuzzCase]:
        hb = self.heartbeat(device).data
        loc = self.location(device, position).data
        content = self._location_content(position)
        bad_crc = hb[:-4] + bytes([hb[-4] ^ 0xFF, hb[-3] ^ 0xFF]) + hb[-2:]
        return [
            FuzzCase("gt06_bad_crc", "Heartbeat with an invalid CRC-ITU", bad_crc, "checksum",
                     must_reject=True),
            FuzzCase("gt06_length_too_large", "Length byte claims 255 bytes, frame is shorter",
                     loc[:2] + b"\xFF" + loc[3:]),
            FuzzCase("gt06_length_zero", "Length byte is zero", loc[:2] + b"\x00" + loc[3:]),
            FuzzCase("gt06_unknown_protocol", "Unknown protocol number 0xEE with valid CRC",
                     packet(0xEE, content, device.next_seq())),
            FuzzCase("gt06_empty_content", "Location packet with no content at all",
                     packet(LOCATION, b"", device.next_seq())),
            FuzzCase("gt06_missing_stop_bits", "Frame without the trailing 0D 0A", loc[:-2]),
            FuzzCase("gt06_wrong_start_bits", "Start bits FF FF instead of 78 78", b"\xFF\xFF" + loc[2:]),
            FuzzCase("gt06_invalid_datetime", "Date/time bytes set to month 99, day 99, hour 99",
                     packet(LOCATION, bytes([99] * 6) + content[6:], device.next_seq()), "range"),
        ]
