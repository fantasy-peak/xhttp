import unittest

from xhttp_proxy.proxy import (
    ProtocolError,
    _rewrite_request,
    _split_connect_target,
    format_authority,
    parse_request_head,
)


class RequestHeadTest(unittest.TestCase):
    def test_connect(self):
        req = parse_request_head(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        )
        self.assertEqual(req.method, "CONNECT")
        self.assertEqual(req.target, "example.com:443")
        self.assertEqual(req.headers, [("Host", "example.com:443")])

    def test_rejects_folded_header(self):
        with self.assertRaises(ProtocolError):
            parse_request_head(b"GET http://x/ HTTP/1.1\r\n bad: value\r\n\r\n")

    def test_rejects_bad_request_line(self):
        with self.assertRaises(ProtocolError):
            parse_request_head(b"GET / HTTP/1.1 extra\r\n\r\n")


class ConnectTargetTest(unittest.TestCase):
    def test_host_port(self):
        self.assertEqual(_split_connect_target("example.com:8443"), ("example.com", 8443))

    def test_default_port(self):
        self.assertEqual(_split_connect_target("example.com"), ("example.com", 443))

    def test_ipv6(self):
        self.assertEqual(_split_connect_target("[::1]:8443"), ("::1", 8443))

    def test_bad_port(self):
        with self.assertRaises((ProtocolError, ValueError)):
            _split_connect_target("example.com:99999")


class RewriteTest(unittest.TestCase):
    def test_strips_hop_by_hop_and_forces_close(self):
        req = parse_request_head(
            b"GET http://example.com/a?b=1 HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"Connection: X-Drop\r\n"
            b"X-Drop: yes\r\n"
            b"X-Keep: no\r\n\r\n"
        )
        out = _rewrite_request(req, "/a?b=1", "example.com").decode("latin-1")
        self.assertIn("GET /a?b=1 HTTP/1.1\r\n", out)
        self.assertIn("Host: example.com\r\n", out)
        self.assertIn("X-Keep: no\r\n", out)
        self.assertNotIn("Proxy-Connection", out)
        self.assertNotIn("X-Drop", out)
        self.assertTrue(out.endswith("Connection: close\r\n\r\n"))

    def test_adds_missing_host(self):
        req = parse_request_head(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
        out = _rewrite_request(req, "/", "example.com:8080").decode("latin-1")
        self.assertIn("Host: example.com:8080\r\n", out)


class AuthorityTest(unittest.TestCase):
    def test_default_port_is_omitted(self):
        self.assertEqual(format_authority("example.com", 80, 80), "example.com")

    def test_non_default_port(self):
        self.assertEqual(format_authority("example.com", 8080, 80), "example.com:8080")

    def test_ipv6_is_bracketed(self):
        self.assertEqual(format_authority("::1", 80, 80), "[::1]")


if __name__ == "__main__":
    unittest.main()
