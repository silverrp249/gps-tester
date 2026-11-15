"""Thin asyncio TCP/UDP client abstraction."""
from __future__ import annotations

import asyncio
from typing import Optional


class ConnectionClosed(ConnectionError):
    """The remote peer closed (or refused) the connection."""


class _UdpClient(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    def datagram_received(self, data, addr):  # noqa: D401
        self.queue.put_nowait(data)

    def error_received(self, exc):
        self.queue.put_nowait(exc)


class Connection:
    """One client connection to the server under test.

    ``recv`` returns ``None`` on timeout and raises :class:`ConnectionClosed`
    (an ``OSError``) when the peer closed the connection.
    """

    def __init__(self, host: str, port: int, transport: str = "tcp",
                 connect_timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.transport = transport.lower()
        self.connect_timeout = connect_timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._udp_transport: Optional[asyncio.DatagramTransport] = None
        self._udp: Optional[_UdpClient] = None

    @property
    def is_open(self) -> bool:
        return self._writer is not None or self._udp_transport is not None

    async def open(self) -> None:
        if self.transport == "tcp":
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.connect_timeout)
        elif self.transport == "udp":
            loop = asyncio.get_running_loop()
            self._udp_transport, self._udp = await asyncio.wait_for(
                loop.create_datagram_endpoint(_UdpClient, remote_addr=(self.host, self.port)),
                self.connect_timeout)
        else:
            raise ValueError(f"Unsupported transport: {self.transport!r}")

    async def send(self, data: bytes) -> None:
        if self._writer is not None:
            self._writer.write(data)
            await self._writer.drain()
        elif self._udp_transport is not None:
            self._udp_transport.sendto(data)
        else:
            raise ConnectionClosed("connection is not open")

    async def recv(self, timeout: float) -> Optional[bytes]:
        try:
            if self._reader is not None:
                data = await asyncio.wait_for(self._reader.read(4096), timeout)
                if not data:
                    raise ConnectionClosed("connection closed by server")
                return data
            if self._udp is not None:
                item = await asyncio.wait_for(self._udp.queue.get(), timeout)
                if isinstance(item, Exception):
                    raise ConnectionClosed(str(item) or item.__class__.__name__)
                return item
        except asyncio.TimeoutError:
            return None
        raise ConnectionClosed("connection is not open")

    def abort(self) -> None:
        """Drop the connection abruptly (TCP RST) without a clean shutdown."""
        if self._writer is not None:
            self._writer.transport.abort()
            self._writer = None
            self._reader = None
        if self._udp_transport is not None:
            self._udp_transport.abort()
            self._udp_transport = None

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except Exception:  # noqa: BLE001 - closing must never fail a test
                pass
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
