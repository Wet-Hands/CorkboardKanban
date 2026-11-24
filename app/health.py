"""Everything the /health page shows, gathered in one place.

Most of it is reported by the worker (heartbeat, printer connections, keepalive,
last print, scheduler, GitHub poll) through the worker_status table; the rest is
checked live (printer device, queue, disk, Pi temperature and power).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import config
from app import models, printer

HEARTBEAT_STALE_SECONDS = 60
JOB_LABELS = {
    "digest": "Morning digest",
    "recurring": "Recurring tasks",
    "reminders": "Due-soon reminders",
    "overdue": "Overdue reprint",
    "github": "GitHub poll",
}
THROTTLE_FLAGS = [
    (0x1, "under-voltage now"),
    (0x2, "CPU speed capped now"),
    (0x4, "throttled now"),
    (0x8, "temperature limit now"),
    (0x10000, "under-voltage since boot"),
    (0x20000, "CPU speed capped since boot"),
    (0x40000, "throttled since boot"),
    (0x80000, "temperature limit since boot"),
]


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    value = datetime.fromisoformat(iso)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def ago(iso: str | None, now: datetime | None = None) -> str:
    value = _parse(iso)
    if value is None:
        return "never"
    seconds = int(((now or datetime.now(timezone.utc)) - value).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"
    return f"{seconds // 86400} d ago"


def duration(iso: str | None, now: datetime | None = None) -> str:
    text = ago(iso, now)
    return text.removesuffix(" ago") if text not in ("never", "just now") else text


def local(iso: str | None) -> str:
    value = _parse(iso)
    if value is None:
        return "—"
    value = value.astimezone(config.TIMEZONE)
    today = datetime.now(config.TIMEZONE).date()
    if value.date() == today:
        return f"today {value:%H:%M}"
    if value.date() == today + timedelta(days=1):
        return f"tomorrow {value:%H:%M}"
    return f"{value:%a %b} {value.day} {value:%H:%M}"


def _item(title: str, state: str, summary: str, details: list[tuple[str, str]] | None = None) -> dict:
    """state is ok, warn, bad or off."""
    return {"title": title, "state": state, "summary": summary, "details": details or []}


def _pi_temperature() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return int(fh.read().strip()) / 1000
    except (OSError, ValueError):
        return None


def _pi_throttled() -> int | None:
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return None


def gather(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    statuses = models.get_statuses()
    worker = statuses.get("worker")
    worker_value = worker["value"] if worker else {}
    items = []

    # Worker
    if worker is None:
        items.append(_item("Worker", "bad", "Has never reported in. Is corkboard-worker running?"))
    else:
        age = (now - _parse(worker["updated_at"])).total_seconds()
        if age > HEARTBEAT_STALE_SECONDS:
            items.append(_item("Worker", "bad", f"Last heard from {ago(worker['updated_at'], now)}. "
                               "Check: systemctl status corkboard-worker"))
        else:
            items.append(_item("Worker", "ok", f"Running for {duration(worker_value.get('started_at'), now)}",
                               [("Process", str(worker_value.get("pid", "?")))]))

    # Printer
    live = printer.status()
    saved = statuses.get("printer", {}).get("value", {})
    disconnects = [d for d in saved.get("disconnects", []) if (now - _parse(d)) <= timedelta(days=1)]
    details = [("Device", live["device"])]
    if saved.get("connected_since") and live["available"]:
        details.append(("Connected for", duration(saved["connected_since"], now)))
    details.append(("Disconnects, last 24 h", str(len(disconnects))))
    if saved.get("disconnects"):
        details.append(("Last disconnect", f"{local(saved['disconnects'][-1])} ({ago(saved['disconnects'][-1], now)})"))
    if config.PRINTER_DRY_RUN:
        items.append(_item("Printer", "ok", "Dry run: cards are saved as PNGs", details))
    elif not live["available"]:
        items.append(_item("Printer", "bad", "Not connected. Switch it on or replug it.", details))
    elif not live["writable"]:
        items.append(_item("Printer", "bad", "Connected, but no permission to write to it (see README).", details))
    else:
        state = "warn" if len(disconnects) >= 3 else "ok"
        summary = "Connected" + (f", but dropped off {len(disconnects)} times today" if state == "warn" else "")
        items.append(_item("Printer", state, summary, details))

    # Keepalive
    keepalive = worker_value.get("keepalive") or {}
    if config.PRINTER_DRY_RUN or not config.PRINTER_KEEPALIVE_SECONDS or not config.PRINTER_KEEPALIVE_COMMAND:
        items.append(_item("Keepalive", "off", "Off"))
    else:
        details = [("Sends", f"{config.PRINTER_KEEPALIVE_COMMAND.hex(' ')} every {config.PRINTER_KEEPALIVE_SECONDS} s"),
                   ("Last sent", ago(keepalive.get("last_sent"), now))]
        if keepalive.get("error") and live["available"]:
            items.append(_item("Keepalive", "warn", f"Not sending: {keepalive['error']}", details))
        elif keepalive.get("last_sent"):
            items.append(_item("Keepalive", "ok", "Sending while the printer is idle", details))
        else:
            items.append(_item("Keepalive", "off", "Waiting for the printer to connect", details))

    # Print queue
    counts = models.queue_counts()
    pending, failed = counts.get("pending", 0), counts.get("failed", 0)
    until = models.quiet_until()
    held = models.held_count() if until else 0
    last_print = worker_value.get("last_print")
    details = [("Waiting", str(pending)), ("Failed", str(failed)), ("Printed", str(counts.get("done", 0)))]
    if last_print:
        details.append(("Last print", f"{last_print.get('label')} ({last_print.get('reason')}), {ago(last_print.get('at'), now)}"))
    if failed:
        items.append(_item("Print queue", "warn", f"{failed} failed job(s): retry them on the Queue page", details))
    elif pending and not live["available"]:
        items.append(_item("Print queue", "warn", f"{pending} job(s) waiting for the printer", details))
    elif held:
        items.append(_item("Print queue", "ok", f"{held} job(s) held for quiet hours", details))
    else:
        items.append(_item("Print queue", "ok", "Nothing waiting" if not pending else f"{pending} printing soon", details))

    # Quiet hours
    if not config.QUIET_HOURS:
        items.append(_item("Quiet hours", "off", "Off (set QUIET_HOURS, e.g. 22:00-07:00)"))
    else:
        details = [("Hours", config.QUIET_HOURS),
                   ("Your own prints", "still print" if config.QUIET_HOURS_ALLOW_MANUAL else "wait too")]
        if until:
            items.append(_item("Quiet hours", "ok", f"On until {until:%H:%M}, holding {held} job(s)", details))
        else:
            items.append(_item("Quiet hours", "ok", "Not now", details))

    # Schedule
    next_runs = worker_value.get("next_runs") or {}
    if next_runs:
        details = [(JOB_LABELS.get(job_id, job_id), local(when)) for job_id, when in sorted(next_runs.items(), key=lambda kv: kv[1])]
        items.append(_item("Schedule", "ok", "Next automatic runs", details))
    else:
        items.append(_item("Schedule", "off", "No schedule reported yet"))

    # GitHub
    github = statuses.get("github")
    if not config.GITHUB_TOKEN:
        items.append(_item("GitHub", "off", "Off (no GITHUB_TOKEN)"))
    elif github is None:
        items.append(_item("GitHub", "off", "Hasn't polled yet"))
    else:
        value = github["value"]
        if value.get("ok"):
            items.append(_item("GitHub", "ok", f"Last poll {ago(value['at'], now)}", [
                ("Review requests open", str(value.get("found", 0))),
                ("New cards / closed", f"{value.get('opened', 0)} / {value.get('closed', 0)}")]))
        else:
            items.append(_item("GitHub", "bad", f"Last poll failed {ago(value['at'], now)}",
                               [("Error", value.get("error", "?"))]))

    # The Pi itself
    details = []
    state, summary = "ok", "OK"
    temp = _pi_temperature()
    if temp is not None:
        details.append(("CPU temperature", f"{temp:.0f} °C"))
        if temp >= 80:
            state, summary = "warn", "Running hot"
    throttled = _pi_throttled()
    if throttled is not None:
        flags = [label for bit, label in THROTTLE_FLAGS if throttled & bit]
        details.append(("Power", ", ".join(flags) if flags else "no under-voltage"))
        if throttled & 0x1:
            state, summary = "bad", "Under-voltage: the power supply is too weak (it can drop the printer)"
        elif throttled & 0x10000:
            state, summary = "warn", "Under-voltage has happened since boot"
    try:
        disk = shutil.disk_usage(config.DATA_DIR)
        free_pct = disk.free / disk.total * 100
        details.append(("Disk free", f"{disk.free / 1e9:.1f} GB ({free_pct:.0f}%)"))
        if free_pct < 5:
            state, summary = "bad", "Disk almost full"
    except OSError:
        pass
    if config.DB_PATH.exists():
        details.append(("Database", f"{os.path.getsize(config.DB_PATH) / 1e6:.1f} MB"))
    items.append(_item("Raspberry Pi", state, summary, details))

    worst = "ok"
    for item in items:
        if item["state"] == "bad":
            worst = "bad"
            break
        if item["state"] == "warn":
            worst = "warn"
    return {"items": items, "overall": worst, "checked_at": local(now.isoformat())}
