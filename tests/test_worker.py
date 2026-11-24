from datetime import timedelta

import config
from app import models, printer
from app.db import transaction
from worker import scheduler, worker


def test_prints_card_in_dry_run(fresh_db):
    task = models.create_task("Print me")
    assert worker.process_one() == "printed"
    assert worker.process_one() == "idle"
    files = list((fresh_db / "printed").glob("*.png"))
    assert len(files) == 1 and f"task-{task['id']}" in files[0].name
    assert models.get_task(task["id"])["last_printed_at"]


def test_jobs_wait_while_printer_is_off(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "PRINTER_DRY_RUN", False)
    monkeypatch.setattr(config, "PRINTER_DEVICE", str(fresh_db / "lp0"))
    models.create_task("Wait for me")
    assert worker.process_one() == "offline"
    assert models.queue_counts() == {"pending": 1}
    assert printer.keepalive() is False
    # Printer comes back: the device node reappears.
    (fresh_db / "lp0").write_bytes(b"")
    assert printer.keepalive() is True
    assert (fresh_db / "lp0").read_bytes() == printer.KEEPALIVE
    assert worker.process_one() == "printed"
    data = (fresh_db / "lp0").read_bytes()
    assert data.startswith(b"\x1dv0") and len(data) > 1000  # GS v 0 raster image


def test_disconnect_mid_print_releases_job(monkeypatch):
    models.create_task("t")

    def unplugged(img, label):
        raise printer.PrinterUnavailable("gone")

    monkeypatch.setattr(printer, "print_image", unplugged)
    assert worker.process_one() == "offline"
    job = models.list_jobs()[0]
    assert job["status"] == "pending" and job["attempts"] == 0


def test_print_errors_retry_then_fail(monkeypatch):
    monkeypatch.setattr(config, "PRINT_MAX_ATTEMPTS", 2)
    models.create_task("t")

    def broken(img, label):
        raise printer.PrintError("jam")

    monkeypatch.setattr(printer, "print_image", broken)
    assert worker.process_one() == "retry"
    with transaction() as conn:
        conn.execute("UPDATE print_jobs SET not_before = NULL")
    assert worker.process_one() == "failed"
    assert models.list_jobs()[0]["last_error"] == "PrintError: jam"


def test_deleted_task_job_is_skipped():
    task = models.create_task("t")
    job = models.claim_next_job()
    models.delete_task(task["id"])
    models.reset_stuck_jobs()
    assert worker.process_one() == "idle"  # job cascaded away with the task
    assert job["task_id"] == task["id"]


def test_digest_job(fresh_db):
    models.enqueue_digest()
    assert worker.process_one() == "printed"
    assert list((fresh_db / "printed").glob("*digest.png"))


def test_reminders_once_per_due_date(today):
    soon = models.create_task("soon", due_date=today + timedelta(days=1), priority="high", print_card=False)
    models.create_task("later", due_date=today + timedelta(days=5), priority="high", print_card=False)
    models.create_task("medium", due_date=today, priority="medium", print_card=False)
    assert scheduler.remind_due_soon() == 1
    assert scheduler.remind_due_soon() == 0
    jobs = models.list_jobs()
    assert [(j["task_id"], j["reason"]) for j in jobs] == [(soon["id"], "reminder")]


def test_reminder_skipped_when_just_printed(today):
    models.create_task("fresh", due_date=today, priority="high")
    job = models.claim_next_job()
    models.finish_job(job)
    assert scheduler.remind_due_soon() == 0


def test_overdue_reprint(today):
    late = models.create_task("late", due_date=today - timedelta(days=3), print_card=False)
    models.create_task("fine", due_date=today + timedelta(days=3), print_card=False)
    assert scheduler.reprint_overdue() == 1
    assert models.list_jobs()[0]["task_id"] == late["id"]


def test_scheduler_builds(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "x")
    sched = scheduler.build_scheduler()
    assert {job.id for job in sched.get_jobs()} == {"recurring", "digest", "reminders", "overdue", "github"}


def test_keepalive_command_is_configurable(fresh_db, monkeypatch):
    device = fresh_db / "lp0"
    monkeypatch.setattr(config, "PRINTER_DRY_RUN", False)
    monkeypatch.setattr(config, "PRINTER_DEVICE", str(device))
    monkeypatch.setattr(config, "PRINTER_KEEPALIVE_COMMAND", bytes.fromhex("1b40"))
    assert printer.keepalive() is False
    assert printer.last_keepalive_error == "printer not connected"
    device.write_bytes(b"")
    assert printer.keepalive() is True and printer.last_keepalive_error is None
    assert device.read_bytes() == b"\x1b\x40"
    monkeypatch.setattr(config, "PRINTER_KEEPALIVE_COMMAND", b"")
    assert printer.keepalive() is False  # empty command turns it off


def test_worker_logs_keepalive(fresh_db, monkeypatch, caplog):
    import threading

    device = fresh_db / "lp0"
    device.write_bytes(b"")
    monkeypatch.setattr(config, "PRINTER_DRY_RUN", False)
    monkeypatch.setattr(config, "PRINTER_DEVICE", str(device))
    stop = threading.Event()
    calls = []

    def fake_keepalive():
        calls.append(1)
        stop.set()
        return True

    monkeypatch.setattr(printer, "keepalive", fake_keepalive)
    with caplog.at_level("INFO", logger="corkboard.worker"):
        worker.run(stop)
    assert calls and "Keepalive running: sending 10 04 01" in caplog.text
