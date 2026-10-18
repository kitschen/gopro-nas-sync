"""Command line entry point: `gopro-sync serve | sync | status`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

from gopro_sync import __version__
from gopro_sync.config import DEFAULT_CONFIG_PATH, DEFAULT_STATE_DIR, ConfigStore


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from gopro_sync.db import Database
    from gopro_sync.nas import NasManager
    from gopro_sync.sync import SyncService
    from gopro_sync.web import create_app

    config = ConfigStore(Path(args.config))
    state_dir = Path(args.state_dir)
    db = Database(state_dir / "state.db")
    if args.demo:
        from gopro_sync.cameras.fake import FakeDriver

        driver = FakeDriver()
    else:
        from gopro_sync.cameras.opengopro import OpenGoProDriver

        driver = OpenGoProDriver(config.config.network.wifi_interface, state_dir)
    nas = NasManager(config.config.network.lan_interface)
    app = create_app(config, db, SyncService(config, db, driver, nas), nas)
    uvicorn.run(app, host=args.host or config.config.server.host, port=args.port or config.config.server.port,
                log_level=args.log_level.lower())


def _local_request(args: argparse.Namespace, method: str, path: str) -> dict:
    config = ConfigStore(Path(args.config)).config
    url = f"http://127.0.0.1:{args.port or config.server.port}{path}"
    req = urllib.request.Request(url, method=method)
    if config.server.api_token:
        req.add_header("Authorization", f"Bearer {config.server.api_token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gopro-sync", description="Sync GoPro media to a NAS")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config file path")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the daemon (web UI + API)")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--demo", action="store_true", help="use a simulated camera (for trying the UI)")

    for name, help_text in (("sync", "trigger a sync on the running daemon"), ("status", "show daemon status")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--port", type=int)

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "serve":
        serve(args)
    elif args.command == "sync":
        print(json.dumps(_local_request(args, "POST", "/api/sync"), indent=2))
    elif args.command == "status":
        print(json.dumps(_local_request(args, "GET", "/api/status"), indent=2))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
