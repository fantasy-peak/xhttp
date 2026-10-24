"""Bidirectional byte relay between a local socket and one XHTTP stream.

Shared by the HTTP proxy (CONNECT) and the SOCKS5 CONNECT handler.  The
uplink half-closes with END_STREAM; the downlink half-closes the local socket
when the server ends the stream, so protocols that half-close still work.
"""

from __future__ import annotations

import asyncio
import logging

from .h2client import XhttpError, XhttpStream

log = logging.getLogger("xhttp")


async def relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stream: XhttpStream,
    idle_timeout: float = 0.0,
) -> None:
    up = asyncio.create_task(_pump_up(reader, stream), name="xhttp-up")
    down = asyncio.create_task(
        _pump_down(writer, stream, idle_timeout), name="xhttp-down"
    )
    try:
        done, _ = await asyncio.wait(
            {up, down}, return_when=asyncio.FIRST_COMPLETED
        )
        if down in done:
            # The response/target side finished; the client has nothing left
            # to wait for.
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
                except (XhttpError, OSError):
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
            if isinstance(result, OSError):
                log.debug("relay ended: %s", result)
            elif isinstance(result, Exception):
                log.debug("relay error: %s", result)
        await stream.aclose()


async def _pump_up(
    reader: asyncio.StreamReader, stream: XhttpStream
) -> None:
    while True:
        data = await reader.read(65536)
        if not data:
            await stream.write_eof()
            return
        await stream.write(data)


async def _pump_down(
    writer: asyncio.StreamWriter,
    stream: XhttpStream,
    idle_timeout: float,
) -> None:
    while True:
        data = await stream.read(idle_timeout)
        if data is None:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except (OSError, ConnectionError):
                pass
            return
        writer.write(data)
        await writer.drain()
