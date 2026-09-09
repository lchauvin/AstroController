"""
Command-line entry point: ``astrocontroller`` or ``python -m astrocontroller``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from .config import Config, ConfigError, StorageConfig, load_config


def _load_dotenv(path: Path) -> None:
    """
    Minimal .env loader.

    Hand-rolled rather than depending on python-dotenv, and it never overwrites
    a variable that is already set, so an explicitly exported key always wins.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astrocontroller",
        description="Remote monitoring and guiding-parameter tuning for N.I.N.A. + PHD2",
    )
    parser.add_argument("--config", help="path to astrocontroller.toml")
    parser.add_argument("--host", help="bind address (overrides config)")
    parser.add_argument("--port", type=int, help="bind port (overrides config)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and connectivity, then exit",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="run against a simulated NINA and PHD2 (no hardware needed)",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # These are chatty at DEBUG and drown out our own logs.
    for noisy in ("httpx", "httpcore", "websockets.client", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def resolve_config(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    return config


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    log = logging.getLogger("astrocontroller")

    _load_dotenv(Path.cwd() / ".env")

    try:
        config = resolve_config(args)
        token = config.require_token()
    except ConfigError as exc:
        log.error("configuration error:\n%s", exc)
        return 2

    if args.check:
        return _check(config, log)

    if token:
        log.info("access token required (from %s)", config.server.token_env)

    if args.fake:
        return _run_fake(config, log)

    import uvicorn

    from .server.app import create_app

    log.info(
        "AstroController on http://%s:%d  (NINA %s, PHD2 %s:%d)",
        config.server.host, config.server.port,
        config.nina.rest_base, config.phd2.host, config.phd2.port,
    )
    serve(config, create_app, uvicorn)
    return 0


def serve(config: Config, create_app, uvicorn) -> None:
    """
    Run uvicorn so that Ctrl+C actually exits.

    The server is built explicitly rather than via `uvicorn.run` so its
    `should_exit` flag can be handed to the app: the SSE endpoint holds a
    connection open indefinitely, and uvicorn waits for open connections
    *before* running lifespan shutdown, so without this the process hangs on
    "Waiting for connections to close" until every browser tab is closed.
    `timeout_graceful_shutdown` is a backstop for anything else that lingers.
    """
    holder: dict = {}
    app = create_app(config, should_stop=lambda: bool(holder.get("server") and holder["server"].should_exit))
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server.host,
            port=config.server.port,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    holder["server"] = server
    server.run()


def _run_fake(config: Config, log: logging.Logger) -> int:
    """
    Run the whole stack against a simulator.

    Everything is hosted in one event loop: the fake NINA API, the fake PHD2
    socket server, and AstroController itself pointed at both. The learning
    store is redirected to a separate file so a play session never pollutes
    real observing history.
    """
    import asyncio

    import uvicorn

    from .fake import FakeImageShare, FakePhd2Server, SimState, build_fake_nina_app
    from .server.app import create_app

    async def run() -> None:
        sim = SimState()

        phd2 = FakePhd2Server(sim)
        phd2_port = await phd2.start()

        # The simulated share replaces the configured one for the duration:
        # --fake means "no real hardware", and a run that quietly showed last
        # night's real frames would be a confusing thing to debug against.
        share = FakeImageShare(sim)
        simulated_share = await asyncio.to_thread(share.start)
        config.images.share_path = simulated_share or ""

        nina_app = build_fake_nina_app(sim, share)
        nina_config = uvicorn.Config(
            nina_app, host="127.0.0.1", port=0, log_config=None, access_log=False
        )
        nina_server = uvicorn.Server(nina_config)
        nina_task = asyncio.create_task(nina_server.serve(), name="fake-nina")
        while not nina_server.started:
            await asyncio.sleep(0.05)
        nina_port = nina_server.servers[0].sockets[0].getsockname()[1]

        config.simulated = True
        config.nina.host = "127.0.0.1"
        config.nina.port = nina_port
        config.phd2.host = "127.0.0.1"
        config.phd2.instance = phd2_port - 4400 + 1
        # Keep simulated runs out of real observing history, but respect an
        # explicitly configured path so a test setup can point somewhere else.
        if config.storage.db_path == StorageConfig().db_path:
            config.storage.db_path = "astrocontroller-fake.db"

        log.warning("SIMULATION MODE -- no real hardware is involved")
        log.info("fake NINA on :%d, fake PHD2 on :%d", nina_port, phd2_port)
        log.info(
            "open http://%s:%d",
            "localhost" if config.server.is_loopback else config.server.host,
            config.server.port,
        )

        holder: dict = {}
        app = create_app(
            config,
            should_stop=lambda: bool(
                holder.get("server") and holder["server"].should_exit
            ),
        )
        app_server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.server.host,
                port=config.server.port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        )
        holder["server"] = app_server
        try:
            await app_server.serve()
        finally:
            # The simulator's own uvicorn instance logs a CancelledError
            # traceback from its interrupted lifespan when the loop tears down.
            # It is harmless but looks like a crash on every Ctrl+C, so quiet
            # that logger once we are already on the way out.
            logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
            nina_server.should_exit = True
            await phd2.stop()
            nina_task.cancel()
            share.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


def _check(config: Config, log: logging.Logger) -> int:
    """Validate config and probe both upstreams without starting the server."""
    import asyncio

    async def probe() -> int:
        import socket

        from .nina.rest import NinaRest

        failures = 0
        log.info("config: %s", config.source_path or "built-in defaults")

        rest = NinaRest(config.nina.rest_base, timeout=5.0)
        try:
            version = await rest.version()
            log.info("NINA Advanced API reachable: version %s", version)
        except Exception as exc:  # noqa: BLE001
            log.error("NINA unreachable at %s: %s", config.nina.rest_base, exc)
            failures += 1
        finally:
            await rest.aclose()

        try:
            with socket.create_connection(
                (config.phd2.host, config.phd2.port), timeout=3.0
            ) as sock:
                greeting = sock.recv(400).decode("utf-8", "replace").strip()
            log.info("PHD2 reachable on port %d: %.120s", config.phd2.port, greeting)
        except OSError as exc:
            log.error(
                "PHD2 unreachable at %s:%d: %s "
                "(is 'Tools > Enable Server' switched on?)",
                config.phd2.host, config.phd2.port, exc,
            )
            failures += 1

        return 1 if failures else 0

    return asyncio.run(probe())


if __name__ == "__main__":
    raise SystemExit(main())
