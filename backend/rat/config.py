"""Runtime configuration for the RAT backend (env-overridable)."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PROJECT_ROOT = BASE_DIR.parent

DATA_DIR = Path(os.environ.get("RAT_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = DATA_DIR / "rat.sqlite3"
REPOS_DIR = DATA_DIR / "repos"
UPLOADS_DIR = DATA_DIR / "uploads"

# Frontend build output served by the API process in production.
FRONTEND_DIST = Path(os.environ.get("RAT_FRONTEND_DIST", PROJECT_ROOT / "frontend" / "dist"))

# git diff rename detection threshold (the brief fixes this at 50%).
RENAME_THRESHOLD = os.environ.get("RAT_RENAME_THRESHOLD", "50%")

# Rename detection search limit. git defaults to 1000; raising it keeps rename
# detection exact on very large commits at the cost of CPU on those rare diffs.
RENAME_LIMIT = int(os.environ.get("RAT_RENAME_LIMIT", "20000"))

# Upload guards (bytes / entries).
MAX_UPLOAD_BYTES = int(os.environ.get("RAT_MAX_UPLOAD_BYTES", 2 * 1024**3))
MAX_UNCOMPRESSED_BYTES = int(os.environ.get("RAT_MAX_UNCOMPRESSED_BYTES", 20 * 1024**3))
MAX_ZIP_ENTRIES = int(os.environ.get("RAT_MAX_ZIP_ENTRIES", 2_000_000))

# Analysis batching (rows are flushed to SQLite in transactions of this size).
FLUSH_COMMITS = int(os.environ.get("RAT_FLUSH_COMMITS", "4000"))

# Simple in-process query cache size for expensive aggregates.
CACHE_ENTRIES = int(os.environ.get("RAT_CACHE_ENTRIES", "256"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, REPOS_DIR, UPLOADS_DIR):
        d.mkdir(parents=True, exist_ok=True)
