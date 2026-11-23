"""Shared GitHub logic: turning a pull request into a review card and closing it again.

Used by both the poll (worker/github_poll.py) and the webhook (app/github_webhook.py),
and tracked in the seen_github_prs table so a PR never gets two cards.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import requests

import config
from app import models
from app.db import get_conn, transaction, utcnow

log = logging.getLogger(__name__)
TIMEOUT = 20


class GitHubError(Exception):
    pass


def api_get(path: str, params: dict | None = None) -> dict:
    if not config.GITHUB_TOKEN:
        raise GitHubError("GITHUB_TOKEN is not set")
    url = path if path.startswith("http") else f"{config.GITHUB_API_URL}{path}"
    response = requests.get(
        url,
        params=params,
        timeout=TIMEOUT,
        headers={
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "corkboard-kanban",
        },
    )
    if response.status_code >= 400:
        raise GitHubError(f"GET {url} -> {response.status_code}: {response.text[:200]}")
    return response.json()


@lru_cache(maxsize=1)
def my_login() -> str | None:
    """The GitHub user whose review requests become cards."""
    if config.GITHUB_USERNAME:
        return config.GITHUB_USERNAME.lower()
    if config.GITHUB_TOKEN:
        try:
            return api_get("/user")["login"].lower()
        except Exception:
            log.exception("Couldn't look up the GitHub user for this token")
    return None


def pr_key(repo_full_name: str, number: int) -> str:
    return f"{repo_full_name}#{number}"


def priority_for(labels: list[str], draft: bool) -> str | None:
    """Card priority from PR labels; None means don't make a card (draft, with GITHUB_DRAFT_MODE=skip)."""
    names = {label.lower() for label in labels}
    if draft or "draft" in names:
        return None if config.GITHUB_DRAFT_MODE == "skip" else "low"
    if names & set(config.GITHUB_HIGH_LABELS):
        return "high"
    if names & set(config.GITHUB_LOW_LABELS):
        return "low"
    return "medium"


def describe(pr: dict) -> str:
    repo = pr["base"]["repo"]["full_name"]
    parts = [repo, f"by @{pr['user']['login']}"]
    if pr.get("additions") is not None:
        parts.append(f"+{pr['additions']} −{pr.get('deletions', 0)}")
    if pr.get("changed_files") is not None:
        files = pr["changed_files"]
        parts.append(f"{files} file{'s' if files != 1 else ''}")
    return " · ".join(parts)


def _seen(key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM seen_github_prs WHERE pr_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def _mark_seen(key: str, task_id: int | None, skipped: bool) -> None:
    now = utcnow()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO seen_github_prs (pr_key, task_id, active, skipped, first_seen, last_seen)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(pr_key) DO UPDATE SET task_id = excluded.task_id, active = 1,
                   skipped = excluded.skipped, last_seen = excluded.last_seen""",
            (key, task_id, int(skipped), now, now),
        )


def active_keys() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM seen_github_prs WHERE active = 1").fetchall()
    return {r["pr_key"]: dict(r) for r in rows}


def open_pr_card(pr: dict) -> dict | None:
    """Create a review card for a full PR object unless it already has one. Returns the new task."""
    key = pr_key(pr["base"]["repo"]["full_name"], pr["number"])
    seen = _seen(key)
    if seen and seen["active"] and not seen["skipped"]:
        with transaction() as conn:
            conn.execute("UPDATE seen_github_prs SET last_seen = ? WHERE pr_key = ?", (utcnow(), key))
        return None
    labels = [label["name"] for label in pr.get("labels", [])]
    priority = priority_for(labels, bool(pr.get("draft")))
    if priority is None:
        _mark_seen(key, None, skipped=True)
        return None
    title = f"Review #{pr['number']}: {pr['title']}"
    if len(title) > models.TITLE_MAX:
        title = title[: models.TITLE_MAX - 1] + "…"
    task = models.create_task(
        title,
        describe(pr),
        models.add_business_days(models.today(), config.GITHUB_DUE_BUSINESS_DAYS),
        priority,
        source="github",
        source_ref=key,
        url=pr["html_url"],
    )
    _mark_seen(key, task["id"], skipped=False)
    log.info("Review card #%s for %s", task["id"], key)
    return task


def close_pr_card(key: str, why: str = "") -> dict | None:
    """Mark a PR's review as no longer pending and complete its card. Returns the task if it changed."""
    seen = _seen(key)
    if not seen or not seen["active"]:
        return None
    with transaction() as conn:
        conn.execute("UPDATE seen_github_prs SET active = 0, last_seen = ? WHERE pr_key = ?", (utcnow(), key))
    task = models.get_task(seen["task_id"]) if seen["task_id"] else None
    if task and task["status"] != "done":
        log.info("Closing review card #%s for %s (%s)", task["id"], key, why or "no longer requested")
        return models.complete_task(task["id"])
    return None
