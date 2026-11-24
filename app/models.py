"""Task and print-queue functions.

Every source of tasks (web UI, Claude connector, GitHub, automations) goes
through `create_task()`, which stores the task and queues its card for
printing. The worker process is the only thing that talks to the printer.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import config
from app.db import get_conn, transaction, utcnow

TITLE_MAX = 200
DESCRIPTION_MAX = 2000
SOURCES = ("web", "claude", "github", "recurring")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# --- Dates ---------------------------------------------------------------------

def today() -> date:
    return datetime.now(config.TIMEZONE).date()


def parse_due_date(value: Any, ref: date | None = None) -> str | None:
    """Turn user input into a YYYY-MM-DD string (or None).

    Accepts ISO dates, "today", "tomorrow", weekday names ("fri", "friday" =
    the next one, today included) and offsets like "+3d" or "+2w".
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip().lower()
    if not text or text in ("none", "null", "no", "-"):
        return None
    ref = ref or today()
    if text == "today":
        return ref.isoformat()
    if text == "tomorrow":
        return (ref + timedelta(days=1)).isoformat()
    for index, name in enumerate(_WEEKDAYS):
        if text in (name, name[:3]) or text in ("next " + name, "next " + name[:3]):
            delta = (index - ref.weekday()) % 7
            if text.startswith("next ") and delta == 0:
                delta = 7
            return (ref + timedelta(days=delta)).isoformat()
    match = re.fullmatch(r"\+?(\d{1,3})\s*([dw])", text)
    if match:
        amount = int(match.group(1)) * (7 if match.group(2) == "w" else 1)
        return (ref + timedelta(days=amount)).isoformat()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise ValueError(
            f"Can't understand due date {value!r}. Use YYYY-MM-DD, today, tomorrow, a weekday or +3d."
        ) from None


def due_state(task: dict, ref: date | None = None) -> str | None:
    """'overdue', 'today', 'tomorrow' or None for an open task."""
    if not task.get("due_date") or task.get("status") == "done":
        return None
    ref = ref or today()
    due = date.fromisoformat(task["due_date"])
    if due < ref:
        return "overdue"
    if due == ref:
        return "today"
    if due == ref + timedelta(days=1):
        return "tomorrow"
    return None


def add_business_days(start: date, days: int) -> date:
    current = start
    remaining = max(days, 0)
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    while current.weekday() >= 5:  # "0 business days" on a weekend lands on Monday
        current += timedelta(days=1)
    return current


def next_occurrence(base: date, recurrence: str) -> date:
    if recurrence == "daily":
        return base + timedelta(days=1)
    if recurrence == "weekdays":
        nxt = base + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    if recurrence == "weekly":
        return base + timedelta(days=7)
    if recurrence == "monthly":
        year, month = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
        day = min(base.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    raise ValueError(f"Not a recurrence: {recurrence}")


# --- Quiet hours ---------------------------------------------------------------------

def parse_quiet_hours(text: str) -> tuple[time, time] | None:
    """"22:00-07:00" -> (22:00, 07:00). Empty means no quiet hours."""
    text = (text or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError(f"QUIET_HOURS must look like 22:00-07:00, not {text!r}")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError(f"QUIET_HOURS has an impossible time: {text!r}")
    start, end = time(h1, m1), time(h2, m2)
    return None if start == end else (start, end)


def quiet_until(now: datetime | None = None) -> datetime | None:
    """When the current quiet hours end, or None if it isn't quiet hours now."""
    window = parse_quiet_hours(config.QUIET_HOURS)
    if window is None:
        return None
    start, end = window
    now = now or datetime.now(config.TIMEZONE)
    current = now.timetz().replace(tzinfo=None)
    if start < end:  # e.g. 13:00-15:00
        inside = start <= current < end
        end_day = now.date()
    else:  # wraps past midnight, e.g. 22:00-07:00
        inside = current >= start or current < end
        end_day = now.date() + timedelta(days=1) if current >= start else now.date()
    if not inside:
        return None
    return datetime.combine(end_day, end, now.tzinfo)


# --- Checklists ---------------------------------------------------------------------

_CHECK_ITEM = re.compile(r"^\s*(?:[-*+]\s+)?\[( |x|X)\]\s+(.*\S)\s*$")


def checklist(description: str) -> list[dict]:
    """Split a description into text lines and checklist items ("- [ ] item", "- [x] done")."""
    parts = []
    for index, line in enumerate((description or "").split("\n")):
        match = _CHECK_ITEM.match(line)
        if match:
            parts.append({"kind": "item", "line": index, "checked": match.group(1) != " ", "text": match.group(2)})
        else:
            parts.append({"kind": "text", "line": index, "text": line})
    return parts


def checklist_progress(description: str) -> tuple[int, int] | None:
    items = [p for p in checklist(description) if p["kind"] == "item"]
    return (sum(p["checked"] for p in items), len(items)) if items else None


def toggle_checklist_item(task_id: int, line: int) -> dict:
    task = require_task(task_id)
    lines = task["description"].split("\n")
    if not 0 <= line < len(lines) or not _CHECK_ITEM.match(lines[line]):
        raise ValueError("That line isn't a checklist item.")
    lines[line] = re.sub(
        r"\[( |x|X)\]", lambda m: "[ ]" if m.group(1) != " " else "[x]", lines[line], count=1
    )
    return update_task(task_id, description="\n".join(lines))


# --- Validation --------------------------------------------------------------------

def _clean_title(title: Any) -> str:
    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("A task needs a title.")
    if len(title) > TITLE_MAX:
        raise ValueError(f"Title is longer than {TITLE_MAX} characters.")
    return title


def _clean_description(description: Any) -> str:
    description = str(description or "").replace("\r\n", "\n").strip()
    if len(description) > DESCRIPTION_MAX:
        raise ValueError(f"Description is longer than {DESCRIPTION_MAX} characters.")
    return description


def _check_choice(value: Any, choices: tuple[str, ...], label: str) -> str:
    value = str(value or "").strip().lower()
    if value not in choices:
        raise ValueError(f"{label} must be one of: {', '.join(choices)}.")
    return value


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


# --- Tasks -------------------------------------------------------------------------

def create_task(
    title: str,
    description: str = "",
    due_date: Any = None,
    priority: str = "medium",
    *,
    status: str = "todo",
    recurrence: str = "none",
    source: str = "web",
    source_ref: str | None = None,
    url: str | None = None,
    print_card: bool | None = None,
    manual: bool = False,
) -> dict:
    """Store a task and (by default) queue its card. Every task source uses this."""
    fields = {
        "title": _clean_title(title),
        "description": _clean_description(description),
        "due_date": parse_due_date(due_date),
        "priority": _check_choice(priority or "medium", config.PRIORITIES, "Priority"),
        "status": _check_choice(status or "todo", config.STATUSES, "Status"),
        "recurrence": _check_choice(recurrence or "none", config.RECURRENCES, "Recurrence"),
        "source": _check_choice(source, SOURCES, "Source"),
        "source_ref": source_ref,
        "url": url,
    }
    now = utcnow()
    with transaction() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (title, description, due_date, priority, status, recurrence,
                                  source, source_ref, url, created_at, updated_at, completed_at)
               VALUES (:title, :description, :due_date, :priority, :status, :recurrence,
                       :source, :source_ref, :url, :now, :now, :completed)""",
            {**fields, "now": now, "completed": now if fields["status"] == "done" else None},
        )
        task_id = cur.lastrowid
    if config.PRINT_ON_CREATE if print_card is None else print_card:
        enqueue_print(task_id, "created", manual=manual)
    return get_task(task_id)


def get_task(task_id: int) -> dict | None:
    with get_conn() as conn:
        return _row(conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone())


def require_task(task_id: int) -> dict:
    task = get_task(task_id)
    if task is None:
        raise LookupError(f"No task #{task_id}.")
    return task


def list_tasks(
    status: str | None = None,
    *,
    include_done: bool = True,
    priority: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(_check_choice(status, config.STATUSES, "Status"))
    elif not include_done:
        clauses.append("status != 'done'")
    if priority:
        clauses.append("priority = ?")
        params.append(_check_choice(priority, config.PRIORITIES, "Priority"))
    sql = "SELECT * FROM tasks"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += """ ORDER BY
        CASE WHEN status = 'done' THEN completed_at END DESC,
        CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
        due_date IS NULL, due_date, id"""
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def board() -> dict[str, list[dict]]:
    """Tasks grouped for the kanban view; Done shows the 20 most recent."""
    return {
        "todo": list_tasks("todo"),
        "doing": list_tasks("doing"),
        "done": list_tasks("done", limit=20),
    }


UPDATABLE = ("title", "description", "due_date", "priority", "status", "recurrence", "url")


def update_task(task_id: int, **changes: Any) -> dict:
    task = require_task(task_id)
    unknown = set(changes) - set(UPDATABLE)
    if unknown:
        raise ValueError(f"Can't update: {', '.join(sorted(unknown))}.")
    values: dict[str, Any] = {}
    if "title" in changes:
        values["title"] = _clean_title(changes["title"])
    if "description" in changes:
        values["description"] = _clean_description(changes["description"])
    if "due_date" in changes:
        values["due_date"] = parse_due_date(changes["due_date"])
        if values["due_date"] != task["due_date"]:
            values["reminded_for"] = None
    if "priority" in changes:
        values["priority"] = _check_choice(changes["priority"], config.PRIORITIES, "Priority")
    if "recurrence" in changes:
        values["recurrence"] = _check_choice(changes["recurrence"], config.RECURRENCES, "Recurrence")
    if "url" in changes:
        values["url"] = changes["url"] or None
    if "status" in changes:
        status = _check_choice(changes["status"], config.STATUSES, "Status")
        values["status"] = status
        if status == "done" and task["status"] != "done":
            values["completed_at"] = utcnow()
        elif status != "done":
            values["completed_at"] = None
            values["recurrence_spawned"] = 0 if task["status"] == "done" else task["recurrence_spawned"]
    if not values:
        return task
    values["updated_at"] = utcnow()
    assignments = ", ".join(f"{key} = :{key}" for key in values)
    with transaction() as conn:
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id = :id", {**values, "id": task["id"]})
    return require_task(task["id"])


def set_status(task_id: int, status: str) -> dict:
    return update_task(task_id, status=status)


def complete_task(task_id: int) -> dict:
    return update_task(task_id, status="done")


def delete_task(task_id: int) -> None:
    require_task(task_id)
    with transaction() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))


def spawn_recurring(ref: date | None = None) -> list[dict]:
    """Create the next card for completed recurring tasks whose next date has arrived."""
    ref = ref or today()
    created = []
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE recurrence != 'none' AND status = 'done' AND recurrence_spawned = 0"
        ).fetchall()]
    for task in rows:
        if task["due_date"]:
            base = date.fromisoformat(task["due_date"])
        else:
            completed = datetime.fromisoformat(task["completed_at"] or task["updated_at"])
            base = completed.astimezone(config.TIMEZONE).date()
        nxt = next_occurrence(base, task["recurrence"])
        while nxt < ref:
            nxt = next_occurrence(nxt, task["recurrence"])
        if nxt > ref:
            continue
        with transaction() as conn:
            claimed = conn.execute(
                "UPDATE tasks SET recurrence_spawned = 1 WHERE id = ? AND recurrence_spawned = 0",
                (task["id"],),
            ).rowcount
        if claimed:
            created.append(create_task(
                task["title"], task["description"], nxt, task["priority"],
                recurrence=task["recurrence"], source="recurring", url=task["url"],
            ))
    return created


def digest(ref: date | None = None) -> dict:
    """What the morning digest card shows."""
    ref = ref or today()
    open_tasks = list_tasks(include_done=False)
    iso = ref.isoformat()
    yesterday_start = datetime.combine(ref - timedelta(days=1), datetime.min.time(), config.TIMEZONE)
    with get_conn() as conn:
        done_yesterday = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'done' AND completed_at >= ? AND completed_at < ?",
            (
                yesterday_start.astimezone(timezone.utc).isoformat(),
                (yesterday_start + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
            ),
        ).fetchone()[0]
    return {
        "date": iso,
        "overdue": [t for t in open_tasks if t["due_date"] and t["due_date"] < iso],
        "today": [t for t in open_tasks if t["due_date"] == iso],
        "doing": [t for t in open_tasks if t["status"] == "doing" and not (t["due_date"] and t["due_date"] <= iso)],
        "high": [t for t in open_tasks if t["priority"] == "high" and t["status"] == "todo"
                 and not (t["due_date"] and t["due_date"] <= iso)],
        "counts": {
            "todo": sum(t["status"] == "todo" for t in open_tasks),
            "doing": sum(t["status"] == "doing" for t in open_tasks),
            "done_yesterday": done_yesterday,
        },
    }


# --- Print queue ----------------------------------------------------------------------

def _enqueue(conn, kind: str, task_id: int | None, reason: str, manual: bool) -> dict:
    """Insert a job unless the same thing is already waiting; a manual request upgrades a waiting job."""
    existing = conn.execute(
        "SELECT * FROM print_jobs WHERE kind = ? AND task_id IS ? AND status = 'pending'", (kind, task_id)
    ).fetchone()
    if existing:
        if manual and not existing["manual"]:
            conn.execute("UPDATE print_jobs SET manual = 1 WHERE id = ?", (existing["id"],))
        job_id = existing["id"]
    else:
        job_id = conn.execute(
            "INSERT INTO print_jobs (kind, task_id, reason, manual, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, task_id, reason, int(manual), utcnow()),
        ).lastrowid
    return dict(conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone())


def enqueue_print(task_id: int, reason: str = "reprint", *, manual: bool = False) -> dict:
    """Queue a card. A task with a job already waiting isn't queued twice.

    `manual` marks a print a person asked for in the web app; it skips quiet hours.
    """
    require_task(task_id)
    with transaction() as conn:
        return _enqueue(conn, "task", int(task_id), reason, manual)


def enqueue_digest(*, manual: bool = False) -> dict:
    with transaction() as conn:
        return _enqueue(conn, "digest", None, "digest", manual)


def claim_next_job(quiet: bool = False) -> dict | None:
    """Take the next job that's due. During quiet hours only manual jobs are taken (if allowed)."""
    now = utcnow()
    quiet_clause = ""
    if quiet:
        quiet_clause = "AND manual = 1" if config.QUIET_HOURS_ALLOW_MANUAL else "AND 0"
    with transaction() as conn:
        job = conn.execute(
            f"""SELECT * FROM print_jobs WHERE status = 'pending' {quiet_clause}
               AND (not_before IS NULL OR not_before <= ?) ORDER BY id LIMIT 1""",
            (now,),
        ).fetchone()
        if job is None:
            return None
        conn.execute("UPDATE print_jobs SET status = 'printing' WHERE id = ?", (job["id"],))
        return {**dict(job), "status": "printing"}


def finish_job(job: dict) -> None:
    now = utcnow()
    with transaction() as conn:
        conn.execute(
            "UPDATE print_jobs SET status = 'done', printed_at = ?, attempts = attempts + 1, last_error = NULL "
            "WHERE id = ?",
            (now, job["id"]),
        )
        if job.get("task_id"):
            conn.execute("UPDATE tasks SET last_printed_at = ? WHERE id = ?", (now, job["task_id"]))


def release_job(job: dict, note: str | None = None) -> None:
    """Put a job back without counting an attempt (printer is off or unplugged)."""
    with transaction() as conn:
        conn.execute(
            "UPDATE print_jobs SET status = 'pending', last_error = ? WHERE id = ?", (note, job["id"])
        )


def fail_job(job: dict, error: str) -> str:
    """Count a failed attempt; retry with backoff until PRINT_MAX_ATTEMPTS. Returns the new status."""
    attempts = int(job.get("attempts") or 0) + 1
    status = "failed" if attempts >= config.PRINT_MAX_ATTEMPTS else "pending"
    delay = min(15 * 2 ** (attempts - 1), 600)
    not_before = (datetime.now(timezone.utc) + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
    with transaction() as conn:
        conn.execute(
            "UPDATE print_jobs SET status = ?, attempts = ?, last_error = ?, not_before = ? WHERE id = ?",
            (status, attempts, error[:500], not_before, job["id"]),
        )
    return status


def reset_stuck_jobs() -> int:
    """Jobs left 'printing' by a crash go back in the queue (called when the worker starts)."""
    with transaction() as conn:
        return conn.execute("UPDATE print_jobs SET status = 'pending' WHERE status = 'printing'").rowcount


def list_jobs(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT j.*, t.title AS task_title FROM print_jobs j
               LEFT JOIN tasks t ON t.id = j.task_id ORDER BY j.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()]


def held_count() -> int:
    """Jobs that wait for quiet hours to end."""
    with get_conn() as conn:
        manual_clause = "AND manual = 0" if config.QUIET_HOURS_ALLOW_MANUAL else ""
        return conn.execute(f"SELECT COUNT(*) FROM print_jobs WHERE status = 'pending' {manual_clause}").fetchone()[0]


def queue_counts() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM print_jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def retry_job(job_id: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE print_jobs SET status = 'pending', attempts = 0, not_before = NULL, last_error = NULL "
            "WHERE id = ? AND status = 'failed'",
            (int(job_id),),
        )


def cancel_job(job_id: int) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM print_jobs WHERE id = ? AND status IN ('pending','failed')", (int(job_id),))


# --- Worker status (shown on the health page) -------------------------------------------

def put_status(key: str, value: Any) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO worker_status (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, json.dumps(value), utcnow()),
        )


def get_statuses() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM worker_status").fetchall()
    return {r["key"]: {"value": json.loads(r["value"]), "updated_at": r["updated_at"]} for r in rows}
