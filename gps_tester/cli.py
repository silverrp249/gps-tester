"""Command line interface: ``gps-tester``."""
from __future__ import annotations

import asyncio
import ipaddress
import signal
import socket
import sys
from pathlib import Path
from typing import Optional, Tuple

import click
from rich.console import Console

from gps_tester import __version__
from gps_tester.core.runner import RunConfig, run_simulation, run_validation
from gps_tester.core.stats import Stats
from gps_tester.protocols import CustomProtocol, available_protocols, get_protocol
from gps_tester.reporting import live_dashboard, print_audit_summary, print_summary, render, write_reports

console = Console()
PROTOCOL_NAMES = sorted(available_protocols()) + ["custom"]


def _is_local(host: str) -> bool:
    """True when every address of ``host`` is loopback / private / link-local."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise click.ClickException(f"Cannot resolve host {host!r}: {exc}") from exc
    addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    return all(a.is_loopback or a.is_private or a.is_link_local for a in addresses)


def _list_protocols(ctx: click.Context, _param, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    for name, cls in sorted(available_protocols().items()):
        click.echo(f"{name:<10} {cls.description}")
    click.echo(f"{'custom':<10} {CustomProtocol.description} (--config FILE)")
    ctx.exit()


async def _run(mode: str, cfg: RunConfig, protocol, stats: Stats, dashboard: bool,
               registered_imei=None) -> None:
    stop = asyncio.Event()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
    except NotImplementedError:      # Windows: Ctrl+C raises KeyboardInterrupt instead
        pass
    if mode == "audit":
        from gps_tester.core.audit import run_audit
        console.print("[bold]Running security audit[/] - each check connects to the server and "
                      "infers its behaviour from the outside.\n")
        await run_audit(cfg, protocol, stats, stop, registered_imei, report=_print_finding)
        return
    runner = run_simulation if mode == "simulate" else run_validation
    live = asyncio.create_task(live_dashboard(stats, stop, console)) if dashboard else None
    try:
        await runner(cfg, protocol, stats, stop)
    finally:
        stop.set()
        if live is not None:
            await live


FINDING_STYLE = {"pass": "green", "warn": "yellow", "fail": "red"}


def _print_finding(finding: dict) -> None:
    mark = {"pass": "[green]PASS[/]", "warn": "[yellow]WARN[/]", "fail": "[red]FAIL[/]"}[finding["severity"]]
    console.print(f"{mark}  [bold]{finding['title']}[/] - {finding['status']}")
    console.print(f"      {finding['detail']}")
    for line in finding["evidence"]:
        console.print(f"        [dim]- {line}[/]")
    console.print()


def _learn(samples: Tuple[str, ...], samples_file: Optional[str], output: str, provider: str,
           model: Optional[str], api_key: Optional[str], base_url: Optional[str]) -> None:
    import yaml

    from gps_tester.ai import infer_template

    items = list(samples)
    if samples_file:
        items += [line.strip() for line in Path(samples_file).read_text(encoding="utf-8").splitlines()
                  if line.strip()]
    if len(items) < 3:
        raise click.UsageError("learn mode needs at least 3 samples (--sample ... x3 or --samples-file)")
    console.print(f"Asking the [bold]{provider}[/] model to infer the protocol from {len(items)} samples...")
    try:
        config = infer_template(items, provider, model, api_key, base_url)
    except (ValueError, RuntimeError, KeyError) as exc:
        raise click.ClickException(f"Auto-parser failed: {exc}") from exc
    notes = config.pop("notes", "")
    Path(output).write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    console.print(f"[green]Template written to {output}[/]")
    if notes:
        console.print(f"[dim]Model notes: {notes}[/]")
    console.print("Review it, then run:  gps-tester --config "
                  f"{output} --host <host> --port <port> --mode validate")


@click.command(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110})
@click.option("--host", help="Server under test. [default: 127.0.0.1 or the value in --config]")
@click.option("--port", type=click.IntRange(1, 65535), help="Server port (or the value in --config).")
@click.option("--protocol", "protocol_name", type=click.Choice(PROTOCOL_NAMES, case_sensitive=False),
              default="gt06", show_default=True, help="Device protocol to speak.")
@click.option("--config", type=click.Path(exists=True, dir_okay=False),
              help="JSON/YAML template of a custom protocol (implies --protocol custom).")
@click.option("--mode", type=click.Choice(["simulate", "validate", "audit", "learn"]), default="simulate",
              show_default=True, help="simulate = load test, validate = robustness test, "
                                      "audit = security-posture check, learn = AI auto-parser.")
@click.option("--registered-imei",
              help="[audit] A real, enrolled device IMEI used as a baseline for the IMEI-validation "
                   "check. Without it the check can only flag that unknown devices are accepted.")
@click.option("--transport", type=click.Choice(["tcp", "udp"]), help="Override the protocol's transport.")
@click.option("--devices", type=click.IntRange(1), default=10, show_default=True,
              help="[simulate] Number of concurrent virtual devices.")
@click.option("--interval", type=click.FloatRange(min=0.01), default=5.0, show_default=True,
              help="[simulate] Seconds between position reports per device.")
@click.option("--duration", type=click.FloatRange(min=0), default=0, show_default=True,
              help="[simulate] Stop after this many seconds (0 = run until Ctrl+C).")
@click.option("--ramp-up", type=click.FloatRange(min=0), default=5.0, show_default=True,
              help="[simulate] Seconds over which the devices connect.")
@click.option("--count", type=click.IntRange(1),
              help="[validate] Number of test cases. Beyond the built-in catalogue, seeded random "
                   "mutations are added. [default: whole catalogue]")
@click.option("--timeout", type=click.FloatRange(min=0.1), default=5.0, show_default=True,
              help="Seconds to wait for a server reply.")
@click.option("--lat", type=float, default=24.7136, show_default=True, help="Route centre latitude.")
@click.option("--lon", type=float, default=46.6753, show_default=True, help="Route centre longitude.")
@click.option("--imei-base", default="86123400000000", show_default=True,
              help="First 14 IMEI digits; the device index is added and a Luhn digit appended.")
@click.option("--seed", type=int, help="Random seed for reproducible runs.")
@click.option("--report-dir", default="reports", show_default=True, type=click.Path(file_okay=False),
              help="Where the JSON and HTML reports are written.")
@click.option("--no-report", is_flag=True, help="Do not write report files.")
@click.option("--no-dashboard", is_flag=True, help="Disable the live dashboard (useful in CI).")
@click.option("--authorized", is_flag=True,
              help="Confirm you own or are authorised to test a non-local target.")
@click.option("--sample", "samples", multiple=True,
              help="[learn] A captured message; repeat 3+ times. Prefix binary data with 'hex:'.")
@click.option("--samples-file", type=click.Path(exists=True, dir_okay=False),
              help="[learn] File with one sample per line.")
@click.option("--output", default="learned_device.yaml", show_default=True,
              help="[learn] Where to write the generated template.")
@click.option("--llm-provider", type=click.Choice(["anthropic", "openai"]), default="anthropic",
              show_default=True, help="[learn] LLM API flavour ('openai' = any OpenAI-compatible endpoint).")
@click.option("--llm-model", help="[learn] Model name (env GPS_TESTER_LLM_MODEL).")
@click.option("--llm-api-key", help="[learn] API key (env GPS_TESTER_LLM_API_KEY).")
@click.option("--llm-base-url", help="[learn] API base URL (env GPS_TESTER_LLM_BASE_URL).")
@click.option("--list-protocols", is_flag=True, is_eager=True, expose_value=False,
              callback=_list_protocols, help="List supported protocols and exit.")
@click.version_option(__version__, prog_name="gps-tester")
def main(host, port, protocol_name, config, mode, registered_imei, transport, devices, interval,
         duration, ramp_up, count, timeout, lat, lon, imei_base, seed, report_dir, no_report,
         no_dashboard, authorized, samples, samples_file, output, llm_provider, llm_model,
         llm_api_key, llm_base_url) -> None:
    """GPS Telemetry Simulator & Protocol Validator.

    Load-test and robustness-test vehicle-tracking / IoT servers by simulating
    real devices (GT06, Teltonika, TK103, Meitrack or your own template).

    \b
    Examples:
      gps-tester --host 127.0.0.1 --port 5000 --protocol gt06 --mode simulate --devices 100 --interval 5
      gps-tester --host 127.0.0.1 --port 5000 --protocol teltonika --mode validate --count 1000
      gps-tester --config custom_device.yaml --mode validate

    Only test servers you own or have written permission to test.
    """
    if mode == "learn":
        _learn(samples, samples_file, output, llm_provider, llm_model, llm_api_key, llm_base_url)
        return

    # ---- protocol ---------------------------------------------------------
    try:
        if config:
            protocol = CustomProtocol.from_file(config)
            host = host or protocol.host
            port = port or protocol.port
        elif protocol_name.lower() == "custom":
            raise click.UsageError("--protocol custom needs --config FILE")
        else:
            protocol = get_protocol(protocol_name)
    except (ValueError, OSError) as exc:
        raise click.ClickException(f"Invalid protocol configuration: {exc}") from exc
    host = host or "127.0.0.1"
    if not port:
        raise click.UsageError("Missing --port (or a 'port' entry in the --config file)")
    if not imei_base.isdigit():
        raise click.UsageError("--imei-base must contain digits only")
    transport = transport or protocol.default_transport

    # ---- safety: this tool generates load and malformed traffic -----------
    if not _is_local(host) and not authorized:
        console.print(f"[yellow]{host} is not a local/private address.[/] Load and malformed-input "
                      "tests can disrupt a server; run them only against systems you own or are "
                      "explicitly authorised to test.")
        if not sys.stdin.isatty() or not click.confirm("I am authorised to test this server. Continue?"):
            raise click.ClickException("Aborted. Pass --authorized to confirm non-interactively.")

    cfg = RunConfig(host=host, port=port, transport=transport, devices=devices, interval=interval,
                    duration=duration, count=count, timeout=timeout, ramp_up=min(ramp_up, duration or ramp_up),
                    imei_base=imei_base, lat=lat, lon=lon, seed=seed)
    meta = {"protocol": getattr(protocol, "label", protocol.name), "host": host, "port": port,
            "transport": transport, "timeout": timeout, "seed": seed}
    if mode == "simulate":
        meta.update({"devices": devices, "interval": interval, "duration": duration, "ramp_up": cfg.ramp_up})
    elif mode == "audit":
        meta.update({"count": count, "registered_imei": registered_imei})
    else:
        meta["count"] = count
    stats = Stats(mode, meta)

    dashboard = not no_dashboard and console.is_terminal and mode != "audit"
    try:
        asyncio.run(_run(mode, cfg, protocol, stats, dashboard, registered_imei))
    except KeyboardInterrupt:
        pass
    stats.finish()

    if mode == "audit":
        print_audit_summary(stats, console)
    else:
        if not dashboard:
            console.print(render(stats))
        print_summary(stats, console)
    if not no_report:
        json_path, html_path = write_reports(stats, report_dir)
        console.print(f"Reports: [cyan]{json_path}[/]  [cyan]{html_path}[/]")

    worst = {f["severity"] for f in stats.findings}
    if stats.outcome_counts().get("unresponsive") or "fail" in worst:
        sys.exit(1)      # lets CI pipelines fail on a crash or a failed audit check


if __name__ == "__main__":
    main()
