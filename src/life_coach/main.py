"""ASGI entry point for the Life Coach API."""

from __future__ import annotations

import uvicorn

from life_coach.api.app import create_app

app = create_app()


def run() -> None:
    """Run the development server; production should use an ASGI process manager."""

    uvicorn.run("life_coach.main:app", host="127.0.0.1", port=8000, access_log=False)


if __name__ == "__main__":
    run()
