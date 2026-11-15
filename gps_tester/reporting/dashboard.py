"""Live, coloured terminal dashboard built with rich."""
from __future__ import annotations

import asyncio

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from gps_tester.core.stats import FAIL, OUTCOMES, PASS, Stats

COLORS = {PASS: "green", "warn": "yellow", FAIL: "red"}


def _ms(value) -> str:
    return "-" if value is None else f"{value:,.1f} ms"


def render(stats: Stats) -> Panel:
    meta = stats.meta
    latency = stats.latency_summary()
    table = Table.grid(padding=(0, 3))
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_column(style="bold")
    table.add_column(justify="right")

    rate = f"{100 * stats.acked / stats.expected:.1f}%" if stats.expected else "-"
    pps = stats.sent / stats.elapsed if stats.elapsed > 0 else 0
    table.add_row("Packets sent", f"[cyan]{stats.sent:,}[/]", "Avg latency", f"[cyan]{_ms(latency['avg'])}[/]")
    label = "Acknowledged" if stats.mode == "simulate" else "Server replies"
    table.add_row(label, f"[green]{stats.acked:,}[/] / {stats.expected:,} ({rate})",
                  "p95 latency", _ms(latency["p95"]))
    table.add_row("No response (timeout)", f"[{'red' if stats.timeouts else 'green'}]{stats.timeouts:,}[/]",
                  "Max latency", _ms(latency["max"]))
    table.add_row("Disconnects", f"[{'red' if stats.disconnects else 'green'}]{stats.disconnects:,}[/]",
                  "Throughput", f"{pps:,.1f} pkt/s")
    table.add_row("Connect errors", f"[{'red' if stats.connect_errors else 'green'}]{stats.connect_errors:,}[/]",
                  "Data sent", f"{stats.bytes_sent / 1024:,.1f} KiB")
    if stats.mode == "simulate":
        table.add_row("Active devices", f"[cyan]{stats.active_connections:,}[/] / {meta.get('devices', '?')}",
                      "Unexpected replies", f"{stats.bad_responses:,}")
    else:
        counts = stats.outcome_counts()
        parts = "  ".join(f"[{COLORS[OUTCOMES[name]]}]{name}: {counts[name]}[/]" for name in OUTCOMES)
        table.add_row("Cases run", f"[cyan]{len(stats.cases)}[/] / {stats.total_cases}", "", "")
        table.add_row(Text.from_markup(parts), "", "", "")

    events = Text("\n".join(stats.events) if stats.events else "no errors so far", style="dim")
    title = (f"[bold]gps-tester[/] | {stats.mode} | {meta.get('protocol')} | "
             f"{meta.get('transport', 'tcp')}://{meta.get('host')}:{meta.get('port')} | {stats.elapsed:,.0f}s")
    return Panel(Group(table, Text(""), Text("Recent events", style="bold"), events),
                 title=title, border_style="blue")


async def live_dashboard(stats: Stats, stop: asyncio.Event, console: Console) -> None:
    with Live(render(stats), console=console, refresh_per_second=4) as live:
        while not stop.is_set():
            live.update(render(stats))
            await asyncio.sleep(0.25)
        live.update(render(stats))


def print_summary(stats: Stats, console: Console) -> None:
    """Final result table (validate mode lists every case that was not a clean pass)."""
    if stats.mode != "validate" or not stats.cases:
        return
    table = Table(title="Robustness findings", show_lines=False)
    for column in ("#", "Case", "Category", "Outcome", "Detail"):
        table.add_column(column)
    findings = [c for c in stats.cases if c.severity != PASS]
    for case in findings[:40]:
        table.add_row(str(case.index), case.name, case.category,
                      f"[{COLORS[case.severity]}]{case.outcome}[/]", case.detail)
    if findings:
        console.print(table)
        if len(findings) > 40:
            console.print(f"... and {len(findings) - 40} more (see the report)")
    else:
        console.print(f"[green]All {len(stats.cases)} cases were handled without disconnects or downtime.[/]")


def print_audit_summary(stats: Stats, console: Console) -> None:
    """Final posture table for audit mode (findings are printed live as they run)."""
    style = {"pass": "green", "warn": "yellow", "fail": "red"}
    table = Table(title="Security audit summary", show_lines=False)
    for column in ("Check", "Result", "Finding"):
        table.add_column(column)
    for finding in stats.findings:
        table.add_row(finding["title"],
                      f"[{style[finding['severity']]}]{finding['status']}[/]",
                      finding["detail"])
    console.print(table)
    fails = sum(f["severity"] == "fail" for f in stats.findings)
    warns = sum(f["severity"] == "warn" for f in stats.findings)
    if fails:
        console.print(f"[red]{fails} finding(s) need attention[/]"
                      + (f", {warns} to review." if warns else "."))
    elif warns:
        console.print(f"[yellow]{warns} finding(s) to review; nothing critical.[/]")
    else:
        console.print("[green]All checks passed.[/]")
