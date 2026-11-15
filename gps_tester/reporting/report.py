"""JSON and self-contained HTML session reports."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any, Dict, Tuple

from gps_tester.core.stats import Stats

CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f6f9;color:#1c2430}
main{max-width:1100px;margin:0 auto;padding:32px 24px}
h1{margin:0 0 4px;font-size:26px} h2{margin:32px 0 12px;font-size:18px}
.sub{color:#5b6777;margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card .label{font-size:12px;color:#5b6777;text-transform:uppercase;letter-spacing:.04em}
.card .value{font-size:24px;font-weight:600;margin-top:4px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;
box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:14px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #e8ecf1;vertical-align:top}
th{background:#eef1f5;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#5b6777}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;word-break:break-all;color:#44505f}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pass{background:#dcf5e3;color:#176b34}.warn{background:#fdf0cd;color:#8a5a00}.fail{background:#fbdcdc;color:#a11919}
.good{color:#176b34}.bad{color:#a11919}
footer{margin-top:32px;color:#8a94a3;font-size:12px}
"""


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    if isinstance(value, int):
        return f"{value:,}{suffix}"
    return escape(str(value)) + suffix


def _card(label: str, value: str, css: str = "") -> str:
    return f'<div class="card"><div class="label">{escape(label)}</div><div class="value {css}">{value}</div></div>'


def _table(headers, rows) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(data: Dict[str, Any]) -> str:
    summary, latency, settings = data["summary"], data["latency_ms"], data["settings"]
    bad = lambda n: "bad" if n else "good"  # noqa: E731
    cards = [
        _card("Packets sent", _fmt(summary["packets_sent"])),
        _card("Acknowledged" if data["mode"] == "simulate" else "Server replies", f'{_fmt(summary["ack_received"])} / {_fmt(summary["ack_expected"])}'),
        _card("ACK rate" if data["mode"] == "simulate" else "Reply rate", _fmt(summary["ack_rate_percent"], "%")),
        _card("No response", _fmt(summary["timeouts"]), bad(summary["timeouts"]) if data["mode"] == "simulate" else ""),
        _card("Disconnects", _fmt(summary["disconnects"]), bad(summary["disconnects"])),
        _card("Connect errors", _fmt(summary["connect_errors"]), bad(summary["connect_errors"])),
        _card("Avg latency", _fmt(latency["avg"], " ms")),
        _card("Throughput", _fmt(summary["packets_per_second"], " pkt/s")),
    ]
    parts = [
        "<h1>GPS Telemetry Test Report</h1>",
        f'<div class="sub">Mode <b>{escape(data["mode"])}</b> &middot; protocol <b>{_fmt(settings.get("protocol"))}</b>'
        f' &middot; target <b>{_fmt(settings.get("transport"))}://{_fmt(settings.get("host"))}:{escape(str(settings.get("port")))}</b>'
        f' &middot; started {escape(data["started_at"])} &middot; duration {_fmt(data["duration_seconds"], " s")}</div>',
        f'<div class="cards">{"".join(cards)}</div>',
        "<h2>Response latency</h2>",
        _table(["Min", "Average", "Median (p50)", "p95", "p99", "Max"],
               [[_fmt(latency[k], " ms") for k in ("min", "avg", "p50", "p95", "p99", "max")]]),
    ]
    if "findings" in data:
        order = {"fail": 0, "warn": 1, "pass": 2}
        rows = []
        for f in sorted(data["findings"], key=lambda f: order.get(f["severity"], 3)):
            ev = "<br>".join(escape(e) for e in f["evidence"])
            rows.append([escape(f["title"]),
                         f'<span class="badge {f["severity"]}">{escape(f["status"])}</span>',
                         escape(f["detail"]) + (f'<br><code>{ev}</code>' if ev else "")])
        parts += [
            "<h2>Security audit</h2>",
            "<p>Every finding is inferred from how the server responded to traffic sent from outside; "
            "the tool does not read the server's logs, database or configuration.</p>",
            _table(["Check", "Result", "Finding &amp; evidence"], rows),
        ]

    if "cases" in data:
        outcomes = data["outcomes"]
        severity = {"handled": "pass", "disconnected": "warn", "accepted": "warn",
                    "unresponsive": "fail", "skipped": "pass"}
        badges = " ".join(f'<span class="badge {severity.get(k, "fail")}">{escape(k)}: {v}</span>'
                          for k, v in outcomes.items())
        rows = [[str(c["index"]), escape(c["name"]), escape(c["category"]), escape(c["description"]),
                 _fmt(c["size"], " B"), f'<span class="badge {c["severity"]}">{escape(c["outcome"])}</span>',
                 escape(c["detail"]), f'<code>{escape(c["payload_hex"])}</code>'] for c in data["cases"]]
        parts += [
            "<h2>Robustness test cases</h2>", f"<p>{badges}</p>",
            "<p><b>handled</b>: the server ignored or answered the frame and kept working. "
            "<b>disconnected</b>: the server closed the connection but still accepts new sessions. "
            "<b>accepted</b>: the server acknowledged a frame it should have rejected. "
            "<b>unresponsive</b>: the server stopped answering valid traffic after this frame. "
            "<b>skipped</b>: this protocol cannot carry the value, so sending it would prove nothing.</p>",
            _table(["#", "Case", "Category", "Description", "Size", "Outcome", "Detail", "Payload (hex)"], rows),
        ]
    if data["events"]:
        rows = [[escape(e["time"]), escape(e["kind"]), escape(e["message"])] for e in data["events"][:500]]
        parts += ["<h2>Events (disconnects, timeouts, errors)</h2>", _table(["Time", "Kind", "Message"], rows)]
    rows = [[escape(str(k)), "-" if v is None else escape(str(v))] for k, v in settings.items()]
    parts += ["<h2>Settings</h2>", _table(["Option", "Value"], rows),
              "<footer>Generated by gps-tester - GPS Telemetry Simulator &amp; Protocol Validator</footer>"]
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>gps-tester report</title><style>{CSS}</style></head>"
            f'<body><main>{"".join(parts)}</main></body></html>')


def write_reports(stats: Stats, directory: str) -> Tuple[Path, Path]:
    data = stats.to_dict()
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"gps-tester_{stats.mode}_{stats.started_at:%Y%m%d_%H%M%S}"
    json_path, html_path = out / f"{stem}.json", out / f"{stem}.html"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    html_path.write_text(render_html(data), encoding="utf-8")
    return json_path, html_path
