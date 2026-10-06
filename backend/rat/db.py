"""SQLite schema and connection helpers for the RAT.

Storage model
-------------
repos     one row per ingested repository (zip upload or cloned URL)
commits   every non-merge commit reachable from the analysed reference (HEAD)
authors   distinct raw (name, email) identities; ``name_mm``/``email_mm`` hold
          the ``.mailmap``-resolved identity and ``canonical_id`` points at the
          representative author once identities are merged
objects   every file and directory ever touched, plus the root directory ``""``
changes   line deltas per commit per object.  File rows carry the real deltas;
          directory rows are pre-aggregated rollups written by ``rat.analyze``
          (a directory's metric is the sum over all descendants, so a single
          ancestor rollup implements the recursive definition exactly).
totals    repository-wide aggregate per object across the whole history; used
          as a fast path for the unfiltered dashboard.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS repos (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  source_type   TEXT NOT NULL CHECK (source_type IN ('url', 'zip')),
  source        TEXT NOT NULL,
  gitdir        TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  phase         TEXT,
  progress      REAL NOT NULL DEFAULT 0,
  message       TEXT,
  error         TEXT,
  head_hash     TEXT,
  commit_count  INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  analyzed_at   TEXT
);

CREATE TABLE IF NOT EXISTS commits (
  id        INTEGER PRIMARY KEY,
  repo_id   INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  hash      TEXT NOT NULL,
  author_id INTEGER NOT NULL,
  ct        INTEGER NOT NULL,
  subject   TEXT NOT NULL DEFAULT '',
  UNIQUE (repo_id, hash)
);
CREATE INDEX IF NOT EXISTS idx_commits_repo_ct     ON commits(repo_id, ct);
CREATE INDEX IF NOT EXISTS idx_commits_repo_author ON commits(repo_id, author_id);

CREATE TABLE IF NOT EXISTS authors (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  email        TEXT NOT NULL,
  name_mm      TEXT NOT NULL DEFAULT '',
  email_mm     TEXT NOT NULL DEFAULT '',
  canonical_id INTEGER,
  UNIQUE (repo_id, name, email)
);

CREATE TABLE IF NOT EXISTS idseq (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS objects (
  repo_id  INTEGER NOT NULL,
  path     TEXT NOT NULL,
  is_file  INTEGER NOT NULL,
  binary   INTEGER NOT NULL DEFAULT 0,
  first_ct INTEGER,
  last_ct  INTEGER,
  PRIMARY KEY (repo_id, path, is_file)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS changes (
  repo_id   INTEGER NOT NULL,
  commit_id INTEGER NOT NULL,
  path      TEXT NOT NULL,
  is_file   INTEGER NOT NULL,
  adds      INTEGER NOT NULL,
  dels      INTEGER NOT NULL,
  ct        INTEGER NOT NULL,
  PRIMARY KEY (repo_id, commit_id, path, is_file)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_changes_path ON changes(repo_id, path, is_file);
CREATE INDEX IF NOT EXISTS idx_changes_ct   ON changes(repo_id, ct);

CREATE TABLE IF NOT EXISTS totals (
  repo_id       INTEGER NOT NULL,
  path          TEXT NOT NULL,
  is_file       INTEGER NOT NULL,
  adds          INTEGER NOT NULL,
  dels          INTEGER NOT NULL,
  churn         INTEGER NOT NULL,
  modifications INTEGER NOT NULL,
  touches       INTEGER NOT NULL,
  first_ct      INTEGER,
  last_ct       INTEGER,
  PRIMARY KEY (repo_id, path, is_file)
) WITHOUT ROWID;
"""

_init_lock = threading.Lock()
_initialised = False


def connect(db_path: Path | str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a tuned SQLite connection."""
    path = str(db_path or config.DB_PATH)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create the schema (idempotent)."""
    global _initialised
    with _init_lock:
        config.ensure_dirs()
        conn = connect(db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialised = True


def wipe_repo_data(conn: sqlite3.Connection, repo_id: int) -> None:
    """Remove all analysis rows for a repo (author merges are preserved by the
    caller via :func:`rat.authors.snapshot_merges`)."""
    for table in ("changes", "totals", "objects", "commits"):
        conn.execute(f"DELETE FROM {table} WHERE repo_id = ?", (repo_id,))
    conn.execute("DELETE FROM authors WHERE repo_id = ?", (repo_id,))
    conn.commit()
