import asyncio
import unittest

from xhttp.h2client import XhttpError
from xhttp.socks5 import _ExactSocket, _XhttpSocket


class FakeStream:
    def __init__(self):
        self.reads = []
        self.writes = []
        self.closed = False

    async def read(self, timeout=0.0):
        if not self.reads:
            return None
        item = self.reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def write(self, data):
        self.writes.append(data)

    async def aclose(self):
        self.closed = True


class XhttpSocketTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_returns_empty_on_eof(self):
        sock = _XhttpSocket(FakeStream(), 0.0)
        self.assertEqual(await sock.recv(8192), b"")

    async def test_read_and_write(self):
        stream = FakeStream()
        stream.reads.append(b"hello")
        sock = _XhttpSocket(stream, 0.0)
        self.assertEqual(await sock.recv(8192), b"hello")
        self.assertEqual(await sock.send(b"world"), 5)
        self.assertEqual(stream.writes, [b"world"])

    async def test_stream_error_becomes_connection_reset(self):
        stream = FakeStream()
        stream.reads.append(XhttpError("stream reset"))
        sock = _XhttpSocket(stream, 0.0)
        with self.assertRaises(ConnectionResetError):
            await sock.recv(8192)

    async def test_close_is_idempotent(self):
        stream = FakeStream()
        sock = _XhttpSocket(stream, 0.0)
        await sock.close()
        await sock.close()
        self.assertTrue(stream.closed)
        self.assertTrue(sock.closed)


class FakeWriter:
    def get_extra_info(self, name):
        return None

    def is_closing(self):
        return False

    def close(self):
        pass

    async def wait_closed(self):
        pass


class ExactSocketTest(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_read_waits_for_all_bytes(self):
        reader = asyncio.StreamReader()
        sock = _ExactSocket(reader, FakeWriter())

        async def feed():
            reader.feed_data(b"\x05")
            await asyncio.sleep(0.01)
            reader.feed_data(b"\x01")

        task = asyncio.create_task(feed())
        try:
            self.assertEqual(await sock.recv(2), b"\x05\x01")
        finally:
            await task
            await sock.close()

    async def test_relay_mode_allows_partial_reads(self):
        reader = asyncio.StreamReader()
        sock = _ExactSocket(reader, FakeWriter())
        sock.exact = False
        reader.feed_data(b"\x05")
        try:
            self.assertEqual(await sock.recv(2), b"\x05")
        finally:
            await sock.close()


if __name__ == "__main__":
    unittest.main()
