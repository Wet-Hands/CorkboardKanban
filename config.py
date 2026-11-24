"""Corkboard configuration.

Everything is read from environment variables, loaded from `.env` in the
project root. See `.env.example` for the full list with comments.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> list[str]:
    return [item.strip().lower() for item in _str(name, default).split(",") if item.strip()]


# --- General -----------------------------------------------------------------
TIMEZONE_NAME = _str("CORKBOARD_TIMEZONE", "UTC")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)
# Address phones use to reach the web UI; the QR code on each card points here.
BASE_URL = _str("CORKBOARD_BASE_URL", "http://corkboard.local:8000").rstrip("/")
DATA_DIR = Path(_str("CORKBOARD_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = Path(_str("CORKBOARD_DB_PATH", str(DATA_DIR / "corkboard.db")))
FONTS_DIR = BASE_DIR / "fonts"

# --- Web ---------------------------------------------------------------------
WEB_HOST = _str("CORKBOARD_HOST", "0.0.0.0")
WEB_PORT = _int("CORKBOARD_PORT", 8000)
SECRET_KEY = _str("CORKBOARD_SECRET_KEY", "dev-only-change-me")
# Optional HTTP basic auth for the web UI (not applied to /mcp or /github).
WEB_USERNAME = _str("CORKBOARD_WEB_USERNAME")
WEB_PASSWORD = _str("CORKBOARD_WEB_PASSWORD")

# --- Printer -----------------------------------------------------------------
# "/dev/usb/lp0" for the real printer, or "dry-run" to save PNGs to data/printed/.
PRINTER_DEVICE = _str("PRINTER_DEVICE", "/dev/usb/lp0")
PRINTER_DRY_RUN = PRINTER_DEVICE.lower() in ("dry-run", "dryrun", "none", "")
PAPER_WIDTH_PX = _int("PRINTER_WIDTH_PX", 384)
PRINTER_IMAGE_IMPL = _str("PRINTER_IMAGE_IMPL", "bitImageRaster")
PRINTER_FRAGMENT_HEIGHT = _int("PRINTER_FRAGMENT_HEIGHT", 960)
PRINTER_FEED_LINES = _int("PRINTER_FEED_LINES", 4)
PRINTER_THRESHOLD = _int("PRINTER_THRESHOLD", 160)
# Send DLE EOT 1 this often while idle so the printer doesn't power itself off. 0 = off.
PRINTER_KEEPALIVE_SECONDS = _int("PRINTER_KEEPALIVE_SECONDS", 30)
# Bytes sent as the keepalive, in hex (default DLE EOT 1). Find one that works with
# scripts/keepalive_test.py. PRINTER_KEEPALIVE_READ also reads the printer's reply.
PRINTER_KEEPALIVE_COMMAND = bytes.fromhex(_str("PRINTER_KEEPALIVE_COMMAND", "100401").replace(" ", ""))
PRINTER_KEEPALIVE_READ = _bool("PRINTER_KEEPALIVE_READ", False)
PRINT_MAX_ATTEMPTS = _int("PRINT_MAX_ATTEMPTS", 5)
PRINTED_DIR = DATA_DIR / "printed"

# --- Tasks and automations ---------------------------------------------------
PRIORITIES = ("low", "medium", "high")
STATUSES = ("todo", "doing", "done")
RECURRENCES = ("none", "daily", "weekdays", "weekly", "monthly")
PRINT_ON_CREATE = _bool("PRINT_ON_CREATE", True)
DIGEST_ENABLED = _bool("DIGEST_ENABLED", True)
DIGEST_TIME = _str("DIGEST_TIME", "07:30")
DIGEST_DAYS = _str("DIGEST_DAYS", "mon-sun")
REMINDER_ENABLED = _bool("REMINDER_ENABLED", True)
REMINDER_TIME = _str("REMINDER_TIME", "16:00")
# Which priorities get a due-soon reminder card (default: high only).
REMINDER_PRIORITIES = _list("REMINDER_PRIORITIES", "high")
OVERDUE_REPRINT_ENABLED = _bool("OVERDUE_REPRINT_ENABLED", True)
OVERDUE_REPRINT_DAY = _str("OVERDUE_REPRINT_DAY", "mon")
OVERDUE_REPRINT_TIME = _str("OVERDUE_REPRINT_TIME", "07:35")

# --- GitHub ------------------------------------------------------------------
GITHUB_TOKEN = _str("GITHUB_TOKEN")
GITHUB_USERNAME = _str("GITHUB_USERNAME")
GITHUB_API_URL = _str("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_POLL_MINUTES = _int("GITHUB_POLL_MINUTES", 5)
GITHUB_SEARCH_QUERY = _str("GITHUB_SEARCH_QUERY", "is:open is:pr review-requested:@me archived:false")
GITHUB_WEBHOOK_SECRET = _str("GITHUB_WEBHOOK_SECRET")
GITHUB_HIGH_LABELS = _list("GITHUB_HIGH_LABELS", "urgent,hotfix")
GITHUB_LOW_LABELS = _list("GITHUB_LOW_LABELS", "")
# What to do with draft PRs (or PRs labelled "draft"): "low" or "skip".
GITHUB_DRAFT_MODE = _str("GITHUB_DRAFT_MODE", "low").lower()
GITHUB_DUE_BUSINESS_DAYS = _int("GITHUB_DUE_BUSINESS_DAYS", 1)

# --- Claude connector (MCP) ---------------------------------------------------
# The connector is served at /mcp/<MCP_SECRET>. Empty = connector disabled.
MCP_SECRET = _str("MCP_SECRET")
# Public host names the tunnel serves (comma separated), for DNS-rebinding checks.
# Empty = accept any Host header (the secret path is then the only protection).
MCP_ALLOWED_HOSTS = _list("MCP_ALLOWED_HOSTS", "")
