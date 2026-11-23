"""The print worker: the only process that talks to the printer.

It takes jobs from the SQLite queue one at a time, renders each card fresh
(so a reprint shows current details), and prints it. While the printer is off
or unplugged, jobs simply wait. While idle it sends a keepalive so the printer
doesn't power itself off. It also runs the scheduler.

Run with:  python -m worker.worker
"""

from __future__ import annotations

import logging
import signal
import threading
import time

import config
from app import models, printer, render
from app.db import init_db

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


def process_one() -> str:
    """Handle the next job. Returns 'idle', 'offline', 'printed', 'skipped', 'retry' or 'failed'."""
    if not printer.is_available():
        return "offline"
    job = models.claim_next_job()
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
    log.info("Printed job %s (%s, %s)", job["id"], label, job["reason"])
    return "printed"


def run(stop: threading.Event) -> None:
    last_keepalive = 0.0
    was_offline = None
    while not stop.is_set():
        result = process_one()
        offline = result == "offline"
        if offline != was_offline:
            if offline:
                log.warning("Printer not available at %s; jobs will wait", config.PRINTER_DEVICE)
            elif was_offline is not None:
                log.info("Printer is back")
            was_offline = offline
        if result in ("printed", "skipped"):
            last_keepalive = time.monotonic()
            continue
        if offline:
            stop.wait(OFFLINE_POLL_SECONDS)
            continue
        if config.PRINTER_KEEPALIVE_SECONDS and time.monotonic() - last_keepalive >= config.PRINTER_KEEPALIVE_SECONDS:
            printer.keepalive()
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
    log.info("Worker started (printer: %s, timezone: %s)", printer.status()["device"], config.TIMEZONE_NAME)
    try:
        run(stop)
    finally:
        scheduler.shutdown(wait=False)
        log.info("Worker stopped")


if __name__ == "__main__":
    main()
