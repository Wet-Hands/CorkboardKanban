"""SQLite storage shared by the web process and the worker process."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    due_date        TEXT,                       -- YYYY-MM-DD, local time
    priority        TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low','medium','high')),
    status          TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','doing','done')),
    recurrence      TEXT NOT NULL DEFAULT 'none'
                    CHECK (recurrence IN ('none','daily','weekdays','weekly','monthly')),
    source          TEXT NOT NULL DEFAULT 'web',  -- web, claude, github, recurring
    source_ref      TEXT,                       -- e.g. "owner/repo#123" for PR cards
    url             TEXT,                       -- link the QR code opens instead of the task page
    recurrence_spawned INTEGER NOT NULL DEFAULT 0, -- next occurrence already created
    reminded_for    TEXT,                       -- due date a due-soon reminder was printed for
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,
    last_printed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS print_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('task','digest')),
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL DEFAULT '',       -- created, reprint, reminder, overdue, digest
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','printing','done','failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    not_before  TEXT,                           -- retry backoff
    created_at  TEXT NOT NULL,
    printed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_print_jobs_status ON print_jobs(status, id);

CREATE TABLE IF NOT EXISTS seen_github_prs (
    pr_key      TEXT PRIMARY KEY,               -- "owner/repo#123"
    task_id     INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    active      INTEGER NOT NULL DEFAULT 1,     -- review currently requested
    skipped     INTEGER NOT NULL DEFAULT 0,     -- seen but no card (draft with GITHUB_DRAFT_MODE=skip)
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS github_deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """A connection in autocommit mode; use `transaction()` for multi-statement writes."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
