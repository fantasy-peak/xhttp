"""VLESS request header encoding and response header decoding.

The VLESS request header (client -> server) is::

    version(1) | uuid(16) | addon_len(1) | addon(addon_len) | cmd(1)
    | port(2, big endian) | atyp(1) | address

``atyp`` is 1 for IPv4, 2 for a domain name, 3 for IPv6.  A domain is encoded
as a one-byte length followed by the ASCII name.

The response header is only ``version(1) | addon_len(1) | addon(addon_len)``;
everything after it is payload.
"""

from __future__ import annotations

import ipaddress
import struct
import uuid as uuid_module

CMD_TCP = 0x01
CMD_UDP = 0x02

ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x02
ATYP_IPV6 = 0x03

VLESS_VERSION = 0x00


class VlessError(ValueError):
    """A malformed UUID, address, or VLESS header."""


def parse_uuid(value: str) -> bytes:
    """Return the 16 raw bytes of a UUID in any form :mod:`uuid` accepts."""

    text = value.strip()
    if not text:
        raise VlessError("empty UUID")
    try:
        return uuid_module.UUID(text).bytes
    except (ValueError, AttributeError) as exc:
        raise VlessError(f"invalid UUID {value!r}: {exc}") from exc


def encode_address(host: str) -> tuple[int, bytes]:
    """Encode a destination host for a VLESS header."""

    host = host.strip()
    if not host:
        raise VlessError("empty destination host")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            raw = host.encode("ascii")
        except UnicodeEncodeError as exc:
            raise VlessError(
                f"destination {host!r} is not ASCII; use a punycode (xn--) name"
            ) from exc
        if not raw or len(raw) > 255:
            raise VlessError(f"destination name is {len(raw)} bytes, max 255")
        return ATYP_DOMAIN, bytes([len(raw)]) + raw

    if ip.version == 4:
        return ATYP_IPV4, ip.packed
    return ATYP_IPV6, ip.packed


def request_header(
    uuid_bytes: bytes,
    host: str,
    port: int,
    *,
    cmd: int = CMD_TCP,
    addon: bytes = b"",
) -> bytes:
    """Build the VLESS request header for one stream."""

    if len(uuid_bytes) != 16:
        raise VlessError(f"UUID must be 16 bytes, got {len(uuid_bytes)}")
    if not 0 < port < 65536:
        raise VlessError(f"port out of range: {port}")
    if len(addon) > 255:
        raise VlessError(f"addon is {len(addon)} bytes, max 255")
    if cmd not in (CMD_TCP, CMD_UDP):
        raise VlessError(f"unsupported VLESS command: {cmd}")

    atyp, address = encode_address(host)
    return (
        bytes([VLESS_VERSION])
        + uuid_bytes
        + bytes([len(addon)])
        + addon
        + bytes([cmd])
        + struct.pack(">H", port)
        + bytes([atyp])
        + address
    )


def parse_response_header(prefix: bytes) -> tuple[int, int]:
    """Validate the two fixed bytes of a VLESS response header.

    Returns ``(version, addon_len)``; the caller must consume ``addon_len``
    bytes that follow.
    """

    if len(prefix) != 2:
        raise VlessError(f"response header needs 2 bytes, got {len(prefix)}")
    return prefix[0], prefix[1]
