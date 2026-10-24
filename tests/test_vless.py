import struct
import unittest

from xhttp_proxy.vless import (
    ATYP_DOMAIN,
    ATYP_IPV4,
    ATYP_IPV6,
    VlessError,
    encode_address,
    parse_response_header,
    parse_uuid,
    request_header,
)

UUID = "03912810-3d4f-4514-b2f3-f3b74fa0495f"


class VlessTest(unittest.TestCase):
    def test_uuid_is_16_raw_bytes(self):
        self.assertEqual(parse_uuid(UUID).hex(), "039128103d4f4514b2f3f3b74fa0495f")

    def test_uuid_rejects_garbage(self):
        with self.assertRaises(VlessError):
            parse_uuid("not-a-uuid")

    def test_domain_address(self):
        atyp, encoded = encode_address("example.com")
        self.assertEqual(atyp, ATYP_DOMAIN)
        self.assertEqual(encoded, b"\x0bexample.com")

    def test_ipv4_address(self):
        atyp, encoded = encode_address("127.0.0.1")
        self.assertEqual(atyp, ATYP_IPV4)
        self.assertEqual(encoded, b"\x7f\x00\x00\x01")

    def test_ipv6_address(self):
        atyp, encoded = encode_address("::1")
        self.assertEqual(atyp, ATYP_IPV6)
        self.assertEqual(encoded, b"\x00" * 15 + b"\x01")

    def test_request_header_tcp(self):
        header = request_header(parse_uuid(UUID), "example.com", 443)
        self.assertEqual(header[0], 0)  # version
        self.assertEqual(header[1:17].hex(), "039128103d4f4514b2f3f3b74fa0495f")
        self.assertEqual(header[17], 0)  # addon length
        self.assertEqual(header[18], 1)  # CMD_TCP
        self.assertEqual(struct.unpack(">H", header[19:21])[0], 443)
        self.assertEqual(header[21], ATYP_DOMAIN)
        self.assertEqual(header[22:], b"\x0bexample.com")

    def test_request_header_rejects_bad_port(self):
        with self.assertRaises(VlessError):
            request_header(parse_uuid(UUID), "example.com", 70000)

    def test_response_header(self):
        self.assertEqual(parse_response_header(b"\x00\x00"), (0, 0))

    def test_response_header_needs_two_bytes(self):
        with self.assertRaises(VlessError):
            parse_response_header(b"\x00")


if __name__ == "__main__":
    unittest.main()
