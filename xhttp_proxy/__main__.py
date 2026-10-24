"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import traceback
from pathlib import Path

from . import __version__
from .config import (
    Config,
    ConfigError,
    apply_share_url,
    config_file_candidates,
    finalize,
    from_mapping,
    load_json_file,
    parse_host_port,
)
from .h2client import H2Session
from .proxy import HttpProxyServer

log = logging.getLogger("xhttp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xhttp-proxy",
        description=(
            "Local HTTP proxy that carries each connection as a VLESS stream "
            "over an XHTTP (stream-one) HTTP/2 request."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", metavar="FILE", help="JSON config file")
    parser.add_argument(
        "--url",
        metavar="VLESS_URL",
        help="vless:// share URL (type=xhttp, mode=stream-one)",
    )
    parser.add_argument("--server", metavar="HOST[:PORT]", help="XHTTP server address")
    parser.add_argument("--port", type=int, help="XHTTP server port")
    parser.add_argument("--uuid", help="VLESS user UUID")
    parser.add_argument("--path", help="XHTTP path")
    parser.add_argument("--sni", help="TLS server name (SNI)")
    parser.add_argument(
        "--host", dest="host_header", metavar="HOST", help="HTTP Host / :authority override"
    )
    parser.add_argument("--user-agent", help="User-Agent sent to the server")
    parser.add_argument(
        "--listen",
        metavar="HOST:PORT",
        help="local HTTP proxy listen address",
    )
    parser.add_argument(
        "--no-tls",
        action="store_true",
        default=None,
        help="use cleartext h2c with prior knowledge instead of TLS",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=None,
        help="do not verify the server certificate (testing only)",
    )
    parser.add_argument("--ca", metavar="FILE", help="CA bundle used to verify the server")
    parser.add_argument(
        "--pin-sha256",
        metavar="HEX",
        help="pin the leaf certificate SHA-256 (implies no CA check unless --ca is set)",
    )
    parser.add_argument(
        "--upstream-proxy",
        metavar="http://HOST:PORT",
        help="reach the XHTTP server through an HTTP CONNECT proxy",
    )
    parser.add_argument(
        "--connect-timeout", type=float, metavar="SEC", help="TCP/TLS connect timeout"
    )
    parser.add_argument(
        "--response-timeout",
        type=float,
        metavar="SEC",
        help="timeout for request headers and the VLESS response header",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        metavar="SEC",
        help="abort a tunnel after this many seconds without downlink data (0=off)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", default=None)
    parser.add_argument(
        "--check", action="store_true", help="validate the configuration and exit"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def build_config(args: argparse.Namespace) -> Config:
    if args.config:
        path = Path(args.config).expanduser()
        data = load_json_file(path)
    else:
        data = {}
        for candidate in config_file_candidates(None):
            if candidate.is_file():
                data = load_json_file(candidate)
                log.debug("loaded config from %s", candidate)
                break

    cfg = from_mapping(data)

    if args.url:
        apply_share_url(cfg, args.url)

    if args.server:
        default_port = args.port or cfg.port or (443 if cfg.tls else 80)
        host, port = parse_host_port(args.server, default_port)
        cfg.server = host
        cfg.port = port
    if args.port is not None:
        cfg.port = args.port
    if args.uuid:
        cfg.uuid = args.uuid
    if args.path:
        cfg.path = args.path
    if args.sni:
        cfg.sni = args.sni
    if args.host_header:
        cfg.host = args.host_header
    if args.user_agent:
        cfg.user_agent = args.user_agent
    if args.listen:
        if args.listen.isdigit():
            cfg.listen_host = "127.0.0.1"
            cfg.listen_port = int(args.listen)
        else:
            host, port = parse_host_port(args.listen, 1080)
            cfg.listen_host = host
            cfg.listen_port = port
    if args.no_tls:
        cfg.tls = False
    if args.insecure is not None:
        cfg.insecure = args.insecure
    if args.ca:
        cfg.ca = args.ca
    if args.pin_sha256:
        cfg.pin_sha256 = args.pin_sha256
    if args.upstream_proxy:
        cfg.upstream_proxy = args.upstream_proxy
    if args.connect_timeout is not None:
        cfg.connect_timeout = args.connect_timeout
    if args.response_timeout is not None:
        cfg.response_timeout = args.response_timeout
    if args.idle_timeout is not None:
        cfg.idle_timeout = args.idle_timeout
    if args.verbose is not None:
        cfg.verbose = args.verbose

    return finalize(cfg)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _install_task_dump() -> None:
    """Dump all asyncio task stacks on SIGUSR1 (POSIX only, for debugging)."""

    if not hasattr(signal, "SIGUSR1"):
        return
    loop = asyncio.get_running_loop()

    def dump() -> None:
        print("=== asyncio tasks ===", file=sys.stderr, flush=True)
        for task in asyncio.all_tasks():
            print(f"--- {task!r}", file=sys.stderr)
            for line in task.get_stack():
                print("".join(traceback.format_stack(line)), file=sys.stderr)
        print("=== end tasks ===", file=sys.stderr, flush=True)

    try:
        loop.add_signal_handler(signal.SIGUSR1, dump)
    except (NotImplementedError, RuntimeError):
        pass


async def _run(cfg: Config) -> None:
    _install_task_dump()
    session = H2Session(cfg)
    proxy = HttpProxyServer(cfg, session)
    try:
        await proxy.serve()
    finally:
        await proxy.close()
        await session.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = build_config(args)
    except ConfigError as exc:
        print(f"xhttp-proxy: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.verbose)

    if args.check:
        print(cfg.summary())
        return 0

    log.info("xhttp-proxy %s", __version__)
    log.info("%s", cfg.summary())
    if cfg.tls and cfg.insecure:
        log.warning("TLS certificate verification is disabled (--insecure)")
    if cfg.tls and cfg.pin_sha256 and not cfg.ca:
        log.info("verifying the server by certificate pin only")
    if not cfg.tls:
        log.info("plaintext h2c prior knowledge (no TLS)")

    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        log.info("interrupted")
    except OSError as exc:
        log.error("failed to start: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
