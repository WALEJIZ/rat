"""Edge-case tests on synthetic repositories.

Each scenario builds a tiny git repository with deterministic timestamps, runs
``rat.analyze.analyze_repo`` into a throw-away SQLite database, and asserts the
exact metric rows.  Scenarios cover the tricky parts of the brief:

  * initial commit (--root)
  * pure rename (no metric change), rename + edit (new path), sub-50% rewrite
    degrading to delete + add
  * deletion, binary files (not measured), file names containing tabs
  * merge commits excluded from H
  * time-window and manual commit-list filters
  * .mailmap merging
  * two analyses running concurrently on one database (id allocation)

Run:  python3 tests/edge_cases.py [--keep]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rat import authors as rat_authors  # noqa: E402
from rat import metrics as rat_metrics  # noqa: E402
from rat.analyze import analyze_repo  # noqa: E402
from rat.db import connect, init_db  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

# All synthetic timestamps are expressed relative to this epoch (2001-09-09) so
# that date strings stay in a comfortable range for git.
BASE_TS = 1_000_000_000


def expect(label: str, got, want) -> None:
    if got == want:
        PASSED.append(label)
    else:
        FAILED.append(f"{label}: got {got!r}, want {want!r}")


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    full_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    full_env.update(env or {})
    res = subprocess.run(
        ["git", *args], cwd=str(repo), env=full_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.decode()}")
    return res.stdout.decode("utf-8", "replace")


def new_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    return repo


def write(repo: Path, rel: str, content) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")


def commit(repo: Path, msg: str, ts: int, author: tuple[str, str] | None = None) -> str:
    from datetime import datetime, timezone

    date = datetime.fromtimestamp(BASE_TS + ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    if author:
        env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"] = author
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg, env=env)
    return git(repo, "rev-parse", "HEAD").strip()


# --------------------------------------------------------------------------
# analysis helpers
# --------------------------------------------------------------------------

def analyze(root: Path, repo: Path, repo_id: int = 1) -> tuple[Path, dict]:
    db = root / "rat.sqlite3"
    init_db(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO repos (id, name, source_type, source, status, gitdir)"
            " VALUES (?,?,?,?,?,?)",
            (repo_id, repo.name, "url", str(repo), "ready", str(repo)),
        )
        conn.commit()
    finally:
        conn.close()
    # analyze_repo takes the git dir itself (ingest passes a bare clone path);
    # for a work tree that is <worktree>/.git.
    return db, analyze_repo(repo_id, repo / ".git", db_path=db)


def totals(db: Path, path: str, is_file: int, repo_id: int = 1) -> tuple | None:
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT adds, dels, churn, modifications, touches FROM totals"
            " WHERE repo_id = ? AND path = ? AND is_file = ?",
            (repo_id, path, is_file),
        ).fetchone()
        return tuple(row) if row else None
    finally:
        conn.close()


def objects(db: Path, repo_id: int = 1) -> set[tuple[str, int]]:
    conn = connect(db)
    try:
        return {
            (r["path"], r["is_file"])
            for r in conn.execute("SELECT path, is_file FROM objects WHERE repo_id = ?", (repo_id,))
        }
    finally:
        conn.close()


def metrics_for(db: Path, repo_id: int, path: str, is_file: int, **hf_kwargs) -> dict:
    with_breakdown = bool(hf_kwargs.pop("with_breakdown", False))
    conn = connect(db)
    try:
        repo = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        hf = rat_metrics.HFilter(conn, repo_id, **hf_kwargs)
        return rat_metrics.object_metrics(
            conn, repo, path, is_file, hf, with_timeline=False, with_breakdown=with_breakdown
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

def scenario_basic(root: Path) -> None:
    repo = new_repo(root)
    write(repo, "a.txt", "l1\nl2\nl3\n")
    commit(repo, "initial", 1000)
    git(repo, "mv", "a.txt", "b.txt")
    commit(repo, "pure rename", 2000)
    write(repo, "b.txt", "l1\nl2\nl3\nl4\nl5\n")
    commit(repo, "edit b", 3000)

    db, stats = analyze(root, repo)
    expect("basic |H|", stats["commits"], 3)
    expect("basic a.txt", totals(db, "a.txt", 1), (3, 0, 3, 1, 1))
    expect("basic b.txt", totals(db, "b.txt", 1), (2, 0, 2, 1, 2))
    expect("basic root", totals(db, "", 0), (5, 0, 5, 2, 2))
    expect("basic objects", objects(db), {("", 0), ("a.txt", 1), ("b.txt", 1)})


def scenario_rename_edit(root: Path) -> None:
    repo = new_repo(root)
    lines = [f"line {i}" for i in range(10)]
    write(repo, "a.txt", "\n".join(lines) + "\n")
    commit(repo, "initial", 1000)
    git(repo, "mv", "a.txt", "b.txt")
    lines[4] = "changed line"
    write(repo, "b.txt", "\n".join(lines) + "\n")
    commit(repo, "rename + edit", 2000)

    db, _ = analyze(root, repo)
    expect("rename+edit b.txt", totals(db, "b.txt", 1), (1, 1, 2, 1, 1))
    expect("rename+edit a.txt", totals(db, "a.txt", 1), (10, 0, 10, 1, 1))
    expect("rename+edit root", totals(db, "", 0), (11, 1, 12, 2, 2))


def scenario_sub50(root: Path) -> None:
    repo = new_repo(root)
    write(repo, "f.txt", "".join(f"old {i}\n" for i in range(20)))
    commit(repo, "initial", 1000)
    git(repo, "mv", "f.txt", "g.txt")
    write(repo, "g.txt", "".join(f"brand new {i}!\n" for i in range(20)))
    commit(repo, "rewrite completely", 2000)

    db, _ = analyze(root, repo)
    # similarity << 50% -> delete + add, not a rename
    expect("sub50 f.txt", totals(db, "f.txt", 1), (20, 20, 40, 2, 2))
    expect("sub50 g.txt", totals(db, "g.txt", 1), (20, 0, 20, 1, 1))
    # root modifications = commits where ANY descendant churned (commit 2 counts once)
    expect("sub50 root", totals(db, "", 0), (40, 20, 60, 2, 2))


def scenario_binary_and_delete(root: Path) -> None:
    repo = new_repo(root)
    write(repo, "text.txt", "one\ntwo\n")
    write(repo, "blob.bin", b"\x00\x01binary\x00data")
    write(repo, "gone.txt", "x\ny\nz\nw\nv\n")
    commit(repo, "initial", 1000)
    write(repo, "blob.bin", b"\x00\x01binary v2\x00\x00")
    commit(repo, "binary edit", 2000)
    git(repo, "rm", "-q", "gone.txt")
    commit(repo, "delete gone", 3000)

    db, _ = analyze(root, repo)
    expect("binary blob.bin", totals(db, "blob.bin", 1), (0, 0, 0, 0, 2))
    expect("delete gone.txt", totals(db, "gone.txt", 1), (5, 5, 10, 2, 2))
    expect("binary text.txt", totals(db, "text.txt", 1), (2, 0, 2, 1, 1))
    # commit 2 is binary-only (churn 0) -> does not modify the root
    expect("binary root", totals(db, "", 0), (7, 5, 12, 2, 2))
    conn = connect(db)
    try:
        row = conn.execute("SELECT binary FROM objects WHERE path = 'blob.bin'").fetchone()
        expect("binary flag set", row[0], 1)
    finally:
        conn.close()


def scenario_attributes(root: Path) -> None:
    """Committed .gitattributes drives the text/binary classification.

    git.git declares its ``t/t0110/url-*`` fixtures ``binary`` although they
    hold no NUL bytes; a real user's clone therefore does not measure them.
    The analysis must follow the same classification.
    """
    repo = new_repo(root)
    write(repo, ".gitattributes", "data.bin binary\n")
    write(repo, "data.bin", "alpha\nbeta\ngamma\n")
    write(repo, "code.txt", "one\n")
    commit(repo, "add data.bin declared binary", 1000)
    write(repo, "data.bin", "alpha\nbeta\ngamma\ndelta\n")
    commit(repo, "extend data.bin", 2000)

    db, _ = analyze(root, repo)
    # `data.bin` holds plain text but is declared binary: not measured.
    expect("attr data.bin", totals(db, "data.bin", 1), (0, 0, 0, 0, 2))
    expect("attr code.txt", totals(db, "code.txt", 1), (1, 0, 1, 1, 1))
    # root: commit 1 = .gitattributes + code.txt (2 added); commit 2 has zero
    # churn (binary-only) so it neither modifies nor touches the root.
    expect("attr root", totals(db, "", 0), (2, 0, 2, 1, 1))
    conn = connect(db)
    try:
        row = conn.execute("SELECT binary FROM objects WHERE path = 'data.bin'").fetchone()
        expect("attr binary flag", row[0], 1)
    finally:
        conn.close()


def scenario_weird_paths(root: Path) -> None:
    repo = new_repo(root)
    write(repo, "we\tird.txt", "1\n2\n3\n4\n")
    write(repo, "dir with space/inner.txt", "a\nb\n")
    commit(repo, "weird names", 1000)

    db, _ = analyze(root, repo)
    expect("tab filename", totals(db, "we\tird.txt", 1), (4, 0, 4, 1, 1))
    expect("spaced dir", totals(db, "dir with space", 0), (2, 0, 2, 1, 1))
    expect(
        "weird objects",
        objects(db),
        {("", 0), ("we\tird.txt", 1), ("dir with space", 0), ("dir with space/inner.txt", 1)},
    )


def scenario_merge_and_filters(root: Path) -> None:
    repo = new_repo(root)
    write(repo, "base.txt", "a\nb\n")
    c1 = commit(repo, "base", 1000)

    git(repo, "checkout", "-q", "-b", "side")
    write(repo, "side.txt", "x\ny\nz\n")
    c2 = commit(repo, "side work", 2000)

    git(repo, "checkout", "-q", "main")
    write(repo, "main.txt", "1\n2\n3\n4\n")
    c3 = commit(repo, "main work", 3000)

    git(repo, "merge", "--no-ff", "-q", "-m", "merge side", "side", env={
        "GIT_AUTHOR_DATE": "2001-09-09T01:06:44+00:00",
        "GIT_COMMITTER_DATE": "2001-09-09T01:06:44+00:00",
    })

    db, stats = analyze(root, repo)
    expect("merge |H| excludes merge", stats["commits"], 3)

    # commit-set filters
    allm = metrics_for(db, 1, "", 0)["metrics"]
    expect("filters all added", allm["added"], 9)
    expect("filters all modifications", allm["modifications"], 3)
    expect("filters all |H|", allm["commits"], 3)

    rangem = metrics_for(db, 1, "", 0, mode="range", from_ct=BASE_TS + 3000)["metrics"]
    expect("filters from 3000 added", rangem["added"], 4)
    expect("filters from 3000 |H|", rangem["commits"], 1)

    rangem2 = metrics_for(db, 1, "", 0, mode="range", from_ct=BASE_TS + 1000, to_ct=BASE_TS + 3000)["metrics"]
    expect("filters window added", rangem2["added"], 5)
    expect("filters window |H|", rangem2["commits"], 2)

    listm = metrics_for(db, 1, "", 0, mode="list", commit_hashes=[c1, c3])["metrics"]
    expect("filters list added", listm["added"], 6)
    expect("filters list modifications", listm["modifications"], 2)
    expect("filters list |H|", listm["commits"], 2)

    # abbreviated hash support
    shortm = metrics_for(db, 1, "", 0, mode="list", commit_hashes=[c1[:8]])["metrics"]
    expect("filters abbreviated hash", shortm["added"], 2)

    res = metrics_for(db, 1, "", 0, with_breakdown=True)
    expect("single author ownership", res["author_breakdown"][0]["ownership"], 1.0)


def scenario_mailmap(root: Path) -> None:
    repo = new_repo(root)
    write(repo, ".mailmap", "Test Author <test@example.com> Other Name <other@example.com>\n")
    write(repo, "a.txt", "1\n2\n")
    commit(repo, "by test author", 1000)
    write(repo, "a.txt", "1\n2\n3\n4\n")
    commit(repo, "by other identity", 2000, author=("Other Name", "other@example.com"))

    db, _ = analyze(root, repo)
    conn = connect(db)
    try:
        before = rat_metrics.list_authors(conn, 1)
        expect("mailmap authors before", len(before), 2)
        result = rat_authors.apply_mailmap(conn, 1)
        expect("mailmap merged", result["merged"], 1)
        after = rat_metrics.list_authors(conn, 1)
        expect("mailmap authors after", len(after), 1)
        expect("mailmap commits aggregated", after[0]["commits"], 2)
        # churn = a.txt (2) + .mailmap (1) + a.txt edit (2) = 5
        expect("mailmap churn aggregated", after[0]["churn"], 5)
        expect("mailmap identity listed", len(after[0]["identities"]), 1)
        # unmerge restores both identities
        rat_authors.unmerge_authors(conn, 1, [after[0]["author_id"]])
        expect("unmerge restores", len(rat_metrics.list_authors(conn, 1)), 2)
    finally:
        conn.close()


def scenario_concurrent(root: Path) -> None:
    """Two analyses in parallel on one database must not collide on commit ids."""
    db = root / "rat.sqlite3"
    init_db(db)
    conn = connect(db)
    try:
        for rid, name in ((1, "ra"), (2, "rb")):
            conn.execute(
                "INSERT INTO repos (id, name, source_type, source, status) VALUES (?,?,?,?,?)",
                (rid, name, "url", name, "ready"),
            )
        conn.commit()
    finally:
        conn.close()

    repos = []
    for n in range(2):
        r = root / f"r{n}"
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        for i in range(30):
            write(r, f"file{i % 7}.txt", "".join(f"line {j}\n" for j in range(i + 1)))
            commit(r, f"c{i}", 1000 + i)
        repos.append(r)

    errors: list[Exception] = []
    stats: dict[int, dict] = {}

    def run(rid: int, path: Path) -> None:
        try:
            stats[rid] = analyze_repo(rid, path / ".git", db_path=db)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(rid, p)) for rid, p in zip((1, 2), repos)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expect("concurrent analyses: no errors", [str(e) for e in errors], [])
    expect("concurrent analyses: counts", [stats[1]["commits"], stats[2]["commits"]], [30, 30])
    conn = connect(db)
    try:
        n_ids = conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        n_distinct = conn.execute("SELECT COUNT(DISTINCT id) FROM commits").fetchone()[0]
        expect("concurrent analyses: unique ids", n_ids, n_distinct)
    finally:
        conn.close()


SCENARIOS = [
    ("basic-rename", scenario_basic),
    ("rename-edit", scenario_rename_edit),
    ("sub-50-percent", scenario_sub50),
    ("binary-and-delete", scenario_binary_and_delete),
    ("gitattributes", scenario_attributes),
    ("weird-paths", scenario_weird_paths),
    ("merge-and-filters", scenario_merge_and_filters),
    ("mailmap", scenario_mailmap),
    ("concurrent", scenario_concurrent),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the temporary directories")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="rat-edge-"))
    try:
        for name, fn in SCENARIOS:
            root = tmp / name
            root.mkdir()
            print(f"—— {name}")
            fn(root)
        print(f"\n{'=' * 60}\npassed: {len(PASSED)}   failed: {len(FAILED)}")
        if FAILED:
            print("\nFAILURES:")
            for f in FAILED:
                print(f"  ✗ {f}")
            return 1
        print("all edge cases passed")
        return 0
    finally:
        if args.keep:
            print(f"(kept {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
