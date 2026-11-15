"""Checksum algorithms shared by the protocol encoders."""
from __future__ import annotations

from typing import Callable, Dict


def crc16_x25(data: bytes) -> int:
    """CRC-ITU (CRC-16/X-25) used by GT06/Concox. Check value: 0x906E."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def crc16_ibm(data: bytes) -> int:
    """CRC-16/IBM (ARC) used by Teltonika. Check value: 0xBB3D."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def xor8(data: bytes) -> int:
    """XOR of all bytes (NMEA style)."""
    result = 0
    for byte in data:
        result ^= byte
    return result


def sum8(data: bytes) -> int:
    """Sum of all bytes modulo 256 (Meitrack style)."""
    return sum(data) & 0xFF


ALGORITHMS: Dict[str, Callable[[bytes], int]] = {
    "crc16_x25": crc16_x25,
    "crc16_itu": crc16_x25,
    "crc16_ibm": crc16_ibm,
    "xor8": xor8,
    "sum8": sum8,
}
