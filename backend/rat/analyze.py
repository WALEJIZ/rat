"""Streaming analysis of a repository's history into SQLite.

Two passes over the reference commit (HEAD):

1. *metadata pass* (cheap, no diffs): every non-merge commit reachable from the
   reference, with both the raw (``%an``/``%ae``) and the ``.mailmap``-resolved
   (``%aN``/``%aE``) author identity, the committer timestamp and the subject.

2. *delta pass* (the expensive one): ``git log --numstat -z -M50% --root`` is
   streamed and parsed incrementally.  Rename detection is git's own (``-M50%``,
   the threshold the brief fixes), so a pure rename produces a ``0/0`` record on
   the new path and a rename with edits is attributed to the new path only.
   Binary entries (``-\\t-``) are recorded with ``0/0`` and never measured.

For every commit the parser writes:
  * a **file row** per changed file (``(path, adds, dels)``), and
  * **directory rows**: the same deltas rolled up to every ancestor directory
    (including the root ``""``).  Because a directory's metric is the sum over
    all of its descendants, this single rollup implements the recursive
    directory definition exactly and makes every later query a point lookup.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Iterator

from . import config
from .db import connect, wipe_repo_data
from .gitio import GitError, GitStream, mailmap_args, run_git

log = logging.getLogger("rat.analyze")

META_FORMAT = "%x00%H%x1f%an%x1f%ae%x1f%aN%x1f%aE%x1f%ct%x1f%s"
DELTA_FORMAT = "%x1e%H"
_FS = b"\x1f"
_NUL = b"\x00"
_RS = b"\x1e"


def _decode(b: bytes) -> str:
    return b.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Pass 1 - commit metadata
# --------------------------------------------------------------------------

def _iter_meta(gitdir: Path) -> Iterator[tuple[str, str, str, str, str, int, str]]:
    """Yield (hash, author_name, author_email, mm_name, mm_email, ct, subject)."""
    stream = GitStream(
        [
            "-c", "i18n.logOutputEncoding=UTF-8",
            *mailmap_args(gitdir),
            "log", "--no-merges",
            f"--pretty=tformat:{META_FORMAT}",
            "HEAD",
        ],
        gitdir,
    )
    buf = b""
    for chunk in stream.chunks():
        buf += chunk
        records = buf.split(_NUL)
        buf = records.pop()
        for rec in records:
            parsed = _parse_meta(rec)
            if parsed:
                yield parsed
    parsed = _parse_meta(buf)
    if parsed:
        yield parsed
    stream.wait()


def _parse_meta(rec: bytes):
    rec = rec.rstrip(b"\n")
    if not rec:
        return None
    parts = rec.split(_FS, 6)
    if len(parts) < 7:
        return None
    h, an, ae, aN, aE, ct, subject = parts
    try:
        ct_i = int(ct)
    except ValueError:
        return None
    return (_decode(h), _decode(an), _decode(ae), _decode(aN) or _decode(an),
            _decode(aE) or _decode(ae), ct_i, _decode(subject).replace("\x00", ""))


# --------------------------------------------------------------------------
# Pass 2 - line deltas
# --------------------------------------------------------------------------

def _iter_deltas(gitdir: Path) -> Iterator[tuple[str, dict]]:
    """Yield (commit_hash, {path: [adds, dels, binary]}) for every commit."""
    stream = GitStream(
        [
            "-c", f"diff.renameLimit={config.RENAME_LIMIT}",
            "-c", "i18n.logOutputEncoding=UTF-8",
            "log", "--no-merges", "--root",
            f"-M{config.RENAME_THRESHOLD}",
            "--numstat", "-z",
            f"--pretty=tformat:{DELTA_FORMAT}",
            "HEAD",
        ],
        gitdir,
    )

    cur_hash: str | None = None
    files: dict[str, list[int]] = {}
    # Rename continuation state: a "add\tdel\t" head is followed by two NUL
    # fields holding the full pre-image and post-image paths.
    rename_head: tuple[int, int, bool] | None = None
    rename_paths: list[str] = []

    def finish(fhash: str | None) -> tuple[str, dict] | None:
        if fhash is None:
            return None
        return fhash, files

    buf = b""
    for chunk in stream.chunks():
        buf += chunk
        fields = buf.split(_NUL)
        buf = fields.pop()
        for field in fields:
            if rename_head is not None:
                rename_paths.append(_decode(field))
                if len(rename_paths) == 2:
                    adds, dels, binary = rename_head
                    files[rename_paths[1]] = [adds, dels, 1 if binary else 0]
                    rename_head = None
                    rename_paths = []
                continue

            if field.startswith(_RS):
                # Commit header; in `-z` mode the newline-terminated header and
                # the first numstat record share a single NUL field.
                done = finish(cur_hash)
                if done:
                    yield done
                head, sep, rest = field[1:].partition(b"\n")
                cur_hash = _decode(head).strip()
                files = {}
                if sep and rest:
                    field = rest  # fall through and parse the glued record

            if not field:
                continue

            parts = field.split(b"\t", 2)
            if len(parts) < 3:
                continue
            raw_adds, raw_dels, path = parts
            binary = raw_adds == b"-" or raw_dels == b"-"
            try:
                adds = 0 if binary else int(raw_adds)
                dels = 0 if binary else int(raw_dels)
            except ValueError:
                continue

            if path == b"":
                rename_head = (adds, dels, binary)
                rename_paths = []
                continue

            files[_decode(path)] = [adds, dels, 1 if binary else 0]

    fields = buf.split(_NUL) if buf else []
    for field in fields:
        if b"\t" in field:
            parts = field.split(b"\t", 2)
            if len(parts) == 3 and parts[2]:
                try:
                    files[_decode(parts[2])] = [
                        0 if parts[0] == b"-" else int(parts[0]),
                        0 if parts[1] == b"-" else int(parts[1]),
                        1 if (parts[0] == b"-" or parts[1] == b"-") else 0,
                    ]
                except ValueError:
                    pass
    done = finish(cur_hash)
    if done:
        yield done
    stream.wait()


def _commit_rows(files: dict[str, list[int]]) -> Iterator[tuple[str, int, int, int]]:
    """Expand per-file deltas into file rows + ancestor directory rollups."""
    dirs: dict[str, list[int]] = {}
    for path, (adds, dels, _binary) in files.items():
        yield path, 1, adds, dels
        if adds == 0 and dels == 0:
            continue
        p = path
        while True:
            i = p.rfind("/")
            key = p[:i] if i >= 0 else ""
            acc = dirs.get(key)
            if acc is None:
                dirs[key] = [adds, dels]
            else:
                acc[0] += adds
                acc[1] += dels
            if key == "":
                break
            p = key
    for key, (adds, dels) in dirs.items():
        yield key, 0, adds, dels


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

class AnalysisError(RuntimeError):
    pass


def _alloc_commit_ids(conn, n: int) -> int:
    """Atomically reserve ``n`` consecutive ids from the global commit sequence.

    Commit ids are a single database-wide sequence (``commits.id`` is the
    primary key shared by every repository), so two analyses running
    concurrently must never hand out overlapping ids.  A single
    ``UPDATE … RETURNING`` is atomic in SQLite.
    """
    row = conn.execute(
        "UPDATE idseq SET value = value + ? WHERE name = 'commit' RETURNING value", (n,)
    ).fetchone()
    if row is None:  # sequence row missing on very old databases
        conn.execute("INSERT INTO idseq (name, value) VALUES ('commit', ?)", (n,))
        return 1
    return int(row[0]) - n + 1


def analyze_repo(
    repo_id: int,
    gitdir: Path,
    *,
    db_path: str | Path | None = None,
    progress: Callable[[str, float, str], None] | None = None,
) -> dict:
    """Analyse ``gitdir`` and (re)write all metric rows for ``repo_id``.

    ``progress(phase, fraction, message)`` is invoked periodically.
    """
    started = time.time()
    gitdir = Path(gitdir)

    def report(phase: str, fraction: float, message: str = "") -> None:
        if progress:
            progress(phase, max(0.0, min(1.0, fraction)), message)

    # Sanity check: the reference commit must exist.
    try:
        run_git(["rev-parse", "--verify", "HEAD"], gitdir, timeout=60)
    except GitError as exc:
        raise AnalysisError(f"The repository has no commits to analyse ({exc}).") from exc

    head = run_git(["rev-parse", "HEAD"], gitdir, timeout=60).stdout.decode().strip()

    conn = connect(db_path)
    try:
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA cache_size = -200000")  # ~200 MB page cache
        wipe_repo_data(conn, repo_id)

        # -- pass 1 ---------------------------------------------------------
        report("reading-history", 0.0, "Reading commit history")
        conn.execute("INSERT OR IGNORE INTO idseq (name, value) VALUES ('commit', 0)")
        conn.execute(
            "UPDATE idseq SET value = MAX(value, COALESCE((SELECT MAX(id) FROM commits), 0))"
            " WHERE name = 'commit'"
        )

        author_ids: dict[tuple[str, str], int] = {}
        commit_index: dict[str, tuple[int, int]] = {}  # hash -> (id, ct)
        pending: list[tuple[str, int, int, str]] = []  # (hash, author_id, ct, subject)
        meta_count = 0

        def flush_commits(force: bool = False) -> None:
            if not pending or not (force or len(pending) >= config.FLUSH_COMMITS):
                return
            first_id = _alloc_commit_ids(conn, len(pending))
            rows = []
            for i, (row_hash, aid, ct, subject) in enumerate(pending):
                cid = first_id + i
                rows.append((cid, repo_id, row_hash, aid, ct, subject))
                commit_index[row_hash] = (cid, ct)
            conn.executemany(
                "INSERT INTO commits (id, repo_id, hash, author_id, ct, subject)"
                " VALUES (?,?,?,?,?,?)",
                rows,
            )
            pending.clear()

        for h, an, ae, aN, aE, ct, subject in _iter_meta(gitdir):
            key = (an, ae)
            aid = author_ids.get(key)
            if aid is None:
                cur = conn.execute(
                    "INSERT INTO authors (repo_id, name, email, name_mm, email_mm)"
                    " VALUES (?,?,?,?,?)",
                    (repo_id, an, ae, aN, aE),
                )
                aid = cur.lastrowid
                author_ids[key] = aid
            pending.append((h, aid, ct, subject))
            meta_count += 1
            if len(pending) >= config.FLUSH_COMMITS:
                flush_commits(True)
                conn.commit()
                report(
                    "reading-history",
                    min(0.15, meta_count / 400000),
                    f"Read {meta_count:,} commits",
                )
        flush_commits(True)
        conn.commit()

        if meta_count == 0:
            raise AnalysisError("No non-merge commits found reachable from HEAD.")

        # -- pass 2 ---------------------------------------------------------
        report("parsing-diffs", 0.15, f"Parsing diffs for {meta_count:,} commits")
        objects: dict[tuple[str, int], list] = {}  # (path, is_file) -> [binary, first_ct, last_ct]
        change_rows: list[tuple] = []
        parsed = 0
        rows_written = 0

        def touch_object(path: str, is_file: int, binary: int, ct: int) -> None:
            key = (path, is_file)
            obj = objects.get(key)
            if obj is None:
                objects[key] = [binary, ct, ct]
            else:
                if binary:
                    obj[0] = 1
                if ct < obj[1]:
                    obj[1] = ct
                if ct > obj[2]:
                    obj[2] = ct

        touch_object("", 0, 0, 0)

        def flush_changes(force: bool = False) -> None:
            if change_rows and (force or len(change_rows) >= 60000):
                conn.executemany(
                    "INSERT INTO changes (repo_id, commit_id, path, is_file, adds, dels, ct)"
                    " VALUES (?,?,?,?,?,?,?)",
                    change_rows,
                )
                change_rows.clear()

        for h, files in _iter_deltas(gitdir):
            entry = commit_index.get(h)
            if entry is None:
                continue
            cid, ct = entry
            for path, (_adds, _dels, binary) in files.items():
                touch_object(path, 1, binary, ct)
            for path, is_file, adds, dels in _commit_rows(files):
                change_rows.append((repo_id, cid, path, is_file, adds, dels, ct))
            parsed += 1
            if len(change_rows) >= 60000:
                flush_changes(True)
                conn.commit()
            if parsed % 500 == 0:
                report(
                    "parsing-diffs",
                    0.15 + 0.8 * parsed / meta_count,
                    f"Parsed {parsed:,} / {meta_count:,} commits",
                )
        flush_changes(True)
        conn.commit()
        rows_written = conn.execute(
            "SELECT COUNT(*) FROM changes WHERE repo_id = ?", (repo_id,)
        ).fetchone()[0]

        # Guarantee every ancestor directory of every file is an object too
        # (directories have no standalone git entries; they exist via contents).
        for (path, is_file), obj in list(objects.items()):
            if is_file != 1:
                continue
            p = path
            while True:
                i = p.rfind("/")
                key = p[:i] if i >= 0 else ""
                touch_object(key, 0, 0, obj[1])
                touch_object(key, 0, 0, obj[2])
                if key == "":
                    break
                p = key

        # -- objects + totals ----------------------------------------------
        report("aggregating", 0.97, "Aggregating metrics")
        conn.executemany(
            "INSERT OR REPLACE INTO objects (repo_id, path, is_file, binary, first_ct, last_ct)"
            " VALUES (?,?,?,?,?,?)",
            [(repo_id, path, is_file, obj[0], obj[1] or None, obj[2] or None)
             for (path, is_file), obj in objects.items()],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO totals
              (repo_id, path, is_file, adds, dels, churn, modifications, touches, first_ct, last_ct)
            SELECT repo_id, path, is_file,
                   SUM(adds), SUM(dels), SUM(adds + dels),
                   SUM(CASE WHEN adds + dels > 0 THEN 1 ELSE 0 END),
                   COUNT(*), MIN(ct), MAX(ct)
              FROM changes WHERE repo_id = ?
             GROUP BY path, is_file
            """,
            (repo_id,),
        )
        conn.commit()

        duration = time.time() - started
        report(
            "ready",
            1.0,
            f"Analysed {parsed:,} commits, {len(objects):,} objects, {rows_written:,} change rows "
            f"in {duration:.1f}s",
        )
        return {
            "commits": parsed,
            "objects": len(objects),
            "change_rows": rows_written,
            "duration_s": round(duration, 2),
            "head": head,
        }
    finally:
        conn.close()
