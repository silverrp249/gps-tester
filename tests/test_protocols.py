import random
import struct
from datetime import datetime, timezone

import pytest

from gps_tester.core.checksums import crc16_ibm, crc16_x25, sum8, xor8
from gps_tester.generators import RouteSimulator, build_cases, make_imei
from gps_tester.protocols import CustomProtocol, Device, Position, available_protocols, get_protocol

POS = Position(24.7136, 46.6753, speed=60, course=90, altitude=600, satellites=9,
               timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))


def test_checksum_reference_values():
    assert crc16_x25(b"123456789") == 0x906E
    assert crc16_ibm(b"123456789") == 0xBB3D
    assert xor8(b"\x01\x02\x04") == 0x07
    assert sum8(b"\xff\x02") == 0x01


def test_gt06_login_matches_protocol_document_example():
    frame = get_protocol("gt06").login(Device("123456789012345"))
    assert frame.data.hex() == "78780d01012345678901234500018cdd0d0a"


def test_gt06_location_layout():
    data = get_protocol("gt06").location(Device("123456789012345"), POS).data
    assert data[:2] == b"\x78\x78" and data[-2:] == b"\r\n"
    assert data[2] == len(data) - 5 == 0x1F and data[3] == 0x12
    assert data[4:10] == bytes([26, 1, 2, 3, 4, 5])
    lat, lon = struct.unpack(">II", data[11:19])
    assert lat / 1_800_000 == pytest.approx(24.7136, abs=1e-6)
    assert lon / 1_800_000 == pytest.approx(46.6753, abs=1e-6)
    assert struct.unpack(">H", data[-4:-2])[0] == crc16_x25(data[2:-4])


def test_teltonika_packet_layout_and_ack():
    proto = get_protocol("teltonika")
    device = Device("356307042441013")
    assert proto.login(device).data == b"\x00\x0f356307042441013"
    frame = proto.location(device, POS)
    data = frame.data
    length = struct.unpack(">I", data[4:8])[0]
    body = data[8:8 + length]
    assert data[:4] == b"\x00" * 4 and len(data) == length + 12
    assert body[0] == 0x08 and body[1] == body[-1] == 1
    assert struct.unpack(">I", data[-4:])[0] == crc16_ibm(body)
    assert struct.unpack(">Q", body[2:10])[0] == int(POS.timestamp.timestamp() * 1000)
    lon, lat = struct.unpack(">ii", body[11:19])
    assert (lon, lat) == (466753000, 247136000)
    assert proto.is_ack(frame, proto.server_ack(data))


def test_tk103_text_format():
    proto = get_protocol("tk103")
    device = Device("359586015829802")
    assert proto.login(device).data == b"##,imei:359586015829802,A;"
    text = proto.location(device, POS).data.decode()
    assert text.startswith("imei:359586015829802,tracker,2601020304,,F,030405.000,A,2442.8160,N,04640.5180,E,")
    assert text.endswith(";")


def test_meitrack_length_and_checksum():
    data = get_protocol("meitrack").location(Device("353358017784062"), POS).data
    text = data.decode()
    assert text.startswith("$$") and text.endswith("\r\n")
    comma, star = text.index(","), text.index("*")
    assert int(text[3:comma]) == len(text) - comma
    assert int(text[star + 1:star + 3], 16) == sum8(data[:star + 1])


def test_custom_protocol_text_and_hex():
    text = CustomProtocol({
        "checksum": {"algorithm": "xor8", "skip": 1},
        "messages": {"location": {"template": "$P,{imei},{lat:.4f}*{checksum}\r\n"}},
    }).location(Device("860000000000017"), POS).data
    body = b"P,860000000000017,24.7136*"
    assert text == b"$" + body + f"{xor8(body):02X}".encode() + b"\r\n"

    binary = CustomProtocol({
        "encoding": "hex", "transport": "udp", "checksum": {"algorithm": "crc16_x25"},
        "messages": {"location": {"template": "aa55 {lat_e7:08x} {checksum}"}},
    })
    data = binary.location(Device("860000000000017"), POS).data
    assert data[:6] == b"\xaa\x55" + struct.pack(">I", 247136000)
    assert data[6:] == struct.pack(">H", crc16_x25(data[:6]))
    assert binary.default_transport == "udp"


def test_custom_protocol_rejects_bad_templates():
    with pytest.raises(ValueError):
        CustomProtocol({"messages": {"location": {"template": "{no_such_field}"}}})
    with pytest.raises(ValueError):
        CustomProtocol({"messages": {}})


def test_imei_has_valid_luhn_digit():
    imei = make_imei("86123400000000", 42)
    digits = [int(c) for c in imei]
    total = sum(digits[-1::-2]) + sum(sum(divmod(2 * d, 10)) for d in digits[-2::-2])
    assert len(imei) == 15 and total % 10 == 0


def test_route_stays_near_centre():
    route = RouteSimulator(24.7136, 46.6753, random.Random(1))
    for _ in range(2000):
        pos = route.step(5)
        assert abs(pos.lat - 24.7136) < 0.5 and abs(pos.lon - 46.6753) < 0.5
        assert 0 <= pos.speed <= 130 and 0 <= pos.course < 360


@pytest.mark.parametrize("name", sorted(available_protocols()))
def test_fuzz_catalogue(name):
    proto = get_protocol(name)
    cases = build_cases(proto, Device("861234000000007"), POS, random.Random(7))
    names = [c.name for c in cases]
    assert len(names) == len(set(names)) and len(cases) > 25
    assert {"range", "structure", "connection"} <= {c.category for c in cases}
    assert len(build_cases(proto, Device("861234000000007"), POS, random.Random(7), 300)) == 300
    again = build_cases(proto, Device("861234000000007"), POS, random.Random(7), 300)
    first = build_cases(proto, Device("861234000000007"), POS, random.Random(7), 300)
    assert [c.payload for c in again[-50:]] == [c.payload for c in first[-50:]]  # seeded = reproducible


def test_gt06_skips_unrepresentable_range_cases():
    """Fields GT06 cannot carry, or values it would clamp, must be skipped - not sent."""
    cases = {c.name: c for c in build_cases(get_protocol("gt06"), Device("861234000000007"), POS,
                                            random.Random(7))}
    for name in ("altitude_negative", "altitude_huge", "odometer_overflow",
                 "speed_negative", "speed_huge", "speed_int32_overflow", "satellites_255"):
        assert cases[name].skip and cases[name].payload == b"" and cases[name].skip_reason
    # Values GT06 *does* carry faithfully are still sent.
    for name in ("lat_above_max", "course_above_360", "satellites_zero", "timestamp_2038"):
        assert not cases[name].skip and cases[name].payload


def test_text_protocols_send_all_carried_fields():
    """TK103 has no altitude field (skip) but transmits speed/course faithfully as text."""
    cases = {c.name: c for c in build_cases(get_protocol("tk103"), Device("861234000000007"), POS,
                                            random.Random(7))}
    assert cases["altitude_huge"].skip                 # not in the tracker sentence
    assert not cases["speed_huge"].skip                # text carries -50 / 99999 literally
    assert not cases["speed_negative"].skip
    # Meitrack carries altitude and odometer, so nothing extra is skipped there.
    mt = {c.name: c for c in build_cases(get_protocol("meitrack"), Device("861234000000007"), POS,
                                         random.Random(7))}
    assert not mt["altitude_huge"].skip and not mt["odometer_overflow"].skip


def test_audit_session_outcome_accept_vs_reject():
    from gps_tester.core.audit import SessionOutcome
    good = SessionOutcome(connected=True, login_ack=True, stayed_open=True)
    assert good.accepted and not good.rejected
    for bad in (SessionOutcome(connected=False),
                SessionOutcome(connected=True, login_ack=False, stayed_open=True),
                SessionOutcome(connected=True, login_ack=None, stayed_open=False)):
        assert bad.rejected and not bad.accepted
