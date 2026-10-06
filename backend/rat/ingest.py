"""Repository ingestion: clone remote URLs, unpack zip uploads, background jobs.

Every ingestion runs on a daemon thread and reports progress through the repos
row (``status``/``phase``/``progress``/``message``) which the UI polls.  Jobs can
be cancelled when a repository is deleted mid-analysis.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import config
from .analyze import AnalysisError, analyze_repo
from .authors import restore_merges, snapshot_merges
from .db import connect, init_db
from .gitio import (
    GitError,
    extract_zip,
    find_gitdir,
    run_git,
    run_git_text,
    run_git_with_progress,
)
from .metrics import clear_cache

log = logging.getLogger("rat.ingest")

_URL_RE = re.compile(r"^(https?://|ssh://|git://|git\+ssh://|file://|[\w.+-]+@[\w.-]+:).+", re.I)


class IngestError(RuntimeError):
    pass


class DuplicateRepoError(IngestError):
    """Raised when the same remote URL is already registered."""

    def __init__(self, repo_id: int, name: str):
        super().__init__(f"Repository '{name}' has already been added.")
        self.repo_id = repo_id
        self.name = name


# --------------------------------------------------------------------------
# Job registry
# --------------------------------------------------------------------------

class Job:
    def __init__(self, repo_id: int):
        self.repo_id = repo_id
        self.cancelled = False
        self.proc = None  # type: ignore[assignment]
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def attach_proc(self, proc) -> None:
        with self._lock:
            self.proc = proc
            if self.cancelled:
                proc.kill()

    def detach_proc(self) -> None:
        with self._lock:
            self.proc = None

    def cancel(self) -> None:
        self.cancelled = True
        with self._lock:
            if self.proc is not None:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def wait(self, timeout: float = 30.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout)


_jobs: dict[int, Job] = {}
_jobs_lock = threading.Lock()


def get_job(repo_id: int) -> Job | None:
    with _jobs_lock:
        return _jobs.get(repo_id)


def _set_job(repo_id: int, job: Job | None) -> None:
    with _jobs_lock:
        if job is None:
            _jobs.pop(repo_id, None)
        else:
            _jobs[repo_id] = job


# --------------------------------------------------------------------------
# Status helpers
# --------------------------------------------------------------------------

def _update_status(repo_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = connect()
    try:
        conn.execute(f"UPDATE repos SET {cols} WHERE id = ?", (*fields.values(), repo_id))
        conn.commit()
    finally:
        conn.close()


def mark_interrupted_repos() -> None:
    """Called at startup: analyses cannot survive a server restart."""
    conn = connect()
    try:
        conn.execute(
            """UPDATE repos SET status = 'error',
                      error = 'Analysis was interrupted by a server restart. Use Re-analyze to retry.',
                      phase = NULL
                WHERE status IN ('pending', 'cloning', 'fetching', 'analyzing')"""
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url or len(url) > 500:
        raise IngestError("Enter a repository URL.")
    if url.startswith("-"):
        raise IngestError("Invalid repository URL.")
    if not _URL_RE.match(url):
        raise IngestError(
            "That does not look like a git URL. Use https://, ssh:// or git@host:path forms."
        )
    return url


def repo_name_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or "repository"


def find_by_source(source: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, name, status FROM repos WHERE source = ? ORDER BY id DESC LIMIT 1", (source,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_repo_from_url(url: str) -> int:
    url = validate_url(url)
    init_db()
    existing = find_by_source(url)
    if existing:
        raise DuplicateRepoError(existing["id"], existing["name"])
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO repos (name, source_type, source, status, phase, progress, message)"
            " VALUES (?, 'url', ?, 'pending', 'queued', 0, 'Queued')",
            (repo_name_from_url(url), url),
        )
        repo_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    _start_job(repo_id, _run_url_ingest, repo_id, url)
    return repo_id


def create_repo_from_zip(zip_bytes_path: Path, display_name: str) -> int:
    init_db()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO repos (name, source_type, source, status, phase, progress, message)"
            " VALUES (?, 'zip', ?, 'pending', 'queued', 0, 'Queued')",
            (display_name, display_name),
        )
        repo_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    _start_job(repo_id, _run_zip_ingest, repo_id, zip_bytes_path)
    return repo_id


def reanalyze_repo(repo_id: int) -> None:
    """Re-run ingestion (fetching first when the repo came from a URL)."""
    job = get_job(repo_id)
    if job is not None:
        raise IngestError("This repository is already being analysed.")
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError("Repository not found.")
    if row["source_type"] == "url":
        _start_job(repo_id, _run_url_ingest, repo_id, row["source"])
    else:
        gitdir = Path(row["gitdir"]) if row["gitdir"] else None
        _start_job(repo_id, _run_analysis_only, repo_id, gitdir)


def delete_repo(repo_id: int) -> None:
    job = get_job(repo_id)
    if job is not None:
        job.cancel()
        job.wait(timeout=60)
    conn = connect()
    try:
        conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        conn.commit()
    finally:
        conn.close()
    target = config.REPOS_DIR / str(repo_id)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    clear_cache()


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------

def _start_job(repo_id: int, target, *args) -> Job:
    job = Job(repo_id)
    thread = threading.Thread(target=_job_wrapper, args=(job, target, args), daemon=True)
    job.thread = thread
    _set_job(repo_id, job)
    thread.start()
    return job


def _job_wrapper(job: Job, target, args) -> None:
    try:
        target(job, *args)
    except Exception as exc:  # noqa: BLE001 - surface every failure to the UI
        log.exception("ingest job failed for repo %s", job.repo_id)
        message = str(exc) or exc.__class__.__name__
        if job.cancelled:
            message = "Analysis cancelled."
        _update_status(
            job.repo_id, status="error", phase=None, error=message, message=message
        )
    finally:
        _set_job(job.repo_id, None)


def _check_cancelled(job: Job) -> None:
    if job.cancelled:
        raise IngestError("Analysis cancelled.")


def _run_git_progress(
    job: Job, args: list[str], gitdir: Path | None, phase: str, base: float, span: float
) -> str:
    """Run a git command streaming `--progress` output into the status row."""
    return run_git_with_progress(
        args,
        gitdir,
        on_start=job.attach_proc,
        on_progress=lambda pct, _line: _update_status(
            job.repo_id,
            phase=phase,
            progress=base + span * pct / 100,
            message=f"{phase.capitalize()} ({pct}%)",
        ),
    )


def _run_url_ingest(job: Job, repo_id: int, url: str) -> None:
    target = config.REPOS_DIR / str(repo_id) / "repo.git"
    try:
        if target.exists():
            _update_status(repo_id, status="fetching", phase="fetching", progress=0.0, error=None,
                           message="Fetching latest changes")
            _run_git_progress(
                job,
                ["fetch", "--prune", "--tags", "origin", "+refs/heads/*:refs/heads/*"],
                target,
                "fetching",
                0.0,
                0.25,
            )
            try:
                symref = run_git_text(["ls-remote", "--symref", "origin", "HEAD"], target, timeout=120)
                for line in symref.splitlines():
                    if line.startswith("ref:") and line.endswith("HEAD"):
                        branch = line.split()[1]
                        run_git(["symbolic-ref", "HEAD", branch], target, check=False, timeout=30)
                        break
            except GitError:
                pass
        else:
            _update_status(repo_id, status="cloning", phase="cloning", progress=0.0, error=None,
                           message="Cloning (deep)…")
            target.parent.mkdir(parents=True, exist_ok=True)
            _run_git_progress(
                job, ["clone", "--bare", "--progress", "--", url, str(target)], None, "cloning", 0.0, 0.5
            )
        _check_cancelled(job)
        _update_status(repo_id, gitdir=str(target))
        _analyze_with_status(job, repo_id, target, base=0.5, span=0.5)
    except GitError as exc:
        raise IngestError(_friendly_git_error(str(exc), url)) from exc
    except AnalysisError as exc:
        raise IngestError(str(exc)) from exc


def _run_zip_ingest(job: Job, repo_id: int, zip_path: Path) -> None:
    work = config.REPOS_DIR / str(repo_id)
    try:
        _update_status(repo_id, status="analyzing", phase="unpacking", progress=0.02,
                       message="Unpacking upload")
        dest = work / "src"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        try:
            extract_zip(zip_path, dest)
        except ValueError as exc:
            raise IngestError(str(exc)) from exc
        _check_cancelled(job)

        gitdir, err = find_gitdir(dest)
        if err:
            raise IngestError(err)
        _update_status(repo_id, gitdir=str(gitdir))
        _analyze_with_status(job, repo_id, gitdir, base=0.05, span=0.95)
    finally:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_analysis_only(job: Job, repo_id: int, gitdir: Path | None) -> None:
    if gitdir is None or not Path(gitdir).exists():
        raise IngestError("The stored repository files are missing; re-upload the zip.")
    _analyze_with_status(job, repo_id, Path(gitdir), base=0.0, span=1.0)


def _analyze_with_status(job: Job, repo_id: int, gitdir: Path, base: float, span: float) -> None:
    conn = connect()
    try:
        merges = snapshot_merges(conn, repo_id)
    finally:
        conn.close()

    started = time.time()

    def progress(phase: str, fraction: float, message: str = "") -> None:
        _update_status(
            repo_id,
            status="analyzing",
            phase=phase,
            progress=base + span * fraction,
            message=message or phase,
        )

    stats = analyze_repo(repo_id, gitdir, progress=progress)

    conn = connect()
    try:
        restore_merges(conn, repo_id, merges)
    finally:
        conn.close()

    _update_status(
        repo_id,
        status="ready",
        phase=None,
        progress=1.0,
        head_hash=stats["head"],
        commit_count=stats["commits"],
        analyzed_at=_now(),
        error=None,
        message=(
            f"Analysed {stats['commits']:,} commits · {stats['objects']:,} objects · "
            f"{stats['change_rows']:,} change rows in {stats['duration_s']}s"
        ),
    )
    clear_cache()
    log.info("repo %s analysed in %.1fs", repo_id, time.time() - started)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _friendly_git_error(raw: str, url: str) -> str:
    low = raw.lower()
    if "could not read username" in low or "authentication failed" in low or "terminal prompts disabled" in low:
        return (
            "The repository requires authentication. Use a public repository URL "
            "(private repositories are not supported yet)."
        )
    if "not found" in low or "repository not found" in low or "does not appear to be a git repository" in low:
        return f"Repository not found at {url}. Check the URL and that the repository is public."
    if "unable to access" in low or "could not resolve host" in low:
        return "Could not reach the remote host. Check your network connection and the URL."
    return raw
