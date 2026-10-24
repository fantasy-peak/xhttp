"""Configuration loading for the XHTTP proxy.

Configuration comes from up to three layers, each overriding the previous one:

1. dataclass defaults,
2. a JSON file (``--config`` or ``config.json`` next to the program),
3. command-line flags.

A ``vless://`` share link can be given with ``--url``; its fields are applied
between layers 2 and 3.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import re
import uuid as uuid_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(ValueError):
    """Invalid or missing configuration."""


@dataclass(slots=True)
class Config:
    # XHTTP / VLESS endpoint
    server: str = ""
    port: int = 0
    uuid: str = ""
    path: str = "/"
    sni: str = ""
    host: str = ""                 # HTTP Host / :authority override
    user_agent: str = DEFAULT_USER_AGENT
    tls: bool = True
    insecure: bool = False
    ca: str = ""
    pin_sha256: str = ""

    # local HTTP proxy
    listen_host: str = "127.0.0.1"
    listen_port: int = 1080

    # optional upstream (corporate) HTTP proxy, http://host:port
    upstream_proxy: str = ""

    # timeouts, seconds; 0 disables idle_timeout only
    connect_timeout: float = 15.0
    response_timeout: float = 15.0
    idle_timeout: float = 0.0

    verbose: bool = False

    # ---- derived values -------------------------------------------------

    @property
    def scheme(self) -> str:
        return "https" if self.tls else "http"

    @property
    def server_authority(self) -> str:
        """``host[:port]`` of the actual server (brackets for IPv6)."""

        host = self.server
        try:
            ip = ipaddress.ip_address(host)
            if ip.version == 6:
                host = f"[{host}]"
        except ValueError:
            pass
        default_port = 443 if self.tls else 80
        if self.port == default_port:
            return host
        return f"{host}:{self.port}"

    @property
    def authority(self) -> str:
        """Value for the HTTP/2 ``:authority`` pseudo-header."""

        if self.host:
            return self.host
        return self.server_authority

    @property
    def server_hostname(self) -> str:
        return self.sni or self.server

    @property
    def uuid_bytes(self) -> bytes:
        return uuid_module.UUID(self.uuid).bytes

    def upstream_endpoint(self) -> tuple[str, int] | None:
        if not self.upstream_proxy:
            return None
        value = self.upstream_proxy.strip()
        if "://" in value:
            parts = urlsplit(value)
            if parts.scheme.lower() != "http":
                raise ConfigError(
                    f"upstream_proxy only supports http://, got {parts.scheme!r}"
                )
            if not parts.hostname:
                raise ConfigError(f"invalid upstream_proxy: {value!r}")
            port = parts.port or 8080
            return parts.hostname, port
        return parse_host_port(value, 8080)

    def summary(self) -> str:
        upstream = ""
        if self.upstream_proxy:
            upstream = f", upstream_proxy={self.upstream_proxy}"
        tls = "tls" if self.tls else "h2c"
        return (
            f"server={self.server}:{self.port} path={self.path} {tls} "
            f"sni={self.server_hostname!r} authority={self.authority!r}; "
            f"listen={self.listen_host}:{self.listen_port}{upstream}"
        )


def parse_host_port(value: str, default_port: int) -> tuple[str, int]:
    """Parse ``host``, ``host:port``, ``[v6]`` or ``[v6]:port``."""

    text = value.strip()
    if not text:
        raise ConfigError("empty host:port")
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ConfigError(f"missing ']' in {value!r}")
        host = text[1:end]
        rest = text[end + 1 :]
        if not rest:
            port = default_port
        elif rest.startswith(":"):
            port = _to_port(rest[1:], value)
        else:
            raise ConfigError(f"trailing data after IPv6 address: {value!r}")
    else:
        if text.count(":") == 1:
            host, port_text = text.split(":", 1)
            port = _to_port(port_text, value)
        elif ":" not in text:
            host, port = text, default_port
        else:
            # A bare IPv6 literal without a port.
            host, port = text, default_port
    if not host:
        raise ConfigError(f"empty host in {value!r}")
    return host, port


def _to_port(text: str, whole: str) -> int:
    try:
        port = int(text, 10)
    except ValueError as exc:
        raise ConfigError(f"invalid port in {whole!r}") from exc
    if not 0 < port < 65536:
        raise ConfigError(f"port out of range in {whole!r}: {port}")
    return port


def normalize_pin(value: str) -> str:
    """Accept ``AA:BB:..`` or ``aabb..`` and return lowercase hex."""

    pin = re.sub(r"[\s:]", "", value).lower()
    if pin and not _HEX64.match(pin):
        raise ConfigError(
            "pin_sha256 must be 32 hex bytes (64 hex characters), optionally colon-separated"
        )
    return pin


def config_file_candidates(explicit: str | None) -> list[Path]:
    """Where config.json is looked up when --config was not given."""

    if explicit:
        return [Path(explicit)]
    import sys

    base = Path(getattr(sys, "executable", sys.argv[0])).resolve().parent
    cwd = Path.cwd()
    candidates = [cwd / "config.json"]
    if base not in (cwd,):
        candidates.append(base / "config.json")
    return candidates


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level JSON value must be an object")
    return data


def from_mapping(data: Mapping[str, Any]) -> Config:
    known = {field.name for field in dataclasses.fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")

    values: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("tls", "insecure", "verbose"):
            values[key] = bool(value)
        elif key in ("port", "listen_port"):
            values[key] = int(value)
        elif key in ("connect_timeout", "response_timeout", "idle_timeout"):
            values[key] = float(value)
        elif value is None:
            continue
        else:
            values[key] = str(value)
    return Config(**values)


def apply_share_url(cfg: Config, url: str) -> None:
    """Apply the subset of a ``vless://`` URL that this client supports."""

    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "vless":
        raise ConfigError(f"expected a vless:// URL, got scheme {parts.scheme!r}")
    if not parts.hostname:
        raise ConfigError("vless URL has no server host")
    if not parts.username:
        raise ConfigError("vless URL has no UUID")

    query = parse_qs(parts.query, keep_blank_values=True)

    def first(name: str, default: str = "") -> str:
        values = query.get(name)
        return values[0] if values else default

    transport = first("type", "xhttp").lower()
    if transport not in ("xhttp", "splithttp"):
        raise ConfigError(f"unsupported transport type={transport!r} (expected xhttp)")

    mode = first("mode", "auto").lower()
    if mode not in ("auto", "stream-one"):
        raise ConfigError(
            f"unsupported XHTTP mode={mode!r}; this client implements stream-one"
        )

    security = first("security", "tls").lower()
    if security == "reality":
        raise ConfigError(
            "REALITY is not supported by this client (no X25519 handshake); "
            "use a TLS or plaintext XHTTP endpoint"
        )
    if security not in ("tls", "none", ""):
        raise ConfigError(f"unsupported security={security!r}")

    cfg.uuid = parts.username
    cfg.server = parts.hostname
    cfg.tls = security == "tls"
    cfg.port = parts.port or (443 if cfg.tls else 80)
    cfg.path = unquote(first("path", cfg.path)) or cfg.path
    cfg.sni = first("sni") or cfg.sni
    cfg.host = first("host") or cfg.host

    pin = first("pinSHA256") or first("pinnedPeerCertSha256")
    if pin:
        cfg.pin_sha256 = pin


def finalize(cfg: Config) -> Config:
    """Validate and fill derived defaults.  Returns the same object."""

    cfg.server = cfg.server.strip()
    if not cfg.server:
        raise ConfigError("server is required (use --server, --url, or config.json)")
    if not cfg.uuid:
        raise ConfigError("UUID is required (use --uuid, --url, or config.json)")

    if cfg.port == 0:
        cfg.port = 443 if cfg.tls else 80
    if not 0 < cfg.port < 65536:
        raise ConfigError(f"port out of range: {cfg.port}")

    if not cfg.path:
        cfg.path = "/"
    if not cfg.path.startswith("/"):
        cfg.path = "/" + cfg.path

    try:
        uuid_module.UUID(cfg.uuid)
    except ValueError as exc:
        raise ConfigError(f"invalid UUID {cfg.uuid!r}: {exc}") from exc

    if not (0 < cfg.listen_port < 65536):
        raise ConfigError(f"listen port out of range: {cfg.listen_port}")
    if cfg.connect_timeout <= 0 or cfg.response_timeout <= 0:
        raise ConfigError("connect_timeout and response_timeout must be > 0")
    if cfg.idle_timeout < 0:
        raise ConfigError("idle_timeout must be >= 0")

    cfg.pin_sha256 = normalize_pin(cfg.pin_sha256)
    if cfg.ca:
        path = Path(cfg.ca).expanduser()
        if not path.is_file():
            raise ConfigError(f"CA file not found: {path}")
        cfg.ca = str(path)

    cfg.upstream_endpoint()  # validate early
    return cfg
