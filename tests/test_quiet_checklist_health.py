import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import config
from app import create_app, health, models, printer, render
from app.db import init_db
from worker import worker

TZ = config.TIMEZONE


def at(hour, minute=0, day=24):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


# --- Quiet hours -----------------------------------------------------------------------

def test_parse_quiet_hours():
    assert models.parse_quiet_hours("") is None
    assert models.parse_quiet_hours("22:00-07:00")[0].hour == 22
    assert models.parse_quiet_hours("9:30 - 9:30") is None
    for bad in ("22-07", "25:00-07:00", "night"):
        with pytest.raises(ValueError):
            models.parse_quiet_hours(bad)


def test_quiet_until_wraps_midnight(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS", "22:00-07:00")
    assert models.quiet_until(at(23, 30)) == at(7, 0, day=25)
    assert models.quiet_until(at(3)) == at(7)
    assert models.quiet_until(at(7)) is None
    assert models.quiet_until(at(12)) is None
    assert models.quiet_until(at(22)) == at(7, 0, day=25)


def test_quiet_until_same_day_window(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS", "13:00-15:00")
    assert models.quiet_until(at(14)) == at(15)
    assert models.quiet_until(at(16)) is None
    monkeypatch.setattr(config, "QUIET_HOURS", "")
    assert models.quiet_until(at(14)) is None


def test_quiet_hours_hold_automatic_but_not_manual_prints(monkeypatch):
    auto = models.create_task("from github", source="github")
    mine = models.create_task("from the web", manual=True)
    assert models.held_count() == 1
    assert worker.process_one(quiet=True) == "printed"
    assert worker.process_one(quiet=True) == "idle"
    statuses = {j["task_id"]: j["status"] for j in models.list_jobs()}
    assert statuses == {mine["id"]: "done", auto["id"]: "pending"}
    assert worker.process_one(quiet=False) == "printed"


def test_quiet_hours_can_hold_everything(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS_ALLOW_MANUAL", False)
    models.create_task("mine", manual=True)
    assert worker.process_one(quiet=True) == "idle"
    assert models.held_count() == 1


def test_manual_request_upgrades_waiting_job():
    task = models.create_task("t")
    assert not models.list_jobs()[0]["manual"]
    models.enqueue_print(task["id"], manual=True)
    (job,) = models.list_jobs()
    assert job["manual"] == 1


def test_worker_loop_logs_quiet_hours(monkeypatch, caplog):
    monkeypatch.setattr(config, "QUIET_HOURS", "00:00-23:59")
    models.create_task("held", source="github")
    stop = threading.Event()
    monkeypatch.setattr(worker, "IDLE_POLL_SECONDS", 0)
    monkeypatch.setattr(stop, "wait", lambda *_: stop.set())
    with caplog.at_level("INFO", logger="corkboard.worker"):
        worker.run(stop)
    assert "holding 1 automatic job(s)" in caplog.text
    assert models.queue_counts() == {"pending": 1}
    assert models.get_statuses()["worker"]["value"]["quiet_until"]


def test_old_database_gets_manual_column(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE print_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    task_id INTEGER, reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, not_before TEXT,
                    created_at TEXT NOT NULL, printed_at TEXT)""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    init_db()
    init_db()  # running twice is fine
    columns = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(print_jobs)")]
    assert "manual" in columns


# --- Checklists ----------------------------------------------------------------------------

DESC = "Before we go:\n- [ ] Sleeping bags\n- [x] Firewood\n* [X] Matches\n[ ] Food\nnot [ ] an item"


def test_checklist_parsing():
    parts = models.checklist(DESC)
    items = [(p["text"], p["checked"]) for p in parts if p["kind"] == "item"]
    assert items == [("Sleeping bags", False), ("Firewood", True), ("Matches", True), ("Food", False)]
    assert models.checklist_progress(DESC) == (2, 4)
    assert models.checklist_progress("no list here") is None


def test_toggle_checklist_item():
    task = models.create_task("Pack", DESC, print_card=False)
    models.toggle_checklist_item(task["id"], 1)
    assert models.checklist_progress(models.get_task(task["id"])["description"]) == (3, 4)
    models.toggle_checklist_item(task["id"], 2)
    description = models.get_task(task["id"])["description"]
    assert "- [ ] Firewood" in description and "- [x] Sleeping bags" in description
    with pytest.raises(ValueError):
        models.toggle_checklist_item(task["id"], 0)  # plain text line
    with pytest.raises(ValueError):
        models.toggle_checklist_item(task["id"], 99)


def _task(**overrides):
    base = {"id": 1, "title": "Pack", "description": "", "due_date": None, "priority": "medium",
            "status": "todo", "recurrence": "none", "source": "web", "url": None}
    return {**base, **overrides}


def test_checklist_renders_and_is_capped():
    plain = render.render_card(_task(description="Before we go:"))
    listed = render.render_card(_task(description=DESC))
    huge = render.render_card(_task(description="\n".join(f"- [ ] item {i}" for i in range(60))))
    assert plain.height < listed.height < huge.height
    assert huge.height < 1100  # capped, with "… N more"


def test_checklist_on_web_pages():
    client = create_app().test_client()
    task = models.create_task("Pack", DESC, print_card=False)
    assert "☑ 2/4" in client.get("/").get_data(as_text=True)
    html = client.get(f"/task/{task['id']}").get_data(as_text=True)
    assert html.count('role="checkbox"') == 4
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    client.post(f"/task/{task['id']}/check/1", data={"csrf_token": token})
    assert models.checklist_progress(models.get_task(task["id"])["description"]) == (3, 4)


# --- Health ----------------------------------------------------------------------------------

def _state(report, title):
    return next(item for item in report["items"] if item["title"] == title)


def test_health_without_worker():
    report = health.gather()
    assert _state(report, "Worker")["state"] == "bad"
    assert report["overall"] == "bad"


def test_health_with_worker_reports(monkeypatch):
    h = worker.Health()
    h.printer_changed(True)
    h.printer_changed(False)
    h.printer_changed(True)
    h.keepalive(True)
    worker.last_print.update(at=datetime.now(timezone.utc).isoformat(), label="task-1", reason="created", job_id=1)
    h.beat(None, force=True)
    models.put_status("github", {"at": datetime.now(timezone.utc).isoformat(), "ok": False, "error": "401"})
    monkeypatch.setattr(config, "GITHUB_TOKEN", "x")
    report = health.gather()
    assert _state(report, "Worker")["state"] == "ok"
    assert _state(report, "Printer")["state"] == "ok"  # dry run
    assert ("Disconnects, last 24 h", "1") in _state(report, "Printer")["details"]
    assert _state(report, "GitHub")["state"] == "bad"
    assert "task-1" in dict(_state(report, "Print queue")["details"])["Last print"]


def test_health_flags_stale_worker_and_missing_printer(fresh_db, monkeypatch):
    worker.Health().beat(None, force=True)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    monkeypatch.setattr(config, "PRINTER_DRY_RUN", False)
    monkeypatch.setattr(config, "PRINTER_DEVICE", str(fresh_db / "missing"))
    report = health.gather(now=later)
    assert _state(report, "Worker")["state"] == "bad"
    assert _state(report, "Printer")["state"] == "bad"


def test_health_page_renders(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS", "00:00-23:59")
    client = create_app().test_client()
    models.create_task("held", source="github")
    page = client.get("/health").get_data(as_text=True)
    assert "Quiet hours" in page and "Raspberry Pi" in page
    assert "held until 23:59" in client.get("/queue").get_data(as_text=True)
    assert "Quiet hours until 23:59" in client.get("/").get_data(as_text=True)


def test_mcp_reports_held_prints(monkeypatch):
    from app import mcp_server

    monkeypatch.setattr(config, "QUIET_HOURS", "00:00-23:59")
    assert mcp_server.create_task("late night idea")["held_until"] == "23:59"
    monkeypatch.setattr(config, "QUIET_HOURS", "")
    assert "held_until" not in mcp_server.create_task("daytime idea")
