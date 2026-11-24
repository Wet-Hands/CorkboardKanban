"""Timed automations: morning digest, recurring tasks, due-soon reminders,
weekly overdue reprint and the GitHub poll. Runs inside the worker process."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from app import models
from app.db import get_conn, transaction

log = logging.getLogger(__name__)


def _hour_minute(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


# --- Automations (plain functions so they can be run and tested directly) ------------

def spawn_recurring() -> int:
    created = models.spawn_recurring()
    for task in created:
        log.info("Recurring task #%s created for %s", task["id"], task["due_date"])
    return len(created)


def morning_digest() -> None:
    job = models.enqueue_digest()
    log.info("Morning digest queued (job %s)", job["id"])


def remind_due_soon() -> int:
    """Reprint open tasks of the reminder priorities that are due today or tomorrow, once per due date."""
    ref = models.today()
    window = {ref.isoformat(), (ref + timedelta(days=1)).isoformat()}
    recent = (datetime.now(timezone.utc) - timedelta(hours=12)).replace(microsecond=0).isoformat()
    count = 0
    for task in models.list_tasks(include_done=False):
        if task["priority"] not in config.REMINDER_PRIORITIES or task["due_date"] not in window:
            continue
        if task["reminded_for"] == task["due_date"]:
            continue
        with transaction() as conn:
            conn.execute("UPDATE tasks SET reminded_for = ? WHERE id = ?", (task["due_date"], task["id"]))
        if task["last_printed_at"] and task["last_printed_at"] >= recent:
            continue  # its card came out in the last 12 hours already
        models.enqueue_print(task["id"], "reminder")
        count += 1
    if count:
        log.info("Queued %d due-soon reminder(s)", count)
    return count


def reprint_overdue() -> int:
    ref = models.today().isoformat()
    overdue = [t for t in models.list_tasks(include_done=False) if t["due_date"] and t["due_date"] < ref]
    for task in overdue:
        models.enqueue_print(task["id"], "overdue")
    if overdue:
        log.info("Queued %d overdue reprint(s)", len(overdue))
    return len(overdue)


def poll_github() -> None:
    from worker.github_poll import poll

    from app.db import utcnow

    try:
        result = poll()
    except Exception as exc:
        log.exception("GitHub poll failed")
        models.put_status("github", {"at": utcnow(), "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
    else:
        models.put_status("github", {"at": utcnow(), "ok": True, **result})


def catch_up() -> None:
    """Run the morning jobs if the worker (re)started after their time today."""
    now = datetime.now(config.TIMEZONE)
    hour, minute = _hour_minute(config.DIGEST_TIME)
    digest_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < digest_at:
        return
    spawn_recurring()
    if not config.DIGEST_ENABLED or now - digest_at > timedelta(hours=2):
        return
    trigger = CronTrigger(day_of_week=config.DIGEST_DAYS, hour=hour, minute=minute, timezone=config.TIMEZONE)
    if trigger.get_next_fire_time(None, digest_at - timedelta(seconds=1)) != digest_at:
        return  # not a digest day
    since = digest_at.astimezone(timezone.utc).isoformat()
    with get_conn() as conn:
        already = conn.execute(
            "SELECT 1 FROM print_jobs WHERE kind = 'digest' AND created_at >= ?", (since,)
        ).fetchone()
    if not already:
        log.info("Catching up on this morning's digest")
        morning_digest()


# --- Scheduler -------------------------------------------------------------------------

def build_scheduler() -> BackgroundScheduler:
    tz = config.TIMEZONE
    scheduler = BackgroundScheduler(
        timezone=tz, job_defaults={"coalesce": True, "misfire_grace_time": 3600, "max_instances": 1}
    )
    hour, minute = _hour_minute(config.DIGEST_TIME)
    scheduler.add_job(spawn_recurring, CronTrigger(hour=hour, minute=minute, timezone=tz), id="recurring")
    if config.DIGEST_ENABLED:
        scheduler.add_job(
            morning_digest,
            CronTrigger(day_of_week=config.DIGEST_DAYS, hour=hour, minute=minute, second=30, timezone=tz),
            id="digest",
        )
    if config.REMINDER_ENABLED:
        r_hour, r_minute = _hour_minute(config.REMINDER_TIME)
        scheduler.add_job(remind_due_soon, CronTrigger(hour=r_hour, minute=r_minute, timezone=tz), id="reminders")
    if config.OVERDUE_REPRINT_ENABLED:
        o_hour, o_minute = _hour_minute(config.OVERDUE_REPRINT_TIME)
        scheduler.add_job(
            reprint_overdue,
            CronTrigger(day_of_week=config.OVERDUE_REPRINT_DAY, hour=o_hour, minute=o_minute, timezone=tz),
            id="overdue",
        )
    if config.GITHUB_TOKEN:
        scheduler.add_job(
            poll_github,
            IntervalTrigger(minutes=config.GITHUB_POLL_MINUTES, timezone=tz),
            id="github",
            next_run_time=datetime.now(tz) + timedelta(seconds=10),
        )
    else:
        log.info("GITHUB_TOKEN not set: GitHub poll disabled")
    return scheduler
