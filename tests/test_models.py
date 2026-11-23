from datetime import date, datetime, timedelta, timezone

import pytest

import config
from app import models
from app.db import transaction

MON = date(2026, 11, 23)  # a Monday


def test_parse_due_date_forms():
    assert models.parse_due_date("2026-12-01", MON) == "2026-12-01"
    assert models.parse_due_date("today", MON) == "2026-11-23"
    assert models.parse_due_date("Tomorrow", MON) == "2026-11-24"
    assert models.parse_due_date("fri", MON) == "2026-11-27"
    assert models.parse_due_date("monday", MON) == "2026-11-23"
    assert models.parse_due_date("next monday", MON) == "2026-11-30"
    assert models.parse_due_date("+3d", MON) == "2026-11-26"
    assert models.parse_due_date("2w", MON) == "2026-12-07"
    assert models.parse_due_date("", MON) is None
    assert models.parse_due_date(None, MON) is None
    with pytest.raises(ValueError):
        models.parse_due_date("someday", MON)


def test_business_days_and_recurrence():
    friday = date(2026, 11, 27)
    assert models.add_business_days(friday, 1) == date(2026, 11, 30)
    assert models.add_business_days(MON, 1) == date(2026, 11, 24)
    assert models.next_occurrence(friday, "weekdays") == date(2026, 11, 30)
    assert models.next_occurrence(date(2026, 1, 31), "monthly") == date(2026, 2, 28)
    assert models.next_occurrence(date(2026, 12, 15), "monthly") == date(2027, 1, 15)
    assert models.next_occurrence(MON, "weekly") == date(2026, 11, 30)


def test_create_task_queues_a_print():
    task = models.create_task("  Buy   milk ", "2%", "2026-12-01", "high")
    assert task["title"] == "Buy milk"
    assert task["status"] == "todo" and task["source"] == "web"
    jobs = models.list_jobs()
    assert len(jobs) == 1 and jobs[0]["task_id"] == task["id"] and jobs[0]["reason"] == "created"


def test_create_task_without_print_and_validation():
    models.create_task("Quiet", print_card=False)
    assert models.list_jobs() == []
    with pytest.raises(ValueError):
        models.create_task("   ")
    with pytest.raises(ValueError):
        models.create_task("x", priority="urgent")
    with pytest.raises(ValueError):
        models.create_task("x" * 201)


def test_status_changes_and_ordering():
    low = models.create_task("low", priority="low", print_card=False)
    high = models.create_task("high", priority="high", print_card=False)
    models.set_status(low["id"], "doing")
    done = models.complete_task(high["id"])
    assert done["completed_at"]
    board = models.board()
    assert [t["id"] for t in board["doing"]] == [low["id"]]
    assert [t["id"] for t in board["done"]] == [high["id"]]
    reopened = models.set_status(high["id"], "todo")
    assert reopened["completed_at"] is None
    assert [t["title"] for t in models.list_tasks(include_done=False)] == ["high", "low"]


def test_update_task_resets_reminder_when_due_changes():
    task = models.create_task("t", due_date="2026-12-01", print_card=False)
    with transaction() as conn:
        conn.execute("UPDATE tasks SET reminded_for = due_date WHERE id = ?", (task["id"],))
    updated = models.update_task(task["id"], due_date="2026-12-05", priority="low")
    assert updated["reminded_for"] is None and updated["priority"] == "low"
    with pytest.raises(ValueError):
        models.update_task(task["id"], source="github")
    with pytest.raises(LookupError):
        models.update_task(999, title="nope")


def test_enqueue_dedupes_pending_jobs():
    task = models.create_task("t")
    again = models.enqueue_print(task["id"])
    assert len(models.list_jobs()) == 1 and again["reason"] == "created"
    models.enqueue_digest()
    models.enqueue_digest()
    assert sum(j["kind"] == "digest" for j in models.list_jobs()) == 1


def test_job_lifecycle_and_backoff(monkeypatch):
    monkeypatch.setattr(config, "PRINT_MAX_ATTEMPTS", 2)
    task = models.create_task("t")
    job = models.claim_next_job()
    assert job["status"] == "printing" and models.claim_next_job() is None
    assert models.fail_job(job, "boom") == "pending"
    assert models.claim_next_job() is None  # backing off
    with transaction() as conn:
        conn.execute("UPDATE print_jobs SET not_before = NULL")
    job = models.claim_next_job()
    assert models.fail_job(job, "boom") == "failed"
    models.retry_job(job["id"])
    job = models.claim_next_job()
    models.finish_job(job)
    assert models.get_task(task["id"])["last_printed_at"]
    assert models.queue_counts() == {"done": 1}


def test_release_and_reset_stuck():
    models.create_task("t")
    job = models.claim_next_job()
    models.release_job(job, "offline")
    job = models.claim_next_job()
    assert job["attempts"] == 0
    assert models.reset_stuck_jobs() == 1


def test_spawn_recurring(today):
    task = models.create_task("Water plants", due_date=today - timedelta(days=1), recurrence="daily",
                              print_card=False)
    assert models.spawn_recurring(today) == []  # not done yet
    models.complete_task(task["id"])
    created = models.spawn_recurring(today)
    assert len(created) == 1
    new = created[0]
    assert new["due_date"] == today.isoformat() and new["source"] == "recurring" and new["recurrence"] == "daily"
    assert models.spawn_recurring(today) == []  # only once
    weekly = models.create_task("Bins", due_date=today, recurrence="weekly", print_card=False)
    models.complete_task(weekly["id"])
    assert models.spawn_recurring(today) == []  # next one is a week out
    assert len(models.spawn_recurring(today + timedelta(days=7))) == 1


def test_digest_groups(today):
    models.create_task("late", due_date=today - timedelta(days=2), print_card=False)
    models.create_task("now", due_date=today, print_card=False)
    doing = models.create_task("busy", print_card=False)
    models.set_status(doing["id"], "doing")
    models.create_task("big", priority="high", print_card=False)
    finished = models.create_task("old", print_card=False)
    models.complete_task(finished["id"])
    yesterday = (datetime.now(config.TIMEZONE) - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    with transaction() as conn:
        conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (yesterday, finished["id"]))
    data = models.digest(today)
    assert [t["title"] for t in data["overdue"]] == ["late"]
    assert [t["title"] for t in data["today"]] == ["now"]
    assert [t["title"] for t in data["doing"]] == ["busy"]
    assert [t["title"] for t in data["high"]] == ["big"]
    assert data["counts"] == {"todo": 3, "doing": 1, "done_yesterday": 1}


def test_due_state(today):
    assert models.due_state({"due_date": (today - timedelta(days=1)).isoformat(), "status": "todo"}, today) == "overdue"
    assert models.due_state({"due_date": today.isoformat(), "status": "todo"}, today) == "today"
    assert models.due_state({"due_date": (today + timedelta(days=1)).isoformat(), "status": "doing"}, today) == "tomorrow"
    assert models.due_state({"due_date": today.isoformat(), "status": "done"}, today) is None
    assert models.due_state({"due_date": None, "status": "todo"}, today) is None
