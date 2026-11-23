"""Every few minutes, find open PRs waiting on your review and keep the cards in step.

New review requests get a card; PRs that drop out of the search (reviewed,
merged, closed, or the request was removed) have their card marked done.
"""

from __future__ import annotations

import logging
import re

import config
from app import github

log = logging.getLogger(__name__)
MAX_PAGES = 10


def _repo_from_url(repository_url: str) -> str:
    match = re.search(r"/repos/([^/]+/[^/]+)$", repository_url)
    if not match:
        raise github.GitHubError(f"Unexpected repository_url {repository_url}")
    return match.group(1)


def search_review_requests() -> list[dict] | None:
    """All search hits, or None if the results may be incomplete (then nothing gets closed)."""
    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = github.api_get(
            "/search/issues",
            {"q": config.GITHUB_SEARCH_QUERY, "per_page": 100, "page": page},
        )
        items.extend(data.get("items", []))
        if data.get("incomplete_results"):
            log.warning("GitHub search returned incomplete results; not closing any cards this time")
            return None
        if len(items) >= data.get("total_count", 0) or not data.get("items"):
            return items
    return None


def poll() -> dict:
    items = search_review_requests()
    found: dict[str, dict] = {}
    for item in items or []:
        repo = _repo_from_url(item["repository_url"])
        found[github.pr_key(repo, item["number"])] = {**item, "repo": repo}

    active = github.active_keys()
    opened = closed = 0
    for key, item in found.items():
        seen = active.get(key)
        if seen and not seen["skipped"]:
            continue
        if seen and seen["skipped"] and item.get("draft"):
            continue  # still a draft; nothing changed
        try:
            pr = github.api_get(f"/repos/{item['repo']}/pulls/{item['number']}")
        except github.GitHubError:
            log.exception("Couldn't fetch %s", key)
            continue
        if github.open_pr_card(pr):
            opened += 1

    if items is not None:
        for key in set(active) - set(found):
            if github.close_pr_card(key, "review no longer requested"):
                closed += 1

    if opened or closed:
        log.info("GitHub poll: %d new review card(s), %d closed", opened, closed)
    return {"found": len(found), "opened": opened, "closed": closed}
