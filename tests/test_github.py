import hashlib
import hmac
import json

import pytest

import config
from app import create_app, github, models
from worker import github_poll


def make_pr(number=12, repo="acme/api", title="Fix login", labels=(), draft=False, user="alice"):
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "draft": draft,
        "labels": [{"name": name} for name in labels],
        "user": {"login": user},
        "base": {"repo": {"full_name": repo}},
        "additions": 40,
        "deletions": 3,
        "changed_files": 2,
        "requested_reviewers": [{"login": "me"}],
    }


def search_item(pr):
    repo = pr["base"]["repo"]["full_name"]
    return {"number": pr["number"], "repository_url": f"https://api.github.com/repos/{repo}",
            "draft": pr["draft"]}


class FakeGitHub:
    def __init__(self, prs, incomplete=False):
        self.prs = {f"{p['base']['repo']['full_name']}#{p['number']}": p for p in prs}
        self.incomplete = incomplete

    def __call__(self, path, params=None):
        if path == "/search/issues":
            items = [search_item(p) for p in self.prs.values()]
            return {"total_count": len(items), "incomplete_results": self.incomplete, "items": items}
        repo, number = path.removeprefix("/repos/").split("/pulls/")
        return self.prs[f"{repo}#{number}"]


def test_priority_rules(monkeypatch):
    assert github.priority_for(["Urgent"], False) == "high"
    assert github.priority_for(["hotfix", "docs"], False) == "high"
    assert github.priority_for([], False) == "medium"
    assert github.priority_for([], True) == "low"
    assert github.priority_for(["draft"], False) == "low"
    monkeypatch.setattr(config, "GITHUB_DRAFT_MODE", "skip")
    assert github.priority_for([], True) is None


def test_open_card_once():
    task = github.open_pr_card(make_pr(labels=["urgent"]))
    assert task["title"] == "Review #12: Fix login"
    assert task["description"] == "acme/api · by @alice · +40 −3 · 2 files"
    assert task["priority"] == "high" and task["source"] == "github"
    assert task["url"] == "https://github.com/acme/api/pull/12"
    assert task["due_date"] == models.add_business_days(models.today(), 1).isoformat()
    assert github.open_pr_card(make_pr()) is None
    assert len(models.list_tasks()) == 1


def test_poll_opens_and_closes(monkeypatch):
    fake = FakeGitHub([make_pr(1), make_pr(2)])
    monkeypatch.setattr(github, "api_get", fake)
    assert github_poll.poll() == {"found": 2, "opened": 2, "closed": 0}
    assert github_poll.poll()["opened"] == 0
    del fake.prs["acme/api#1"]
    assert github_poll.poll()["closed"] == 1
    statuses = {t["source_ref"]: t["status"] for t in models.list_tasks()}
    assert statuses == {"acme/api#1": "done", "acme/api#2": "todo"}
    # Review requested again later: a fresh card.
    fake.prs["acme/api#1"] = make_pr(1)
    assert github_poll.poll()["opened"] == 1
    assert len(models.list_tasks()) == 3


def test_poll_does_not_close_on_incomplete_results(monkeypatch):
    fake = FakeGitHub([make_pr(1)])
    monkeypatch.setattr(github, "api_get", fake)
    github_poll.poll()
    fake.prs.clear()
    fake.incomplete = True
    assert github_poll.poll()["closed"] == 0


def test_skipped_draft_gets_card_when_ready(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_DRAFT_MODE", "skip")
    fake = FakeGitHub([make_pr(5, draft=True)])
    monkeypatch.setattr(github, "api_get", fake)
    assert github_poll.poll()["opened"] == 0
    fake.prs["acme/api#5"]["draft"] = False
    assert github_poll.poll()["opened"] == 1


# --- Webhook ------------------------------------------------------------------------

SECRET = "hook-secret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "GITHUB_USERNAME", "Me")
    github.my_login.cache_clear()
    return create_app().test_client()


def send(client, event, payload, delivery="d1", secret=SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/github", data=body, content_type="application/json", headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": sig})


def test_webhook_rejects_bad_signature(client):
    assert send(client, "ping", {}, secret="wrong").status_code == 401


def test_webhook_disabled_without_secret(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", "")
    assert create_app().test_client().post("/github", data=b"{}").status_code == 404


def test_webhook_review_flow(client):
    pr = make_pr(9)
    payload = {"action": "review_requested", "pull_request": pr, "requested_reviewer": {"login": "me"}}
    assert send(client, "pull_request", payload).json["result"] == "card created"
    assert send(client, "pull_request", payload).json["result"] == "duplicate delivery"
    assert send(client, "pull_request", payload, delivery="d2").json["result"] == "already on the board"
    other = {"action": "review_requested", "pull_request": make_pr(10), "requested_reviewer": {"login": "bob"}}
    assert send(client, "pull_request", other, delivery="d3").json["result"] == "ignored"
    review = {"action": "submitted", "pull_request": pr, "review": {"user": {"login": "ME"}}}
    assert send(client, "pull_request_review", review, delivery="d4").json["result"] == "closed"
    (task,) = models.list_tasks()
    assert task["status"] == "done"


def test_webhook_closed_pr(client):
    pr = make_pr(3)
    send(client, "pull_request", {"action": "review_requested", "pull_request": pr,
                                  "requested_reviewer": {"login": "me"}}, delivery="a")
    send(client, "pull_request", {"action": "closed", "pull_request": {**pr, "merged": True}}, delivery="b")
    assert models.list_tasks()[0]["status"] == "done"
