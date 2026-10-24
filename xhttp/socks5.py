"""Local SOCKS5 inbound (RFC 1928), built on the ``asyncio-socks5`` library.

The library implements the SOCKS5 greeting, method negotiation, request
parsing, replies and the TCP relay.  We only supply a ``ConnectSession`` whose
``connect_remote`` opens a VLESS/XHTTP stream instead of a direct TCP socket,
and a small ``Socket`` adapter around :class:`XhttpStream`.

A domain name in the SOCKS5 request is passed through to the VLESS header, so
it is resolved by the XHTTP server ("SOCKS5H" / remote DNS).  Authentication is
no-auth; BIND and UDP ASSOCIATE are rejected with COMMAND_NOT_SUPPORTED.
"""

from __future__ import annotations

import asyncio
import logging

from socks5.server import Socks5
from socks5.server.exceptions import AuthenticationError, Socks5Error
from socks5.server.sessions import BaseSession, ConnectSession
from socks5.types import Socket
from socks5.utils import TCPSocket
from socks5.values import Command

from .config import Config
from .h2client import H2Session, XhttpError, XhttpStream

log = logging.getLogger("xhttp")


class _XhttpSocket(Socket):
    """``socks5.types.Socket`` over one VLESS byte stream."""

    def __init__(self, stream: XhttpStream, idle_timeout: float):
        self._stream = stream
        self._idle_timeout = idle_timeout
        self._closed = False

    async def recv(self, num: int) -> bytes:
        try:
            data = await self._stream.read(self._idle_timeout)
        except XhttpError as exc:
            # The library's relay only catches OSError; convert so a reset
            # stream ends this SOCKS5 session quietly.
            raise ConnectionResetError(str(exc)) from exc
        return data if data is not None else b""

    async def send(self, data: bytes) -> int:
        try:
            await self._stream.write(data)
        except XhttpError as exc:
            raise ConnectionResetError(str(exc)) from exc
        return len(data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stream.aclose()
        except XhttpError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed


class _ExactSocket(TCPSocket):
    """``TCPSocket`` that uses ``readexactly`` during the SOCKS5 handshake.

    ``asyncio-socks5`` reads fixed-size fields with ``StreamReader.read(n)``,
    which may return fewer than ``n`` bytes when a client writes the greeting
    in several TCP segments.  Exact reads make the handshake fragmentation
    proof; the flag is cleared before the relay starts, where partial reads
    are the normal behaviour.
    """

    exact = True

    async def recv(self, num: int) -> bytes:
        if self.exact:
            try:
                return await self.r.readexactly(num)
            except asyncio.IncompleteReadError as exc:
                return exc.partial
        return await super().recv(num)


def _make_connect_session(
    session: H2Session, idle_timeout: float
) -> type[ConnectSession]:
    class _XhttpConnectSession(ConnectSession):
        async def connect_remote(self, host: str, port: int) -> Socket:
            try:
                stream = await session.open_stream(host, port)
            except XhttpError as exc:
                # ConnectSession maps OSError to GENERAL_SOCKS_SERVER_FAILURE;
                # letting a RuntimeError escape would print a traceback.
                log.warning("SOCKS5 CONNECT %s:%s failed: %s", host, port, exc)
                raise OSError(str(exc)) from exc
            return _XhttpSocket(stream, idle_timeout)

    return _XhttpConnectSession


class _LibrarySocks5(Socks5):
    """``Socks5`` with an exact-read handshake socket and task tracking."""

    def __init__(self, host: str, port: int, session_class: type[ConnectSession]):
        super().__init__(
            host,
            port,
            connect_session_class=session_class,
            udp_session_class=BaseSession,
        )
        self._clients: set[asyncio.Task[None]] = set()

    async def start_server(self) -> None:
        async def link(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            task = asyncio.current_task()
            if task is not None:
                self._clients.add(task)
            sock = _ExactSocket(reader, writer)
            try:
                await self._link(sock)
            finally:
                if task is not None:
                    self._clients.discard(task)
                await sock.close()

        self.server = await asyncio.start_server(link, self.host, self.port)
        for sock in self.server.sockets or []:
            log.info("SOCKS5 proxy listening on %s", sock.getsockname())

    async def _link(self, sock: _ExactSocket) -> None:
        try:
            command, host, port = await self.shake_hand(sock)
            sock.exact = False
            if command == Command.CONNECT:
                await self.connect_session(sock, host, port)
            elif command == Command.UDP_ASSOCIATE:
                await self.udp_session(sock, host, port)
            elif command == Command.BIND:
                await self.bind_session(sock, host, port)
        except AuthenticationError as exc:
            log.warning("SOCKS5 authentication failed: %s", exc)
        except (Socks5Error, ConnectionError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("SOCKS5 session failed")
        finally:
            await sock.close()

    async def stop(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except OSError:
                pass
        tasks = list(self._clients)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class Socks5Server:
    """Owns the local SOCKS5 listener and its library instance."""

    def __init__(self, cfg: Config, session: H2Session):
        endpoint = cfg.socks_endpoint()
        if endpoint is None:
            raise ValueError("SOCKS5 listener is disabled")
        host, port = endpoint
        self._server = _LibrarySocks5(
            host, port, _make_connect_session(session, cfg.idle_timeout)
        )

    async def serve(self) -> None:
        await self._server.start_server()
        try:
            await asyncio.Event().wait()
        finally:
            await self._server.stop()

    async def close(self) -> None:
        await self._server.stop()
