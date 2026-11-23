"""Corkboard web application (Flask)."""

from __future__ import annotations

from flask import Flask

import config


def create_app() -> Flask:
    from app.db import init_db
    from app.github_webhook import bp as github_bp
    from app.web import bp as web_bp, register_template_helpers

    init_db()
    flask_app = Flask(
        __name__,
        template_folder=str(config.BASE_DIR / "templates"),
        static_folder=str(config.BASE_DIR / "static"),
    )
    flask_app.config.update(SECRET_KEY=config.SECRET_KEY, SESSION_COOKIE_SAMESITE="Lax")
    flask_app.register_blueprint(web_bp)
    flask_app.register_blueprint(github_bp)
    register_template_helpers(flask_app)
    return flask_app
