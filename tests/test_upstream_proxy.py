import asyncio
import unittest

from xhttp.config import Config, finalize
from xhttp.h2client import H2Session, XhttpError

UUID = "03912810-3d4f-4514-b2f3-f3b74fa0495f"


async def _fake_proxy(reply: bytes, seen: list[bytes]):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            seen.append(head)
            writer.write(reply)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


class UpstreamConnectTest(unittest.TestCase):
    def run_session(self, cfg: Config, reply: bytes = b"HTTP/1.1 400 Bad Request\r\n\r\n"):
        seen: list[bytes] = []
        outcome: dict[str, BaseException] = {}

        async def main():
            server = await _fake_proxy(reply, seen)
            port = server.sockets[0].getsockname()[1]
            cfg.upstream_proxy = f"http://127.0.0.1:{port}"
            session = H2Session(finalize(cfg))
            try:
                await session.ensure_connected()
            except XhttpError as exc:
                outcome["error"] = exc
            finally:
                await session.close()
                server.close()
                await server.wait_closed()

        asyncio.run(main())
        return seen, outcome.get("error")

    def test_connect_target_always_has_a_port(self):
        # Regression: a TLS-enabled default port used to be dropped, producing
        # "CONNECT 65.49.208.101", which strict proxies answer 400.
        seen, exc = self.run_session(
            Config(server="65.49.208.101", port=443, uuid=UUID, tls=True)
        )
        self.assertEqual(len(seen), 1)
        self.assertTrue(
            seen[0].startswith(b"CONNECT 65.49.208.101:443 HTTP/1.1\r\n"), seen[0]
        )
        self.assertIsNotNone(exc)
        self.assertIn("CONNECT 65.49.208.101:443", str(exc))

    def test_connect_target_has_port_for_plaintext_default(self):
        seen, exc = self.run_session(
            Config(server="example.com", port=80, uuid=UUID, tls=False)
        )
        self.assertTrue(
            seen[0].startswith(b"CONNECT example.com:80 HTTP/1.1\r\n"), seen[0]
        )
        self.assertIsNotNone(exc)

    def test_connect_target_brackets_ipv6(self):
        seen, _ = self.run_session(
            Config(server="2001:db8::1", port=8443, uuid=UUID, tls=True)
        )
        self.assertTrue(
            seen[0].startswith(b"CONNECT [2001:db8::1]:8443 HTTP/1.1\r\n"), seen[0]
        )


if __name__ == "__main__":
    unittest.main()
