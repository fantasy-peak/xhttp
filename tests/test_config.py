import unittest

from xhttp.config import (
    Config,
    ConfigError,
    apply_share_url,
    finalize,
    looks_like_config,
    parse_host_port,
)


UUID = "03912810-3d4f-4514-b2f3-f3b74fa0495f"


class HostPortTest(unittest.TestCase):
    def test_host_only(self):
        self.assertEqual(parse_host_port("example.com", 443), ("example.com", 443))

    def test_host_port(self):
        self.assertEqual(parse_host_port("example.com:8443", 443), ("example.com", 8443))

    def test_ipv6(self):
        self.assertEqual(parse_host_port("[::1]:8443", 443), ("::1", 8443))

    def test_bare_ipv6(self):
        self.assertEqual(parse_host_port("::1", 443), ("::1", 443))

    def test_bad_port(self):
        with self.assertRaises(ConfigError):
            parse_host_port("example.com:0", 443)


class LooksLikeConfigTest(unittest.TestCase):
    def test_accepts_our_schema(self):
        self.assertTrue(looks_like_config({"server": "example.com", "uuid": UUID}))

    def test_rejects_another_tools_config(self):
        self.assertFalse(
            looks_like_config(
                {"h2_path": "/x", "listen_address": "0.0.0.0", "uuid": UUID}
            )
        )


class FinalizeTest(unittest.TestCase):
    def make(self, **kwargs):
        values = {"server": "example.com", "uuid": UUID, **kwargs}
        return finalize(Config(**values))

    def test_default_port_follows_tls(self):
        self.assertEqual(self.make().port, 443)
        self.assertEqual(self.make(tls=False).port, 80)

    def test_authority_includes_non_default_port(self):
        cfg = self.make(port=8443)
        self.assertEqual(cfg.authority, "example.com:8443")

    def test_authority_uses_host_override(self):
        cfg = self.make(host="cdn.example.net")
        self.assertEqual(cfg.authority, "cdn.example.net")

    def test_connect_authority_keeps_default_port(self):
        # RFC 9110 9.3.6: a CONNECT target must always carry the port.
        cfg = self.make()
        self.assertEqual(cfg.connect_authority, "example.com:443")
        self.assertEqual(self.make(tls=False).connect_authority, "example.com:80")

    def test_connect_authority_brackets_ipv6(self):
        self.assertEqual(
            self.make(server="2001:db8::1").connect_authority, "[2001:db8::1]:443"
        )

    def test_connect_authority_ignores_host_override(self):
        cfg = self.make(host="cdn.example.net")
        self.assertEqual(cfg.connect_authority, "example.com:443")

    def test_path_gets_leading_slash(self):
        self.assertEqual(self.make(path="xhttp").path, "/xhttp")

    def test_pin_is_normalized(self):
        cfg = self.make(pin_sha256="AA:" * 31 + "BB")
        self.assertEqual(cfg.pin_sha256, "aa" * 31 + "bb")

    def test_bad_pin_rejected(self):
        with self.assertRaises(ConfigError):
            self.make(pin_sha256="abcd")

    def test_uuid_required(self):
        with self.assertRaises(ConfigError):
            finalize(Config(server="example.com"))

    def test_socks_is_disabled_by_default(self):
        self.assertIsNone(self.make().socks_endpoint())

    def test_socks_can_be_enabled(self):
        cfg = self.make(socks_listen="127.0.0.1:1081")
        self.assertEqual(cfg.socks_endpoint(), ("127.0.0.1", 1081))

    def test_socks_numeric_port(self):
        self.assertEqual(self.make(socks_listen="1082").socks_endpoint(), ("127.0.0.1", 1082))

    def test_socks_cannot_share_the_http_address(self):
        with self.assertRaises(ConfigError):
            self.make(socks_listen="127.0.0.1:1080")


class ShareUrlTest(unittest.TestCase):
    def test_basic(self):
        cfg = Config()
        apply_share_url(
            cfg,
            "vless://"
            + UUID
            + "@example.com:8443?type=xhttp&mode=stream-one&security=tls"
            + "&path=%2Fxhttp&sni=cdn.example.net",
        )
        cfg = finalize(cfg)
        self.assertEqual(cfg.server, "example.com")
        self.assertEqual(cfg.port, 8443)
        self.assertEqual(cfg.path, "/xhttp")
        self.assertEqual(cfg.sni, "cdn.example.net")
        self.assertTrue(cfg.tls)

    def test_plaintext(self):
        cfg = Config()
        apply_share_url(
            cfg,
            "vless://" + UUID + "@example.com?type=xhttp&security=none&path=/x",
        )
        cfg = finalize(cfg)
        self.assertFalse(cfg.tls)
        self.assertEqual(cfg.port, 80)

    def test_reality_rejected(self):
        cfg = Config()
        with self.assertRaises(ConfigError):
            apply_share_url(
                cfg,
                "vless://" + UUID + "@example.com:443?type=xhttp&security=reality",
            )


if __name__ == "__main__":
    unittest.main()
