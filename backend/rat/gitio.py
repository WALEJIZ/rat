"""Low level git process helpers, zip extraction and git-dir discovery."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

from . import config


class GitError(RuntimeError):
    """Raised when a git invocation fails."""


def git_bin() -> str:
    path = shutil.which("git")
    if not path:
        raise GitError("The 'git' executable was not found on PATH.")
    return path


def base_env() -> dict:
    env = dict(os.environ)
    env.update(
        {
            # Never hang waiting for credentials; surface an error instead.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "echo",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ADVICE": "0",
            # Hide local user identity from output-affecting config.
            "GIT_CONFIG_PARAMETERS": "",
        }
    )
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    return env


def mailmap_args(gitdir: str | Path | None) -> list[str]:
    """Extra ``-c`` arguments so ``%aN``/``%aE`` resolve ``.mailmap``.

    Bare repositories already fall back to ``HEAD:.mailmap``; for a git dir
    inside a work tree git only consults the worktree ``.mailmap`` when the
    process cwd happens to sit inside that work tree, so point at the file
    explicitly (missing files are silently ignored by git, so only pass it
    when the file exists).
    """
    if not gitdir:
        return []
    gd = Path(gitdir)
    if gd.name == ".git" and gd.is_dir():
        mm = gd.parent / ".mailmap"
        if mm.is_file():
            return ["-c", f"mailmap.file={mm}"]
    return []


def worktree_cwd(gitdir: str | Path | None) -> str | None:
    """Working directory that gives git the semantics of a real user clone.

    For a git dir inside a work tree, git resolves ``.gitattributes`` (and therefore
    the text/binary classification of every diff) relative to the process cwd: run
    from outside the work tree and checked-in attributes such as git.git's
    ``/t0110/url-* binary`` are silently ignored.  Running from the work tree root
    makes the analysis see exactly what a user sees inside their clone.  Bare
    repositories resolve in-tree attributes themselves, so they keep the
    inherited cwd.
    """
    if not gitdir:
        return None
    gd = Path(gitdir)
    if gd.name == ".git" and gd.is_dir():
        return str(gd.resolve().parent)
    return None


def _git_cmd(gitdir: str | Path | None, args: Iterable[str]) -> list[str]:
    cmd = [git_bin(), "--no-pager"]
    if gitdir:
        # Absolute: some callers change cwd (see worktree_cwd), and a relative
        # --git-dir would then be resolved against the wrong directory.
        cmd += ["--git-dir", str(Path(gitdir).resolve())]
    cmd += list(args)
    return cmd


def run_git(
    args: Iterable[str],
    gitdir: str | Path | None = None,
    *,
    check: bool = True,
    timeout: float | None = 120.0,
) -> subprocess.CompletedProcess:
    """Run a git command to completion and return the result."""
    proc = subprocess.run(
        _git_cmd(gitdir, args),
        capture_output=True,
        cwd=worktree_cwd(gitdir),
        env=base_env(),
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {err}")
    return proc


def run_git_text(args: Iterable[str], gitdir: str | Path | None = None, *, timeout: float | None = 120.0) -> str:
    return run_git(args, gitdir, timeout=timeout).stdout.decode("utf-8", "replace").strip()


def _drain_to_devnull(proc: subprocess.Popen) -> None:
    """stdout is discarded but must be drained so git can never block."""
    assert proc.stdout is not None
    while proc.stdout.read(1 << 16):
        pass


def run_git_with_progress(
    args: Iterable[str],
    gitdir: str | Path | None = None,
    *,
    on_progress=None,
    on_start=None,
) -> str:
    """Run a git command (`clone`/`fetch`) streaming its stderr progress.

    ``on_progress(percent, line)`` is called for every progress update;
    ``on_start(proc)`` receives the process so callers can cancel it.
    Returns the full stderr output.
    """
    proc = subprocess.Popen(
        _git_cmd(gitdir, args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        cwd=worktree_cwd(gitdir),
        env=base_env(),
    )
    if on_start:
        on_start(proc)
    threading.Thread(target=_drain_to_devnull, args=(proc,), daemon=True).start()

    chunks: list[bytes] = []
    assert proc.stderr is not None
    fd = proc.stderr.fileno()
    buf = b""
    while True:
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
        buf += data
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        if on_progress:
            for line in parts:
                m = re.search(rb"(\d+)%", line)
                if m:
                    on_progress(int(m.group(1)), line.decode("utf-8", "replace"))
    rc = proc.wait()
    text = b"".join(chunks).decode("utf-8", "replace")
    if rc != 0:
        raise GitError(f"git exited with code {rc}: {text.strip()[-2000:]}")
    return text


class GitStream:
    """Run a git command and stream its stdout in binary chunks.

    stderr is drained on a background thread so large warnings can never block
    the pipe, and is surfaced by :meth:`wait` when the command fails.
    """

    def __init__(self, args: Iterable[str], gitdir: str | Path | None = None):
        self._stderr_lines: list[bytes] = []
        self._proc = subprocess.Popen(
            _git_cmd(gitdir, args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1024 * 1024,
            cwd=worktree_cwd(gitdir),
            env=base_env(),
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr_lines.append(line)

    def chunks(self, size: int = 1 << 20) -> Iterator[bytes]:
        assert self._proc.stdout is not None
        stream = self._proc.stdout
        read = getattr(stream, "read1", stream.read)
        while True:
            chunk = read(size)
            if not chunk:
                break
            yield chunk

    @property
    def stdout(self):
        return self._proc.stdout

    def kill(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass

    def wait(self) -> None:
        rc = self._proc.wait()
        self._stderr_thread.join(timeout=5)
        if rc != 0:
            err = b"".join(self._stderr_lines).decode("utf-8", "replace").strip()
            raise GitError(f"git exited with code {rc}: {err[-2000:]}")


# --------------------------------------------------------------------------
# Repository discovery (used for zip uploads)
# --------------------------------------------------------------------------

_BARE_MARKERS = ("HEAD", "objects", "refs")


def _looks_like_git_dir(p: Path) -> bool:
    return p.is_dir() and all((p / m).exists() for m in _BARE_MARKERS)


def _resolve_dotgit_pointer(dotgit_file: Path) -> Path | None:
    try:
        text = dotgit_file.read_text("utf-8", "replace").strip()
    except OSError:
        return None
    if not text.lower().startswith("gitdir:"):
        return None
    target = text.split(":", 1)[1].strip()
    p = Path(target)
    if not p.is_absolute():
        p = (dotgit_file.parent / p).resolve()
    return p if p.exists() else None


def find_gitdir(root: Path) -> tuple[Path, str | None]:
    """Locate the git directory inside an extracted repository directory.

    Returns ``(gitdir, error)``. Supports:
      * a checked-out working tree (``.git`` directory or worktree file)
      * a bare repository (``HEAD`` + ``objects`` + ``refs``)
      * either of the above nested up to two levels deep (e.g. zips that
        wrap everything in a single top-level folder)
    """
    candidates: list[Path] = []

    def scan(d: Path, depth: int) -> None:
        if depth > 2 or not d.is_dir():
            return
        entry = d / ".git"
        if entry.is_dir():
            candidates.append(entry)
        elif entry.is_file():
            resolved = _resolve_dotgit_pointer(entry)
            if resolved:
                candidates.append(resolved)
            else:
                candidates.append(entry)  # keep for a precise error message
        elif _looks_like_git_dir(d):
            candidates.append(d)

    scan(root, 0)
    if not candidates:
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            children = []
        for child in children[:2000]:
            scan(child, 1)

    for cand in candidates:
        if cand.is_file():
            continue
        if cand.name == ".git" or _looks_like_git_dir(cand):
            common = _git_common_dir(cand)
            return common, None

    if candidates:
        return candidates[0], (
            "The uploaded zip contains a .git pointer to a location that does not exist "
            "(the repository needs to be zipped together with its real .git directory)."
        )
    return root, (
        "No git repository found in the uploaded zip. Zip the repository including its "
        "'.git' directory (or a bare clone) and try again."
    )


def _git_common_dir(gitdir: Path) -> Path:
    """Resolve the common git dir (handles worktree pointers)."""
    try:
        out = run_git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], gitdir, timeout=30
        ).stdout.decode("utf-8", "replace").strip()
        p = Path(out)
        if p.exists():
            return p
    except GitError:
        pass
    # Fallback for older git: read the 'commondir' file when present.
    commondir = gitdir / "commondir"
    if commondir.is_file():
        target = commondir.read_text("utf-8", "replace").strip()
        p = Path(target)
        if not p.is_absolute():
            p = (gitdir / p).resolve()
        if p.exists():
            return p
    return gitdir


# --------------------------------------------------------------------------
# Safe zip extraction
# --------------------------------------------------------------------------

def extract_zip(zip_path: Path, dest: Path) -> Path:
    """Extract a zip into ``dest`` with path-traversal and size guards."""
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > config.MAX_ZIP_ENTRIES:
            raise ValueError(f"Zip contains too many entries ({len(infos)}).")
        dest_resolved = dest.resolve()
        for info in infos:
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if any(p == ".." for p in parts) or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise ValueError(f"Unsafe path inside zip: {info.filename!r}")
            total += info.file_size
            if total > config.MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Zip expands beyond the configured size limit.")
            target = dest_resolved.joinpath(*parts)
            if not str(target).startswith(str(dest_resolved)):
                raise ValueError(f"Unsafe path inside zip: {info.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=1 << 20)
            except RuntimeError as exc:  # encrypted member
                raise ValueError(f"Cannot read zip entry {info.filename!r}: {exc}") from exc
    return dest
