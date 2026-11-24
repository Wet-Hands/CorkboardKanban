"""The print worker: the only process that talks to the printer.

It takes jobs from the SQLite queue one at a time, renders each card fresh
(so a reprint shows current details), and prints it. While the printer is off
or unplugged, jobs simply wait. While idle it sends a keepalive so the printer
doesn't power itself off. It also runs the scheduler.

Run with:  python -m worker.worker
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime

import config
from app import models, printer, render
from app.db import init_db, utcnow

log = logging.getLogger("corkboard.worker")

IDLE_POLL_SECONDS = 1.0
OFFLINE_POLL_SECONDS = 5.0


def render_job(job: dict):
    """Return (image, label) for a job, or (None, reason) if there's nothing to print."""
    if job["kind"] == "digest":
        return render.render_digest(models.digest()), "digest"
    task = models.get_task(job["task_id"]) if job["task_id"] else None
    if task is None:
        return None, "task deleted"
    return render.render_card(task), f"task-{task['id']}"


def process_one(quiet: bool = False) -> str:
    """Handle the next job. Returns 'idle', 'offline', 'printed', 'skipped', 'retry' or 'failed'.

    During quiet hours only manual jobs (if QUIET_HOURS_ALLOW_MANUAL) are printed.
    """
    if not printer.is_available():
        return "offline"
    job = models.claim_next_job(quiet=quiet)
    if job is None:
        return "idle"
    try:
        img, label = render_job(job)
        if img is None:
            models.finish_job(job)
            log.info("Job %s skipped: %s", job["id"], label)
            return "skipped"
        printer.print_image(img, label)
    except printer.PrinterUnavailable as exc:
        models.release_job(job, str(exc))
        log.warning("Job %s waiting: %s", job["id"], exc)
        return "offline"
    except Exception as exc:
        status = models.fail_job(job, f"{type(exc).__name__}: {exc}")
        log.exception("Job %s failed (%s)", job["id"], status)
        return "failed" if status == "failed" else "retry"
    models.finish_job(job)
    last_print.update(at=utcnow(), job_id=job["id"], label=label, reason=job["reason"])
    log.info("Printed job %s (%s, %s)", job["id"], label, job["reason"])
    return "printed"


last_print: dict = {}


class Health:
    """What the worker reports to the worker_status table for the web app's health page."""

    HEARTBEAT_SECONDS = 15
    MAX_DISCONNECTS = 20

    def __init__(self, scheduler=None):
        self.scheduler = scheduler
        self.started_at = utcnow()
        saved = models.get_statuses().get("printer", {}).get("value", {})
        self.disconnects: list[str] = saved.get("disconnects", [])[-self.MAX_DISCONNECTS:]
        self.connected: bool | None = None
        self.connected_since: str | None = None
        self.keepalive_last_sent: str | None = None
        self.keepalive_error: str | None = None
        self.last_beat = 0.0

    def printer_changed(self, connected: bool) -> None:
        now = utcnow()
        if connected:
            self.connected_since = now
        elif self.connected:  # was connected, now gone
            self.disconnects = (self.disconnects + [now])[-self.MAX_DISCONNECTS:]
        self.connected = connected
        self._save_printer()

    def keepalive(self, sent: bool) -> None:
        if sent:
            self.keepalive_last_sent = utcnow()
            self.keepalive_error = None
        else:
            self.keepalive_error = printer.last_keepalive_error

    def _save_printer(self) -> None:
        models.put_status("printer", {
            "connected": self.connected,
            "connected_since": self.connected_since if self.connected else None,
            "disconnects": self.disconnects,
        })

    def beat(self, quiet_until: datetime | None, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_beat < self.HEARTBEAT_SECONDS:
            return
        self.last_beat = time.monotonic()
        next_runs = {}
        if self.scheduler is not None:
            for job in self.scheduler.get_jobs():
                if job.next_run_time:
                    next_runs[job.id] = job.next_run_time.isoformat()
        models.put_status("worker", {
            "started_at": self.started_at,
            "pid": os.getpid(),
            "quiet_until": quiet_until.isoformat() if quiet_until else None,
            "keepalive": {
                "command": config.PRINTER_KEEPALIVE_COMMAND.hex(" "),
                "seconds": config.PRINTER_KEEPALIVE_SECONDS,
                "last_sent": self.keepalive_last_sent,
                "error": self.keepalive_error,
            },
            "last_print": dict(last_print) or None,
            "next_runs": next_runs,
        })


def run(stop: threading.Event, health: Health | None = None) -> None:
    health = health or Health()
    last_keepalive = 0.0
    was_offline = None
    was_quiet = None
    keepalive_ok = None
    while not stop.is_set():
        until = models.quiet_until()
        quiet = until is not None
        if quiet != was_quiet:
            if quiet:
                log.info("Quiet hours until %s: holding %d automatic job(s)", f"{until:%H:%M}", models.held_count())
            elif was_quiet is not None:
                log.info("Quiet hours over")
            was_quiet = quiet
            health.beat(until, force=True)
        result = process_one(quiet)
        offline = result == "offline"
        if offline != was_offline:
            if offline:
                log.warning("Printer not available at %s; jobs will wait", config.PRINTER_DEVICE)
            elif was_offline is not None:
                log.info("Printer is back")
            was_offline = offline
            keepalive_ok = None  # log the first keepalive after each (re)connect
            health.printer_changed(not offline)
        if result == "printed":
            health.beat(until, force=True)
        else:
            health.beat(until)
        if result in ("printed", "skipped"):
            last_keepalive = time.monotonic()
            continue
        if offline:
            stop.wait(OFFLINE_POLL_SECONDS)
            continue
        if config.PRINTER_KEEPALIVE_SECONDS and time.monotonic() - last_keepalive >= config.PRINTER_KEEPALIVE_SECONDS:
            sent = printer.keepalive()
            health.keepalive(sent)
            if sent != keepalive_ok:
                if sent:
                    log.info("Keepalive running: sending %s every %ss",
                             config.PRINTER_KEEPALIVE_COMMAND.hex(" "), config.PRINTER_KEEPALIVE_SECONDS)
                else:
                    log.warning("Keepalive not sent: %s", printer.last_keepalive_error)
                keepalive_ok = sent
            last_keepalive = time.monotonic()
        stop.wait(IDLE_POLL_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    init_db()
    stuck = models.reset_stuck_jobs()
    if stuck:
        log.info("Requeued %d interrupted job(s)", stuck)

    from worker.scheduler import build_scheduler, catch_up

    scheduler = build_scheduler()
    scheduler.start()
    try:
        catch_up()
    except Exception:
        log.exception("Catch-up failed")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    models.parse_quiet_hours(config.QUIET_HOURS)  # fail early on a typo in .env
    log.info("Worker started (printer: %s, timezone: %s)", printer.status()["device"], config.TIMEZONE_NAME)
    try:
        run(stop, Health(scheduler))
    finally:
        scheduler.shutdown(wait=False)
        log.info("Worker stopped")


if __name__ == "__main__":
    main()
