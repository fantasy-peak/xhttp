"""Local HTTP proxy front end.

Supports the two shapes a browser sends to an HTTP proxy:

* ``CONNECT host:port`` -- an opaque TCP tunnel (HTTPS, WebSocket over TLS...).
* ``GET http://host/path`` -- absolute-form proxying for plain HTTP.  The
  request line is rewritten to origin-form, hop-by-hop headers are removed,
  and ``Connection: close`` is forced because the proxy handles exactly one
  request per client connection.

Each tunnel becomes one VLESS stream inside the shared HTTP/2 XHTTP session.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Config
from .h2client import H2Session, XhttpError, XhttpStream

log = logging.getLogger("xhttp")

_MAX_HEAD = 64 * 1024
_MAX_HEADER_LINES = 200

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
    if not raw.endswith(b"\r\n\r\n"):
        raise ProtocolError("incomplete request head")
    lines = raw[:-4].decode("latin-1").split("\r\n")
    if not lines or not lines[0]:
        raise ProtocolError("empty request line")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise ProtocolError(f"malformed request line: {lines[0]!r}")
    method, target, version = parts
    if not method or not target or not version.startswith("HTTP/"):
        raise ProtocolError(f"malformed request line: {lines[0]!r}")
    if len(lines) - 1 > _MAX_HEADER_LINES:
        raise ProtocolError("too many headers")

    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[0] in " \t":
            raise ProtocolError("folded header lines are not accepted")
        name, sep, value = line.partition(":")
        if not sep or not name or any(c.isspace() for c in name):
            raise ProtocolError(f"malformed header: {line!r}")
        headers.append((name, value.strip()))
    return RequestHead(method=method, target=target, version=version, headers=headers)


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
    connection_tokens: set[str] = set()
    for name, value in req.headers:
        if name.lower() == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
    skip = _HOP_BY_HOP | connection_tokens

    out = [f"{req.method} {path} {req.version}"]
    has_host = False
    for name, value in req.headers:
        lower = name.lower()
        if lower == "host":
            has_host = True
        if lower in skip:
            continue
        out.append(f"{name}: {value}")
    if not has_host:
        out.append(f"Host: {authority}")
    # One request per client connection: the target must delimit the body by
    # closing, which lets the relay know when the downlink is complete.
    out.append("Connection: close")
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")


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

        await self._relay(reader, writer, stream)

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
        head = _rewrite_request(req, path, authority)

        try:
            stream = await self.session.open_stream(parts.hostname, port, initial=head)
        except (XhttpError, OSError) as exc:
            log.warning("GET %s://%s failed: %s", scheme, authority, exc)
            await self._send_error(writer, 502, "Bad Gateway", str(exc))
            return

        await self._relay(reader, writer, stream)

    # ---- byte relay -----------------------------------------------------

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        stream: XhttpStream,
    ) -> None:
        up = asyncio.create_task(self._pump_up(reader, stream), name="xhttp-up")
        down = asyncio.create_task(
            self._pump_down(writer, stream), name="xhttp-down"
        )
        try:
            done, _ = await asyncio.wait(
                {up, down}, return_when=asyncio.FIRST_COMPLETED
            )
            if down in done:
                # The response/target side finished; the client has nothing
                # left to wait for.
                if not up.done():
                    up.cancel()
            else:
                # The client half-closed (or failed) first.
                error = None
                if not up.cancelled():
                    error = up.exception()
                if error is None:
                    try:
                        await down
                    except (XhttpError, OSError, ConnectionError):
                        pass
                elif not down.done():
                    down.cancel()
        finally:
            for task in (up, down):
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(up, down, return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, (ConnectionError, BrokenPipeError, OSError)):
                    log.debug("relay ended: %s", result)
                elif isinstance(result, Exception):
                    log.debug("relay error: %s", result)
            await stream.aclose()

    async def _pump_up(
        self, reader: asyncio.StreamReader, stream: XhttpStream
    ) -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                await stream.write_eof()
                return
            await stream.write(data)

    async def _pump_down(
        self, writer: asyncio.StreamWriter, stream: XhttpStream
    ) -> None:
        while True:
            data = await stream.read(self.cfg.idle_timeout)
            if data is None:
                try:
                    if writer.can_write_eof():
                        writer.write_eof()
                except (OSError, ConnectionError):
                    pass
                return
            writer.write(data)
            await writer.drain()

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
