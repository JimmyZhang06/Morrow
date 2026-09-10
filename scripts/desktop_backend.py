"""Packaged loopback runtime. Configuration arrives over a private parent pipe."""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import sys
import threading
from pathlib import Path

import uvicorn
from alembic.config import Config

from alembic import command
from life_coach.api.app import create_app
from life_coach.platform.settings import Settings


def main() -> None:
    payload = json.loads(sys.stdin.readline())
    if payload.get("version") != 1:
        raise ValueError("unsupported runtime configuration")
    # Never inherit development credentials or provider selection from the host.
    for name in list(os.environ):
        if name.startswith(("APP_", "LOCAL_")):
            del os.environ[name]
    for name, value in payload["environment"].items():
        if not name.startswith(("APP_", "LOCAL_")) or not isinstance(value, str):
            raise ValueError("invalid runtime environment")
        os.environ[name] = value
    if os.environ.get("APP_ENV") != "desktop":
        raise ValueError("desktop mode required")
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    runtime_dsn = os.environ["APP_DATABASE_URL"]
    os.environ["APP_DATABASE_URL"] = os.environ["LOCAL_ADMIN_DATABASE_URL"]
    config = Config()
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")
    seed = runpy.run_path(str(root / "scripts" / "seed_local.py"))["seed"]
    asyncio.run(seed(include_consent=payload.get("aiConsent") is True))
    os.environ["APP_DATABASE_URL"] = runtime_dsn
    settings = Settings(_env_file=None)
    server = uvicorn.Server(uvicorn.Config(
        create_app(settings=settings), host="127.0.0.1", port=int(payload["apiPort"]),
        access_log=False, log_level="warning", timeout_graceful_shutdown=10,
    ))

    def watch_parent() -> None:
        # Closing the pipe also shuts down the API after a parent crash.
        sys.stdin.read()
        server.should_exit = True

    threading.Thread(target=watch_parent, daemon=True).start()
    server.run()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not emit SQL, user material, credentials, or validation inputs.
        print(f"DESKTOP_BACKEND_START_FAILED:{type(exc).__name__}", file=sys.stderr, flush=True)
        sys.exit(1)
