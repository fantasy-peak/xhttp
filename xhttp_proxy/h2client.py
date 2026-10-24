"""HTTP/2 + VLESS ("XHTTP stream-one") client.

One HTTP/2 connection is shared by every proxied local connection.  Each
local connection becomes one POST stream:

* request body  -> uplink   (VLESS request header first)
* response body -> downlink (VLESS response header first)

The server may be Xray-core or the C++ v2ray-cpp server.  Only HTTP/2
(TLS + ALPN ``h2``, or cleartext prior knowledge) is implemented; XHTTP over
HTTP/3 would need a QUIC stack.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import socket
import ssl
from dataclasses import dataclass, field

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.errors import ErrorCodes
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    InformationalResponseReceived,
    PushedStreamReceived,
    RemoteSettingsChanged,
    ResponseReceived,
    SettingsAcknowledged,
    StreamEnded,
    StreamReset,
    TrailersReceived,
    WindowUpdated,
)
from h2.exceptions import H2Error, StreamClosedError, TooManyStreamsError
from h2.settings import SettingCodes

from .config import Config
from .vless import VlessError, parse_response_header, request_header

log = logging.getLogger("xhttp")

# We advertise a 1 MiB receive window so a single stream does not stall on
# the HTTP/2 default of 64 KiB.  The application still acks only after data
# has been queued for the local socket, so memory stays bounded.
_RECV_WINDOW = 1 << 20
# Per-stream queue: at most 64 DATA frames (roughly 1 MiB) buffered.
_QUEUE_CHUNKS = 64
# Cap on concurrent streams we will open ourselves, independent of the
# server's SETTINGS_MAX_CONCURRENT_STREAMS.
_MAX_LOCAL_STREAMS = 256


class XhttpError(RuntimeError):
    """The XHTTP/VLESS session failed."""


@dataclass(slots=True)
class _StreamState:
    sid: int
    queue: asyncio.Queue[bytes | None]
    headers_ready: asyncio.Event
    pending: bytearray = field(default_factory=bytearray)
    status: int | None = None
    error: BaseException | None = None
    remote_ended: bool = False
    local_ended: bool = False
    aborted: bool = False


class XhttpStream:
    """One VLESS byte pipe (a single HTTP/2 stream)."""

    def __init__(self, session: "H2Session", state: _StreamState):
        self._session = session
        self._state = state

    async def read(self, timeout: float = 0.0) -> bytes | None:
        """Next downlink bytes, or ``None`` on a clean EOF."""

        state = self._state
        if state.pending:
            data = bytes(state.pending)
            state.pending.clear()
            return data
        if state.error is not None:
            raise XhttpError(str(state.error)) from state.error
        if state.remote_ended and state.queue.empty():
            return None

        try:
            if timeout and timeout > 0:
                item = await asyncio.wait_for(state.queue.get(), timeout)
            else:
                item = await state.queue.get()
        except asyncio.TimeoutError:
            raise XhttpError(f"downlink idle for {timeout:.0f}s") from None

        if item is None:
            if state.error is not None:
                raise XhttpError(str(state.error)) from state.error
            return None
        return item

    async def write(self, data: bytes) -> None:
        await self._session._send_data(self._state, bytes(data))

    async def write_eof(self) -> None:
        """Half-close the uplink (HTTP/2 END_STREAM)."""

        await self._session._end_stream(self._state)

    async def abort(self) -> None:
        await self._session._close_stream(self._state, reset=True)

    async def aclose(self) -> None:
        st = self._state
        await self._session._close_stream(st, reset=not st.remote_ended)


def _make_ssl_context(cfg: Config) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(["h2"])
    if cfg.ca:
        ctx.load_verify_locations(cfg.ca)
    if cfg.insecure or (cfg.pin_sha256 and not cfg.ca):
        # Pinning replaces the PKI check unless a CA was supplied explicitly.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class H2Session:
    """Shared HTTP/2 connection to the XHTTP endpoint."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._uuid = cfg.uuid_bytes
        self._conn: H2Connection | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._cond = asyncio.Condition()
        self._connect_lock = asyncio.Lock()
        self._streams: dict[int, _StreamState] = {}
        self._failure: BaseException | None = None

    # ---- connection -----------------------------------------------------

    async def ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._conn is not None:
                return
            await self._connect()

    async def _connect(self) -> None:
        cfg = self.cfg
        ctx = _make_ssl_context(cfg) if cfg.tls else None
        try:
            upstream = cfg.upstream_endpoint()
            if upstream is not None:
                reader, writer = await self._open_via_proxy(ctx, *upstream)
            else:
                coro = asyncio.open_connection(
                    cfg.server,
                    cfg.port,
                    ssl=ctx,
                    server_hostname=cfg.server_hostname if ctx else None,
                    limit=256 * 1024,
                )
                reader, writer = await asyncio.wait_for(coro, cfg.connect_timeout)
        except asyncio.TimeoutError as exc:
            raise XhttpError(
                f"connect to {cfg.server}:{cfg.port} timed out after {cfg.connect_timeout:g}s"
            ) from exc
        except (OSError, ssl.SSLError) as exc:
            raise XhttpError(f"connect to {cfg.server}:{cfg.port} failed: {exc}") from exc

        try:
            self._check_tls(writer, ctx)
            sock = writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass

            conn = H2Connection(
                config=H2Configuration(client_side=True, header_encoding=None)
            )
            conn.initiate_connection()
            conn.update_settings(
                {
                    SettingCodes.INITIAL_WINDOW_SIZE: _RECV_WINDOW,
                    SettingCodes.ENABLE_PUSH: 0,
                }
            )
            writer.write(conn.data_to_send())
            await writer.drain()
        except BaseException:
            writer.close()
            raise

        self._conn = conn
        self._reader = reader
        self._writer = writer
        self._failure = None
        self._reader_task = asyncio.create_task(
            self._reader_loop(conn, reader, writer), name="xhttp-h2-reader"
        )
        log.info(
            "connected to %s:%s (%s, ALPN %s)",
            cfg.server,
            cfg.port,
            "TLS" if cfg.tls else "h2c",
            self._alpn(writer) if cfg.tls else "none",
        )

    async def _open_via_proxy(
        self, ctx: ssl.SSLContext | None, proxy_host: str, proxy_port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        cfg = self.cfg
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(proxy_host, proxy_port, limit=256 * 1024),
                cfg.connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise XhttpError(
                f"connect to upstream proxy {proxy_host}:{proxy_port} failed: {exc}"
            ) from exc

        # The CONNECT target is the real server address, not the optional
        # Host-header override (which may be a CDN name with no A record here).
        target = cfg.server_authority
        try:
            writer.write(
                (
                    f"CONNECT {target} HTTP/1.1\r\n"
                    f"Host: {target}\r\n"
                    "Proxy-Connection: keep-alive\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), cfg.connect_timeout
            )
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            writer.close()
            raise XhttpError(f"upstream proxy CONNECT failed: {exc}") from exc

        line = head.split(b"\r\n", 1)[0]
        parts = line.split(b" ")
        if len(parts) < 2 or parts[1] != b"200":
            writer.close()
            raise XhttpError(
                f"upstream proxy refused CONNECT {target}: "
                f"{line.decode('latin-1', 'replace')}"
            )

        if ctx is not None:
            try:
                await writer.start_tls(
                    ctx,
                    server_hostname=cfg.server_hostname,
                    ssl_handshake_timeout=cfg.connect_timeout,
                )
            except (OSError, ssl.SSLError, asyncio.TimeoutError) as exc:
                writer.close()
                raise XhttpError(f"TLS handshake through upstream proxy failed: {exc}") from exc
        return reader, writer

    def _check_tls(
        self, writer: asyncio.StreamWriter, ctx: ssl.SSLContext | None
    ) -> None:
        if ctx is None:
            return
        sslobj = writer.get_extra_info("ssl_object")
        if sslobj is None:
            raise XhttpError("TLS was requested but no SSL object is attached")
        proto = sslobj.selected_alpn_protocol()
        if proto != "h2":
            raise XhttpError(
                f"server selected ALPN {proto!r}; XHTTP stream-one needs 'h2'"
            )
        pin = self.cfg.pin_sha256
        if pin:
            der = sslobj.getpeercert(binary_form=True)
            if not der:
                raise XhttpError("server presented no certificate to pin")
            actual = hashlib.sha256(der).hexdigest()
            if not hmac.compare_digest(actual, pin):
                raise XhttpError(
                    f"certificate pin mismatch: got {actual}, expected {pin}"
                )

    @staticmethod
    def _alpn(writer: asyncio.StreamWriter) -> str:
        sslobj = writer.get_extra_info("ssl_object")
        if sslobj is None:
            return "-"
        return sslobj.selected_alpn_protocol() or "-"

    async def close(self) -> None:
        task = self._reader_task
        writer = self._writer
        if task is not None:
            task.cancel()
        if writer is not None:
            writer.close()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    # ---- reader / events ------------------------------------------------

    async def _reader_loop(
        self,
        conn: H2Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        error: BaseException | None = None
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    error = XhttpError("server closed the HTTP/2 connection")
                    break

                out = b""
                async with self._cond:
                    if self._conn is not conn:
                        return
                    events = conn.receive_data(data)
                    out = conn.data_to_send()
                    if out:
                        writer.write(out)
                if out:
                    await writer.drain()

                for event in events:
                    await self._handle_event(conn, event)

                async with self._cond:
                    self._cond.notify_all()
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError, H2Error) as exc:
            error = exc
        except Exception as exc:  # a bug in an event handler must still tear down
            log.exception("HTTP/2 reader failed")
            error = exc
        finally:
            await self._connection_lost(conn, error)

    async def _handle_event(self, conn: H2Connection, event: object) -> None:
        if isinstance(event, ResponseReceived):
            st = self._streams.get(event.stream_id)
            if st is None:
                return
            headers = {name: value for name, value in event.headers}
            raw = headers.get(b":status")
            try:
                st.status = int(raw.decode("ascii")) if raw is not None else None
            except (TypeError, ValueError, UnicodeDecodeError):
                st.status = None
            st.headers_ready.set()
            return

        if isinstance(event, DataReceived):
            st = self._streams.get(event.stream_id)
            if st is not None and not st.aborted:
                # The queue is the backpressure point: while it is full the
                # reader stops reading the socket instead of growing memory.
                await st.queue.put(event.data)
            # Ack even for a stream we just forgot (a reset one): the bytes
            # still consumed connection-level flow-control credit.
            await self._ack(event.stream_id, event.flow_controlled_length)
            return

        if isinstance(event, StreamEnded):
            st = self._streams.get(event.stream_id)
            if st is None:
                return
            st.remote_ended = True
            st.headers_ready.set()
            try:
                st.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            async with self._cond:
                self._streams.pop(event.stream_id, None)
                self._cond.notify_all()
            return

        if isinstance(event, StreamReset):
            st = self._streams.get(event.stream_id)
            if st is None:
                return
            st.error = XhttpError(
                f"server reset stream {event.stream_id} (error {event.error_code})"
            )
            st.remote_ended = True
            st.headers_ready.set()
            try:
                st.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            async with self._cond:
                self._streams.pop(event.stream_id, None)
                self._cond.notify_all()
            return

        if isinstance(event, ConnectionTerminated):
            self._failure = XhttpError(
                f"HTTP/2 GOAWAY (last stream {event.last_stream_id}, "
                f"error {event.error_code})"
            )
            return

        if isinstance(event, PushedStreamReceived):
            try:
                async with self._cond:
                    conn.reset_stream(event.pushed_stream_id, ErrorCodes.REFUSED_STREAM)
                    out = conn.data_to_send()
                    if out and self._writer is not None:
                        self._writer.write(out)
            except (StreamClosedError, H2Error):
                pass
            return

        if isinstance(event, (InformationalResponseReceived, TrailersReceived,
                              SettingsAcknowledged)):
            return
        if isinstance(event, (WindowUpdated, RemoteSettingsChanged)):
            return

    async def _ack(self, stream_id: int, length: int) -> None:
        out = b""
        async with self._cond:
            conn = self._conn
            if conn is None:
                return
            try:
                conn.acknowledge_received_data(length, stream_id)
            except (StreamClosedError, H2Error):
                return
            out = conn.data_to_send()
            if out and self._writer is not None:
                self._writer.write(out)
            self._cond.notify_all()
        if out and self._writer is not None:
            await self._writer.drain()

    async def _connection_lost(
        self, conn: H2Connection, error: BaseException | None
    ) -> None:
        failure = self._failure or error or XhttpError("connection lost")
        self._failure = failure
        writer: asyncio.StreamWriter | None = None

        async with self._cond:
            if self._conn is not conn:
                return
            self._conn = None
            writer = self._writer
            self._writer = None
            for st in list(self._streams.values()):
                if st.error is None:
                    st.error = failure
                st.remote_ended = True
                st.headers_ready.set()
                try:
                    st.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            self._streams.clear()
            self._cond.notify_all()

        log.warning("HTTP/2 connection lost: %s", failure)
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    # ---- streams --------------------------------------------------------

    async def open_stream(
        self, host: str, port: int, *, initial: bytes = b""
    ) -> XhttpStream:
        cfg = self.cfg
        try:
            header = request_header(self._uuid, host, port) + initial
        except VlessError as exc:
            raise XhttpError(str(exc)) from exc

        await self.ensure_connected()

        st: _StreamState | None = None
        while st is None:
            async with self._cond:
                conn = self._conn
                if conn is None:
                    raise XhttpError(str(self._failure or "not connected"))
                limit = min(
                    int(conn.remote_settings.max_concurrent_streams),
                    _MAX_LOCAL_STREAMS,
                )
                if len(self._streams) >= limit:
                    await self._cond.wait()
                    continue
                sid = conn.get_next_available_stream_id()
                st = _StreamState(
                    sid=sid,
                    queue=asyncio.Queue(maxsize=_QUEUE_CHUNKS),
                    headers_ready=asyncio.Event(),
                )
                headers = [
                    (b":method", b"POST"),
                    (b":scheme", cfg.scheme.encode("ascii")),
                    (b":authority", cfg.authority.encode("ascii")),
                    (b":path", cfg.path.encode("utf-8")),
                    (b"content-type", b"application/octet-stream"),
                    (b"accept", b"*/*"),
                    (b"user-agent", cfg.user_agent.encode("ascii", "replace")),
                ]
                if cfg.host:
                    headers.append((b"host", cfg.host.encode("ascii")))
                try:
                    conn.send_headers(sid, headers, end_stream=False)
                except TooManyStreamsError:
                    st = None
                    await self._cond.wait()
                    continue
                self._streams[sid] = st
                out = conn.data_to_send()
                if out and self._writer is not None:
                    self._writer.write(out)
            if out and self._writer is not None:
                await self._writer.drain()

        assert st is not None
        try:
            # The VLESS header is the first thing the server can act on; send
            # it before waiting for the response, otherwise both sides wait.
            await self._send_data(st, header)
            await asyncio.wait_for(st.headers_ready.wait(), cfg.response_timeout)
            if st.error is not None:
                raise XhttpError(str(st.error)) from st.error
            if st.status != 200:
                raise XhttpError(
                    f"XHTTP endpoint returned HTTP {st.status} "
                    f"(check path {cfg.path!r}, UUID, and transport)"
                )

            prefix = await self._recv_exact(st, 2, cfg.response_timeout)
            _version, addon_len = parse_response_header(prefix)
            if addon_len:
                await self._recv_exact(st, addon_len, cfg.response_timeout)
        except BaseException:
            await self._close_stream(st, reset=True)
            raise

        return XhttpStream(self, st)

    async def _recv_exact(
        self, st: _StreamState, size: int, timeout: float
    ) -> bytes:
        while len(st.pending) < size:
            if st.error is not None:
                raise XhttpError(str(st.error)) from st.error
            if st.remote_ended and st.queue.empty():
                raise XhttpError("stream ended before the VLESS response header")
            try:
                item = await asyncio.wait_for(st.queue.get(), timeout)
            except asyncio.TimeoutError:
                raise XhttpError("timed out waiting for the VLESS response header") from None
            if item is None:
                if st.error is not None:
                    raise XhttpError(str(st.error)) from st.error
                raise XhttpError("stream ended before the VLESS response header")
            st.pending.extend(item)
        result = bytes(st.pending[:size])
        del st.pending[:size]
        return result

    async def _send_data(self, st: _StreamState, data: bytes) -> None:
        offset = 0
        total = len(data)
        while offset < total:
            async with self._cond:
                conn = self._conn
                if conn is None:
                    raise XhttpError(str(self._failure or "connection closed"))
                if st.error is not None:
                    raise XhttpError(str(st.error)) from st.error
                if st.aborted or st.local_ended:
                    raise XhttpError("write after the stream was closed")
                while True:
                    try:
                        window = conn.local_flow_control_window(st.sid)
                    except StreamClosedError as exc:
                        raise XhttpError("stream closed by the server") from exc
                    if window > 0:
                        break
                    await self._cond.wait()
                    conn = self._conn
                    if conn is None:
                        raise XhttpError(str(self._failure or "connection closed"))
                chunk_size = min(total - offset, window, conn.max_outbound_frame_size)
                conn.send_data(st.sid, data[offset : offset + chunk_size])
                offset += chunk_size
                out = conn.data_to_send()
                if out and self._writer is not None:
                    self._writer.write(out)
            if out and self._writer is not None:
                await self._writer.drain()

    async def _end_stream(self, st: _StreamState) -> None:
        out = b""
        async with self._cond:
            conn = self._conn
            if conn is None or st.local_ended or st.aborted:
                return
            try:
                conn.end_stream(st.sid)
            except StreamClosedError:
                st.local_ended = True
                return
            st.local_ended = True
            out = conn.data_to_send()
            if out and self._writer is not None:
                self._writer.write(out)
        if out and self._writer is not None:
            await self._writer.drain()

    async def _close_stream(self, st: _StreamState, *, reset: bool) -> None:
        out = b""
        async with self._cond:
            conn = self._conn
            if (
                reset
                and conn is not None
                and not st.remote_ended
                and not st.aborted
            ):
                try:
                    conn.reset_stream(st.sid, ErrorCodes.CANCEL)
                except (StreamClosedError, H2Error):
                    pass
            st.aborted = True
            self._streams.pop(st.sid, None)
            # If the reader is blocked pushing into this stream's full queue,
            # make room so it can finish the put and go back to reading the
            # socket.  Otherwise one abandoned download wedges the whole
            # shared HTTP/2 connection.
            while True:
                try:
                    st.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            if conn is not None:
                out = conn.data_to_send()
                if out and self._writer is not None:
                    self._writer.write(out)
            self._cond.notify_all()
        if out and self._writer is not None:
            try:
                await self._writer.drain()
            except (ConnectionError, OSError):
                pass
