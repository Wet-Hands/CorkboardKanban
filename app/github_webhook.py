"""Optional GitHub webhook at POST /github, for instant review cards on repos you admin.

Configure the webhook with content type application/json, the secret from
GITHUB_WEBHOOK_SECRET, and the "Pull requests" and "Pull request reviews" events.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, jsonify, request

import config
from app import github
from app.db import transaction, utcnow

log = logging.getLogger(__name__)
bp = Blueprint("github_webhook", __name__)


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _first_delivery(delivery_id: str) -> bool:
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO github_deliveries (delivery_id, received_at) VALUES (?, ?)", (delivery_id, utcnow())
            )
            conn.execute("DELETE FROM github_deliveries WHERE received_at < ?", (week_ago,))
        return True
    except sqlite3.IntegrityError:
        return False


@bp.post("/github")
def github_webhook():
    if not config.GITHUB_WEBHOOK_SECRET:
        abort(404)
    body = request.get_data()
    if not verify_signature(body, request.headers.get("X-Hub-Signature-256"), config.GITHUB_WEBHOOK_SECRET):
        abort(401)
    delivery = request.headers.get("X-GitHub-Delivery", "")
    if delivery and not _first_delivery(delivery):
        return jsonify(result="duplicate delivery")

    event = request.headers.get("X-GitHub-Event", "")
    payload = request.get_json(silent=True) or {}
    if event == "ping":
        return jsonify(result="pong")
    me = github.my_login()
    if not me:
        log.warning("Webhook ignored: set GITHUB_USERNAME (or GITHUB_TOKEN) so Corkboard knows who you are")
        return jsonify(result="ignored: GITHUB_USERNAME not set")

    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    if not pr:
        return jsonify(result="ignored")
    key = github.pr_key(pr["base"]["repo"]["full_name"], pr["number"])
    reviewer = ((payload.get("requested_reviewer") or {}).get("login") or "").lower()

    if event == "pull_request":
        if action == "review_requested" and reviewer == me:
            task = github.open_pr_card(pr)
            return jsonify(result="card created" if task else "already on the board")
        if action == "review_request_removed" and reviewer == me:
            github.close_pr_card(key, "review request removed")
            return jsonify(result="closed")
        if action == "closed":
            github.close_pr_card(key, "PR merged" if pr.get("merged") else "PR closed")
            return jsonify(result="closed")
        if action == "ready_for_review":
            requested = {(r.get("login") or "").lower() for r in pr.get("requested_reviewers", [])}
            if me in requested:
                task = github.open_pr_card(pr)
                return jsonify(result="card created" if task else "already on the board")
    elif event == "pull_request_review":
        author = ((payload.get("review") or {}).get("user") or {}).get("login", "").lower()
        if action == "submitted" and author == me:
            github.close_pr_card(key, "you reviewed it")
            return jsonify(result="closed")
    return jsonify(result="ignored")
