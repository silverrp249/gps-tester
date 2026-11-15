# GPS Telemetry Simulator & Protocol Validator (`gps-tester`)

An open-source command line tool for **QA, load testing and robustness testing of
vehicle-tracking and IoT servers**. It simulates fleets of real GPS trackers and
checks how your server copes with unexpected input *before* you go live.

- **Simulate & load** - hundreds or thousands of concurrent virtual vehicles
  (asyncio) driving a realistic route and reporting live positions.
- **Validate robustness** - edge cases and malformed frames (out-of-range
  coordinates, negative/huge speed, empty fields, truncated packets, bad
  checksums, abrupt disconnects...) with a health check after every case.
- **AI auto-parser (optional)** - give it 3 captured messages from an unknown
  device and an LLM drafts a protocol template for you.
- **Live dashboard** (rich) plus **JSON and HTML reports** for every session.

> **Use it only on servers you own or are explicitly authorised to test.**
> The tool generates load and malformed traffic. For any target that is not a
> local/private address it asks for confirmation (or `--authorized`).

## Supported protocols

| Name        | Devices                              | Format                                            |
|-------------|--------------------------------------|---------------------------------------------------|
| `gt06`      | Concox, Jimi, GT06/CRX clones        | Binary, CRC-ITU computed automatically            |
| `teltonika` | FMB920, FMB120, FM family            | Codec 8 over TCP, GPS + IO elements, CRC-16/IBM   |
| `tk103`     | Xexun, Coban, TK103/GPS103 clones    | Plain text `imei:xxxx,tracker,...`                |
| `meitrack`  | Meitrack MVT / T series              | Plain text `$$<flag><len>,<imei>,AAA,...*<sum>`   |
| `custom`    | Anything                             | Your own JSON/YAML template over TCP or UDP       |

## Installation

```bash
git clone https://github.com/<your-user>/gps-tester.git
cd gps-tester
pip install .            # or: pip install -e ".[dev]" for development
```

Requires Python 3.9+. Docker:

```bash
docker build -t gps-tester .
docker run --rm -it --network host -v "$PWD/reports:/reports" \
    gps-tester --host 127.0.0.1 --port 5000 --protocol gt06 --devices 100
```

## Quick start (no real server needed)

A small mock server is included so you can try everything locally:

```bash
gps-tester-mock --protocol gt06 --port 5000          # terminal 1
gps-tester --host 127.0.0.1 --port 5000 --protocol gt06 --mode simulate --devices 100 --interval 5   # terminal 2
```

## Usage

### 1. Simulation & load mode

```bash
# 100 GT06 devices, one position every 5 s, until Ctrl+C
gps-tester --host 127.0.0.1 --port 5000 --protocol gt06 --mode simulate --devices 100 --interval 5

# 2000 Teltonika devices for 10 minutes, connecting over 60 s, centred on Jeddah
gps-tester --host 10.0.0.5 --port 5027 --protocol teltonika --devices 2000 --interval 10 \
           --duration 600 --ramp-up 60 --lat 21.4858 --lon 39.1925
```

Every device has its own valid IMEI (Luhn check digit), logs in, moves along a
smooth random route and reconnects with back-off if the server drops it.
Latency is measured on every frame the protocol acknowledges (GT06 login and
heartbeat, every Teltonika AVL packet, TK103 login and heartbeat).

For thousands of devices raise the open-file limit first: `ulimit -n 65535`.

### 2b. Security audit mode

```bash
# Infer the server's defensive posture from the outside
gps-tester --host 127.0.0.1 --port 5000 --protocol gt06 --mode audit
# With a real enrolled device as a baseline (far more accurate IMEI check)
gps-tester --host 127.0.0.1 --port 5000 --protocol gt06 --mode audit --registered-imei 123456789012345
```

Audit mode is a **black-box** check: the tool is only a client, so every
finding is inferred from how the server *responds* to traffic. It never reads
the server's logs, database or configuration. It runs four checks and writes a
combined report:

| Check | What it looks for | Result |
|-------|-------------------|--------|
| Server reachable   | can a session be opened and is login acknowledged?        | `ok` / `unreachable` |
| Device authentication (IMEI) | does an unregistered IMEI get treated differently from `--registered-imei`? | `present` / `absent` / `inconclusive` |
| Connection rate limiting | does the server start refusing connections as the rate climbs? | `present` / `absent` / `inconclusive` |
| Malformed-input resilience | the robustness suite above, summarised | `ok` / `weak-validation` / `issue` |

Without `--registered-imei` the IMEI check can only flag that an unknown device
was accepted (`likely-absent`); with it, the tool compares the two and can say
`present` or `absent` for certain. The process exits with code **1** if any
check fails, so it can gate a pipeline.

What it **cannot** do: read your server's internals, prove *why* a packet was
dropped, or confirm an IMEI is really enrolled. Firewall / fail2ban rules live
on your server; this tool tells you whether their effect is observable, not how
to configure them.

### 2. Robustness & input-validation mode

```bash
gps-tester --host 127.0.0.1 --port 5000 --protocol teltonika --mode validate --count 1000
gps-tester --host 127.0.0.1 --port 5000 --protocol tk103 --mode validate      # whole catalogue
```

Each case opens a normal session, sends one bad payload, then opens a fresh
valid session to check that the server is still alive.

| Category     | Examples                                                                          |
|--------------|-----------------------------------------------------------------------------------|
| `range`      | latitude 91.5, longitude -540, NaN, speed -50 / 99999 / infinite, year 1970/2099   |
| `structure`  | truncated frames, header only, oversized (64 KiB), duplicates, leading garbage     |
| `checksum`   | bad CRC / checksum, wrong length field, unknown codec, record-count mismatch       |
| `connection` | half a frame then TCP reset, reset right after login                               |
| `random`     | seeded random mutations (`--count` above the catalogue size, `--seed` to replay)   |

| Outcome        | Meaning                                                              |
|----------------|----------------------------------------------------------------------|
| `handled`      | server ignored or answered the frame and kept working                 |
| `disconnected` | server closed the connection, but new sessions still work             |
| `accepted`     | server acknowledged a frame it should have rejected (weak validation) |
| `unresponsive` | server stopped answering valid traffic - probable crash or hang       |
| `skipped`      | value cannot be represented in this protocol, so it was not sent       |

Range cases that a protocol cannot carry faithfully - a field it does not
transmit (e.g. GT06 has no altitude or odometer), or a value its field width
would clamp back into the valid range (e.g. a 1-byte GT06 speed cannot hold
99999) - are reported as `skipped` with the reason, instead of being sent as a
harmless valid-looking frame. Text protocols (TK103, Meitrack) carry values
literally, so they skip far fewer.

The process exits with code **1** if any case ends `unresponsive`, so it can
gate a CI pipeline (`--no-dashboard` gives plain output).

### 3. Custom protocol from a template

```bash
gps-tester --config examples/custom_device.yaml --mode validate
```

```yaml
name: acme-tracker
transport: tcp            # tcp | udp
host: 127.0.0.1
port: 6000
encoding: text            # text | hex
checksum: {algorithm: xor8, format: "{:02X}", skip: 1}
messages:
  login:
    template: "$LOGIN,{imei}*{checksum}\r\n"
    expect_response: true
  location:
    template: "$POS,{imei},{date},{time},{lat:.6f},{lon:.6f},{speed:.1f},{course:.0f}*{checksum}\r\n"
```

Templates are Python format strings. Placeholders: `imei imei_bcd index seq lat
lon lat_abs lon_abs ns ew lat_nmea lon_nmea lat_e7 lon_e7 speed speed_int
speed_knots course course_int altitude altitude_int satellites ignition odometer
timestamp timestamp_ms date time datetime year month day hour minute second
checksum`. Checksums: `xor8`, `sum8`, `crc16_x25` (CRC-ITU), `crc16_ibm`.
See `examples/custom_binary_udp.json` for a binary UDP device.

### 4. AI auto-parser (optional)

```bash
export GPS_TESTER_LLM_API_KEY=...            # or ANTHROPIC_API_KEY / OPENAI_API_KEY
gps-tester --mode learn --samples-file examples/samples.txt --output my_device.yaml
gps-tester --mode learn --sample "hex:78780d01..." --sample "hex:..." --sample "hex:..." \
           --llm-provider openai --llm-base-url http://localhost:11434/v1 --llm-model llama3.1
gps-tester --config my_device.yaml --host 127.0.0.1 --port 6000 --mode validate
```

`--llm-provider anthropic` uses the Anthropic Messages API; `openai` works with
any OpenAI-compatible endpoint (including self-hosted models). The generated
template is validated locally, but **review it** - it is a draft. Samples are
sent to the provider you choose, so strip sensitive data first.

## Reports

Each session writes `reports/gps-tester_<mode>_<timestamp>.json` and `.html`:
packets sent, acknowledgements, timeouts, disconnects, connect errors,
latency (min / avg / p50 / p95 / p99 / max), an event log and, in validate
mode, every test case with its payload and outcome.

## All options

Run `gps-tester --help` and `gps-tester --list-protocols`.

## Project structure

```
gps-tester/
├── pyproject.toml            packaging (pip install .)
├── Dockerfile
├── README.md / LICENSE
├── examples/                 custom templates + sample captures
├── tests/                    pytest suite
└── gps_tester/
    ├── cli.py                click command line interface
    ├── core/
    │   ├── transport.py      asyncio TCP/UDP client
    │   ├── runner.py         simulate + validate modes
    │   ├── stats.py          counters, latency, case results
    │   ├── checksums.py      CRC-ITU, CRC-16/IBM, xor8, sum8
    │   └── mock_server.py    gps-tester-mock
    ├── protocols/
    │   ├── base.py           Protocol base class + registry
    │   ├── gt06.py  teltonika.py  tk103.py  meitrack.py  custom.py
    ├── generators/
    │   ├── route.py          virtual vehicle routes, IMEI generator
    │   └── fuzz.py           edge cases and malformed frames
    ├── reporting/
    │   ├── dashboard.py      live rich dashboard
    │   └── report.py         JSON + HTML reports
    └── ai/
        └── auto_parser.py    LLM protocol inference
```

## Adding a protocol

```python
# gps_tester/protocols/myproto.py
from gps_tester.protocols.base import Device, Frame, Position, Protocol, register

@register
class MyProtocol(Protocol):
    name = "myproto"
    description = "My device"

    def login(self, device: Device):                 # optional
        return Frame(f"HELLO {device.imei}\n".encode(), "login", expect_response=True)

    def location(self, device: Device, position: Position):
        return Frame(f"{device.imei},{position.lat},{position.lon}\n".encode())
```

Import the module in `gps_tester/protocols/__init__.py` and it appears in
`--protocol`. Optionally implement `heartbeat`, `is_ack`, `server_ack` (mock
server) and `extra_fuzz_cases` (protocol-specific malformed frames).

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Limitations

- Encoders cover the core messages (login, location, heartbeat); alarms,
  commands and Teltonika Codec 8 Extended / UDP are not implemented yet.
- GT06 location packets and TK103/Meitrack positions are not acknowledged by
  the protocol, so latency for them comes from logins and heartbeats.
- Over UDP without acknowledgements the tool cannot tell whether a server is
  still alive; validate results are then informational.

## نبذة بالعربية

أداة مفتوحة المصدر لاختبار استقرار ومتانة سيرفرات تتبع المركبات وإنترنت الأشياء:
محاكاة مئات الأجهزة المتزامنة (GT06، Teltonika Codec 8، TK103، Meitrack أو قالب
مخصص عبر JSON/YAML)، اختبار معالجة البيانات المشوهة والحالات الحدية، لوحة نتائج
حية، وتقارير JSON و HTML. استخدمها فقط على خوادم تملكها أو لديك تصريح باختبارها.

## License

MIT - see [LICENSE](LICENSE).
