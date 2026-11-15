"""A tiny TCP server that speaks just enough of each protocol to acknowledge
frames. Use it to try the tool locally - it is not a real tracking server."""
from __future__ import annotations

import asyncio

import click

from gps_tester.protocols import CustomProtocol, available_protocols, get_protocol


async def serve(host: str, port: int, protocol) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                reply = protocol.server_ack(data)
                if reply:
                    writer.write(reply)
                    await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    click.echo(f"Mock {protocol.name} server listening on {host}:{port} (Ctrl+C to stop)")
    async with server:
        await server.serve_forever()


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", type=int, default=5000, show_default=True)
@click.option("--protocol", "protocol_name", default="gt06", show_default=True,
              type=click.Choice(sorted(available_protocols())))
@click.option("--config", type=click.Path(exists=True, dir_okay=False),
              help="Custom protocol template (uses its mock_match/mock_reply entries).")
def main(host: str, port: int, protocol_name: str, config: str) -> None:
    """Local mock tracking server for trying gps-tester."""
    protocol = CustomProtocol.from_file(config) if config else get_protocol(protocol_name)
    try:
        asyncio.run(serve(host, port, protocol))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
