"""Local HTTP proxy front end.

Supports the two shapes a browser sends to an HTTP proxy:

* ``CONNECT host:port`` -- an opaque TCP tunnel (HTTPS, WebSocket over TLS...).
* ``GET http://host/path`` -- absolute-form proxying for plain HTTP.  The
  request line is rewritten to origin-form, hop-by-hop headers are removed,
  and ``Connection: close`` is forced because the proxy handles exactly one
  request per client connection.

Each tunnel becomes one VLESS stream inside the shared HTTP/2 XHTTP session.

``h11`` is only the HTTP/1.1 parser for this client-facing proxy listener;
the connection to the XHTTP server is always HTTP/2 (``h2``/ALPN or h2c
prior knowledge) and never HTTP/1.1.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import h11

from .config import Config
from .h2client import H2Session, XhttpError
from .relay import relay

log = logging.getLogger("xhttp")

_MAX_HEAD = 64 * 1024

_HOP_BY_HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


@dataclass(slots=True)
class RequestHead:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]]


class ProtocolError(ValueError):
    pass


def parse_request_head(raw: bytes) -> RequestHead:
    """Parse an HTTP/1.x request head with ``h11``.

    ``h11`` validates the request line, header syntax and the mandatory
    ``Host`` header for HTTP/1.1.  Only the head is fed to it; the body and any
    bytes after it stay in the StreamReader and are relayed verbatim.
    """

    conn = h11.Connection(h11.SERVER, max_incomplete_event_size=_MAX_HEAD)
    try:
        conn.receive_data(raw)
        event = conn.next_event()
    except h11.RemoteProtocolError as exc:
        raise ProtocolError(str(exc)) from exc
    if not isinstance(event, h11.Request):
        raise ProtocolError("incomplete request head")
    try:
        return RequestHead(
            method=event.method.decode("ascii"),
            target=event.target.decode("latin-1"),
            version="HTTP/" + event.http_version.decode("ascii"),
            headers=[
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in event.headers
            ],
        )
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"invalid request encoding: {exc}") from exc


def format_authority(host: str, port: int, default_port: int) -> str:
    value = f"[{host}]" if ":" in host else host
    if port != default_port:
        value = f"{value}:{port}"
    return value


def _split_connect_target(target: str) -> tuple[str, int]:
    text = target.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ProtocolError(f"bad CONNECT target: {target!r}")
        host = text[1:end]
        rest = text[end + 1 :]
        if not rest:
            port = 443
        elif rest.startswith(":"):
            port = int(rest[1:])
        else:
            raise ProtocolError(f"bad CONNECT target: {target!r}")
    else:
        if text.count(":") == 1:
            host, port_text = text.split(":", 1)
            port = int(port_text)
        elif ":" not in text:
            host, port = text, 443
        else:
            host, port = text, 443  # bare IPv6 literal
    if not host or not 0 < port < 65536:
        raise ProtocolError(f"bad CONNECT target: {target!r}")
    return host, port


def _rewrite_request(req: RequestHead, path: str, authority: str) -> bytes:
    """Build the origin-form request for the destination, using ``h11``."""

    connection_tokens: set[str] = set()
    for name, value in req.headers:
        if name.lower() == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
    skip = _HOP_BY_HOP | connection_tokens

    headers: list[tuple[bytes, bytes]] = []
    has_host = False
    for name, value in req.headers:
        lower = name.lower()
        if lower == "host":
            has_host = True
        if lower in skip:
            continue
        try:
            headers.append(
                (name.encode("latin-1"), value.encode("latin-1"))
            )
        except UnicodeEncodeError as exc:
            raise ProtocolError(f"header cannot be encoded: {name}") from exc
    if not has_host:
        headers.append((b"host", authority.encode("latin-1")))
    # One request per client connection: the target must delimit the body by
    # closing, which lets the relay know when the downlink is complete.
    headers.append((b"connection", b"close"))

    conn = h11.Connection(h11.CLIENT, max_incomplete_event_size=_MAX_HEAD)
    try:
        data = conn.send(
            h11.Request(
                method=req.method.encode("ascii"),
                target=path.encode("latin-1"),
                headers=headers,
                # h11 only serializes HTTP/1.1; upgrading a 1.0 proxy request
                # is safe and the forced Connection: close keeps semantics.
                http_version=b"1.1",
            )
        )
    except (h11.LocalProtocolError, UnicodeEncodeError) as exc:
        raise ProtocolError(str(exc)) from exc
    if data is None:
        raise ProtocolError("could not serialize the outgoing request")
    return data


class HttpProxyServer:
    def __init__(self, cfg: Config, session: H2Session):
        self.cfg = cfg
        self.session = session
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task[None]] = set()

    async def serve(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client,
            self.cfg.listen_host,
            self.cfg.listen_port,
            limit=_MAX_HEAD,
        )
        sockets = self._server.sockets or []
        for sock in sockets:
            log.info("HTTP proxy listening on %s", sock.getsockname())
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        server = self._server
        self._server = None
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

    # ---- client handling ------------------------------------------------

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        try:
            await self._handle_client(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("client handler failed")
        finally:
            if task is not None:
                self._clients.discard(task)
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), self.cfg.response_timeout
            )
        except asyncio.LimitOverrunError:
            await self._send_error(writer, 431, "Request Header Fields Too Large")
            return
        except asyncio.IncompleteReadError:
            return
        except asyncio.TimeoutError:
            log.debug("%s: timed out reading the request head", peer)
            return

        try:
            req = parse_request_head(raw)
        except ProtocolError as exc:
            log.debug("%s: bad request: %s", peer, exc)
            await self._send_error(writer, 400, "Bad Request", str(exc))
            return

        log.debug("%s %s %s", peer, req.method, req.target)
        if req.method.upper() == "CONNECT":
            await self._handle_connect(reader, writer, req)
        else:
            await self._handle_forward(reader, writer, req)

    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        req: RequestHead,
    ) -> None:
        try:
            host, port = _split_connect_target(req.target)
        except (ProtocolError, ValueError) as exc:
            await self._send_error(writer, 400, "Bad Request", str(exc))
            return

        try:
            stream = await self.session.open_stream(host, port)
        except (XhttpError, OSError) as exc:
            log.warning("CONNECT %s:%s failed: %s", host, port, exc)
            await self._send_error(writer, 502, "Bad Gateway", str(exc))
            return

        try:
            writer.write(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: xhttp\r\n\r\n"
            )
            await writer.drain()
        except (OSError, ConnectionError):
            await stream.abort()
            return

        await relay(reader, writer, stream, self.cfg.idle_timeout)

    async def _handle_forward(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        req: RequestHead,
    ) -> None:
        try:
            parts = urlsplit(req.target)
        except ValueError as exc:
            await self._send_error(writer, 400, "Bad Request", str(exc))
            return

        scheme = parts.scheme.lower()
        if scheme != "http" or not parts.hostname:
            await self._send_error(
                writer,
                400,
                "Bad Request",
                "the proxy accepts absolute-form http:// URLs; use CONNECT for https://",
            )
            return
        try:
            port = parts.port or 80
        except ValueError as exc:
            await self._send_error(writer, 400, "Bad Request", str(exc))
            return

        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        authority = format_authority(parts.hostname, port, 80)
        try:
            head = _rewrite_request(req, path, authority)
        except ProtocolError as exc:
            await self._send_error(writer, 400, "Bad Request", str(exc))
            return

        try:
            stream = await self.session.open_stream(parts.hostname, port, initial=head)
        except (XhttpError, OSError) as exc:
            log.warning("GET %s://%s failed: %s", scheme, authority, exc)
            await self._send_error(writer, 502, "Bad Gateway", str(exc))
            return

        await relay(reader, writer, stream, self.cfg.idle_timeout)

    # ---- responses ------------------------------------------------------

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        detail: str = "",
    ) -> None:
        body = ((detail or reason) + "\n").encode("utf-8", "replace")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "Proxy-Agent: xhttp\r\n\r\n"
        ).encode("latin-1")
        try:
            writer.write(head + body)
            await writer.drain()
        except (OSError, ConnectionError):
            pass
