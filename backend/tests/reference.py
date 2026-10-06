"""Independent, deliberately slow reference implementation of the brief's metrics.

This is the correctness oracle for the app.  It deliberately uses a *different*
git plumbing path from ``rat.analyze`` (one ``git diff-tree`` process per commit
instead of a single streamed ``git log --numstat``), parses with different code,
and aggregates in plain Python instead of SQL, so that agreement is strong
evidence that both are right.

Definitions implemented (see the brief):
  l+/l-      per-file added/removed lines of one commit (rename-aware, -M50%,
             renames attributed to the new path, deletions recorded on the
             deleted path, binary files contribute nothing)
  growth     l+ - l-
  churn      l+ + l-
  dir(o)     sum over immediate children, recursively == sum over all
             descendant files
  n(H,o)     commits in H whose churn for o is > 0
  author     n(H,o,a), churn lambda(H,o,a), computed per commit identity

Usage:
  python3 tests/reference.py --gitdir DIR --out OUT.json [--from TS] [--to TS]
                             [--commits h1,h2,...] [--max-commits N] [--progress]

The output JSON contains, for the selected commit set H:
  h_count        |H|
  files          {path: {added, removed, modifications, touches}}
  dirs           {path: {added, removed, modifications}}
  file_authors   {path: {identity: {added, removed, modifications, touches}}}
  dir_authors    {path: {identity: {added, removed, modifications}}}
where identity is "name\\x1femail" of the raw commit author.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

NUL = b"\x00"
FS = "\x1f"


def git(gitdir: Path, *args: str) -> bytes:
    res = subprocess.run(
        ["git", *args],
        cwd=str(gitdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.decode('utf-8', 'replace')}")
    return res.stdout


def read_commits(gitdir: Path) -> list[tuple[str, int, str]]:
    """(hash, committer timestamp, identity) for every non-merge commit from HEAD."""
    out = git(
        gitdir,
        "-c", "i18n.logOutputEncoding=UTF-8",
        "log", "--no-merges", "--pretty=format:%H%x1f%ct%x1f%an%x1f%ae",
        "HEAD",
    ).decode("utf-8", "replace")
    commits = []
    for line in out.splitlines():
        if not line:
            continue
        h, ct, an, ae = line.split(FS)
        commits.append((h, int(ct), f"{an}{FS}{ae}"))
    return commits


def commit_deltas(gitdir: Path, commit: str) -> dict[str, tuple[int, int]]:
    """Rename-aware {post-image path: (added, removed)} for a single commit.

    ``--root`` makes the initial commit show its full content as additions.
    Binary files (``-``) are recorded as (0, 0) — not measured.

    ``diff.renameLimit`` is raised to the same value the app uses
    (``RAT_RENAME_LIMIT``): git silently *skips* rename detection on commits
    with more candidate paths than the limit, which would otherwise turn a
    large rename into a delete + add and make the oracle "disagree" with the
    app purely because of configuration.
    """
    limit = os.environ.get("RAT_RENAME_LIMIT", "20000")
    out = git(
        gitdir,
        "-c", f"diff.renameLimit={limit}",
        "diff-tree", "-r", "--no-commit-id", "--root", "-M50%", "--numstat", "-z", commit,
    )
    fields = out.split(NUL)
    deltas: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        parts = field.split(b"\t", 2)
        if len(parts) < 3:
            continue
        raw_add, raw_del, path = parts
        if path == b"":
            # rename record: the next two fields are the pre/post image paths
            new_path = fields[i + 1].decode("utf-8", "replace")
            i += 2
            path = new_path
        else:
            path = path.decode("utf-8", "replace")
        if raw_add == b"-" or raw_del == b"-":
            deltas[path] = (0, 0)
        else:
            deltas[path] = (int(raw_add), int(raw_del))
    return deltas


def _new_state() -> dict:
    return {
        "h_count": 0,
        "files": defaultdict(lambda: {"added": 0, "removed": 0, "modifications": 0, "touches": 0}),
        "dirs": defaultdict(lambda: {"added": 0, "removed": 0, "modifications": 0}),
        "file_authors": defaultdict(
            lambda: defaultdict(lambda: {"added": 0, "removed": 0, "modifications": 0, "touches": 0})
        ),
        "dir_authors": defaultdict(
            lambda: defaultdict(lambda: {"added": 0, "removed": 0, "modifications": 0})
        ),
    }


def _apply_commit(state: dict, deltas: dict[str, tuple[int, int]], ident: str) -> None:
    """Fold one commit's file deltas into a state (files, dir rollups, authors)."""
    state["h_count"] += 1
    rollup: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for path, (adds, dels) in deltas.items():
        fm = state["files"][path]
        fm["added"] += adds
        fm["removed"] += dels
        fm["touches"] += 1
        fam = state["file_authors"][path][ident]
        fam["added"] += adds
        fam["removed"] += dels
        fam["touches"] += 1
        if adds + dels > 0:
            fm["modifications"] += 1
            fam["modifications"] += 1
        p = path
        while True:
            i = p.rfind("/")
            key = p[:i] if i >= 0 else ""
            rollup[key][0] += adds
            rollup[key][1] += dels
            if key == "":
                break
            p = key

    for key, (adds, dels) in rollup.items():
        dm = state["dirs"][key]
        dm["added"] += adds
        dm["removed"] += dels
        dam = state["dir_authors"][key][ident]
        dam["added"] += adds
        dam["removed"] += dels
        if adds + dels > 0:
            dm["modifications"] += 1
            dam["modifications"] += 1


def _plain(state: dict) -> dict:
    return {
        "h_count": state["h_count"],
        "files": {p: dict(v) for p, v in state["files"].items()},
        "dirs": {p: dict(v) for p, v in state["dirs"].items()},
        "file_authors": {p: {i: dict(m) for i, m in v.items()} for p, v in state["file_authors"].items()},
        "dir_authors": {p: {i: dict(m) for i, m in v.items()} for p, v in state["dir_authors"].items()},
    }


def aggregate(
    gitdir: Path,
    commits: list[tuple[str, int, str]],
    in_filter,
    *,
    progress: bool = False,
) -> dict:
    state = _new_state()
    for n, (h, ct, ident) in enumerate(commits, start=1):
        if not in_filter(h, ct):
            continue
        _apply_commit(state, commit_deltas(gitdir, h), ident)
        if progress and n % 500 == 0:
            print(f"  … {n}/{len(commits)} commits scanned", file=sys.stderr)
    return _plain(state)


def aggregate_many(
    gitdir: Path,
    commits: list[tuple[str, int, str]],
    named_filters: dict[str, object],
    *,
    progress: bool = False,
) -> dict[str, dict]:
    """Aggregate several commit-set filters in a single scan (deltas computed once)."""
    states = {name: _new_state() for name in named_filters}
    for n, (h, ct, ident) in enumerate(commits, start=1):
        hits = [name for name, pred in named_filters.items() if pred(h, ct)]
        if hits:
            deltas = commit_deltas(gitdir, h)
            for name in hits:
                _apply_commit(states[name], deltas, ident)
        if progress and n % 500 == 0:
            print(f"  … {n}/{len(commits)} commits scanned", file=sys.stderr)
    return {name: _plain(state) for name, state in states.items()}


def object_metrics(ref: dict, path: str, is_file: bool) -> dict:
    """Reference metrics for an object.

    ``ref["dirs"]`` is already flattened: every file delta is added to all of
    its ancestor directories, so ``dirs[path]`` is the recursive sum over the
    directory's whole subtree — do not add descendant entries again.
    """
    if is_file:
        m = ref["files"].get(path)
        if not m:
            return {"added": 0, "removed": 0, "modifications": 0, "touches": 0, "growth": 0, "churn": 0}
        return {**m, "growth": m["added"] - m["removed"], "churn": m["added"] + m["removed"]}
    m = dict(ref["dirs"].get(path, {"added": 0, "removed": 0, "modifications": 0}))
    return {**m, "growth": m["added"] - m["removed"], "churn": m["added"] + m["removed"]}


def author_metrics(ref: dict, path: str, is_file: bool, identities: list[str]) -> dict:
    """Author metrics for one identity (or several merged) on one object.

    ``dir_authors`` is flattened the same way as ``dirs``.
    """
    if is_file:
        pool = ref["file_authors"].get(path, {})
    else:
        pool = ref["dir_authors"].get(path, {})
    out = {"added": 0, "removed": 0, "modifications": 0}
    for ident in identities:
        m = pool.get(ident)
        if m:
            out["added"] += m["added"]
            out["removed"] += m["removed"]
            out["modifications"] += m.get("modifications", 0)
    out["growth"] = out["added"] - out["removed"]
    out["churn"] = out["added"] + out["removed"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gitdir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--from", dest="from_ts", type=int, default=None)
    ap.add_argument("--to", dest="to_ts", type=int, default=None)
    ap.add_argument("--commits", default=None, help="comma-separated commit hashes (manual list)")
    ap.add_argument("--max-commits", type=int, default=None, help="debug: scan at most N commits")
    ap.add_argument("--progress", action="store_true")
    args = ap.parse_args()

    commits = read_commits(args.gitdir)
    if args.max_commits:
        commits = commits[: args.max_commits]

    if args.commits:
        wanted = {h.strip().lower() for h in args.commits.split(",") if h.strip()}
        in_filter = lambda h, ct: h.lower() in wanted  # noqa: E731
    else:
        lo, hi = args.from_ts, args.to_ts
        in_filter = (lambda h, ct: (lo is None or ct >= lo) and (hi is None or ct < hi))  # noqa: E731

    ref = aggregate(args.gitdir, commits, in_filter, progress=args.progress)
    ref["filter"] = {
        "from_ts": args.from_ts, "to_ts": args.to_ts,
        "commits": args.commits, "scanned": len(commits),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(ref))
    print(
        f"reference: |H|={ref['h_count']} files={len(ref['files'])} dirs={len(ref['dirs'])} "
        f"-> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
