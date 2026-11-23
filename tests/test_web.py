import re

import pytest

import config
from app import create_app, models


@pytest.fixture
def client():
    return create_app().test_client()


def csrf(client, path="/task/new"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_board_and_pages_render(client):
    task = models.create_task("Shown", "desc", "today", "high", print_card=False)
    for path in ("/", "/task/new", f"/task/{task['id']}", f"/task/{task['id']}/edit", "/queue"):
        response = client.get(path)
        assert response.status_code == 200, path
    assert "Shown" in client.get("/").get_data(as_text=True)
    assert client.get("/task/999").status_code == 404


def test_create_task_from_form(client):
    token = csrf(client)
    response = client.post("/task/new", data={
        "csrf_token": token, "title": "From phone", "description": "", "due_date": "2026-12-24",
        "priority": "low", "recurrence": "none", "print_card": "1"})
    assert response.status_code == 302
    (task,) = models.list_tasks()
    assert (task["title"], task["priority"], task["due_date"]) == ("From phone", "low", "2026-12-24")
    assert models.queue_counts() == {"pending": 1}


def test_form_errors_are_shown(client):
    token = csrf(client)
    response = client.post("/task/new", data={"csrf_token": token, "title": " ", "priority": "low"})
    assert response.status_code == 400 and "needs a title" in response.get_data(as_text=True)


def test_post_without_csrf_is_rejected(client):
    assert client.post("/task/new", data={"title": "x"}).status_code == 400


def test_status_print_delete(client):
    task = models.create_task("Move me", print_card=False)
    token = csrf(client)
    client.post(f"/task/{task['id']}/status", data={"csrf_token": token, "status": "doing"})
    assert models.get_task(task["id"])["status"] == "doing"
    client.post(f"/task/{task['id']}/print", data={"csrf_token": token})
    assert models.queue_counts() == {"pending": 1}
    client.post(f"/task/{task['id']}/delete", data={"csrf_token": token})
    assert models.get_task(task["id"]) is None


def test_edit(client):
    task = models.create_task("Old", print_card=False)
    token = csrf(client)
    client.post(f"/task/{task['id']}/edit", data={
        "csrf_token": token, "title": "New", "description": "d", "due_date": "", "priority": "high",
        "recurrence": "weekly"})
    updated = models.get_task(task["id"])
    assert (updated["title"], updated["priority"], updated["recurrence"]) == ("New", "high", "weekly")


def test_pngs(client):
    task = models.create_task("Card", print_card=False)
    for path in (f"/task/{task['id']}/card.png", "/preview.png?title=Hi&priority=high&due_date=tomorrow",
                 "/digest.png"):
        response = client.get(path)
        assert response.status_code == 200 and response.data.startswith(b"\x89PNG"), path


def test_queue_retry_and_digest(client):
    token = csrf(client)
    client.post("/digest/print", data={"csrf_token": token})
    assert models.list_jobs()[0]["kind"] == "digest"


def test_basic_auth(monkeypatch):
    monkeypatch.setattr(config, "WEB_USERNAME", "me")
    monkeypatch.setattr(config, "WEB_PASSWORD", "pw")
    client = create_app().test_client()
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Authorization": "Basic bWU6cHc="}).status_code == 200
    assert client.get("/healthz").status_code == 200
