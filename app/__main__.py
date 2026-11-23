"""Run the web UI, the Claude connector and the GitHub webhook:  python -m app"""

from __future__ import annotations

import logging

import uvicorn

import config


def build():
    from app import create_app
    from app.mcp_server import build_asgi_app

    return build_asgi_app(create_app())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        build(),
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )


if __name__ == "__main__":
    main()
