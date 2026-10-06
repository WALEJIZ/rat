"""Cross-check the running RAT API against the independent reference implementation.

For a set of commit sets H (all history / a time window / a manual commit list)
and a set of objects (the root, top-level hotspots, a nested directory, files),
every metric returned by the API is compared with the reference computed by
``tests/reference.py`` (a different git plumbing path and a different
aggregation implementation).  Author metrics and ownership are compared too.

Usage:
  python3 tests/verify.py --repo cJSON --gitdir ~/rat-data/clones/cjson
  python3 tests/verify.py --repo redis --gitdir ... --max-commits 2000   # quick pass
Exit code 0 = every check passed, 1 = mismatches (printed).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from math import ceil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reference import (  # noqa: E402
    FS,
    aggregate_many,
    author_metrics,
    object_metrics,
    read_commits,
)


def api(base: str, method: str, path: str, body=None, timeout: float = 900.0):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        raise SystemExit(f"API error {method} {path}: {exc.code} {exc.read().decode()[:500]}")


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def check(self, label: str, app_val, ref_val, tolerance: float = 0.0) -> None:
        ok = (abs(app_val - ref_val) <= tolerance) if isinstance(app_val, (int, float)) and isinstance(ref_val, (int, float)) else app_val == ref_val
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(f"{label}: app={app_val!r} reference={ref_val!r}")

    def line(self, ok: bool, text: str) -> None:
        print(f"  {'✓' if ok else '✗'} {text}")


def filter_body(filt: dict, object_path: str, is_file: bool, **extra) -> dict:
    body = {
        "object": object_path,
        "object_type": "file" if is_file else "dir",
        "mode": filt["mode"],
        "from_ct": filt.get("from_ct"),
        "to_ct": filt.get("to_ct"),
        "commits": filt.get("commits"),
        "with_timeline": False,
        "with_breakdown": False,
    }
    body.update(extra)
    if not body["with_breakdown"]:
        body.pop("with_breakdown")
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--repo", required=True, help="repository name (substring, case-insensitive)")
    ap.add_argument("--gitdir", required=True, type=Path, help="a clone of the same repository")
    ap.add_argument("--top-files", type=int, default=4)
    ap.add_argument("--top-dirs", type=int, default=3)
    ap.add_argument("--max-commits", type=int, default=None, help="debug: cap the reference scan")
    ap.add_argument("--skip-authors", action="store_true")
    args = ap.parse_args()

    # ---- locate the repository in the app -----------------------------------
    repos = api(args.api, "GET", "/api/repos")
    name = args.repo.lower()
    repo = next((r for r in repos if name in r["name"].lower() and r["status"] == "ready"), None)
    if repo is None:
        raise SystemExit(f"no ready repository matching {args.repo!r} (have: {[r['name'] for r in repos]})")
    rid = repo["id"]
    summary = api(args.api, "GET", f"/api/repos/{rid}/summary")
    print(f"repo #{rid} {repo['name']}: {summary['commits']:,} commits, analysed {repo['analyzed_at']}")

    author_rows = api(args.api, "GET", f"/api/repos/{rid}/authors")["authors"]
    identities_by_author = {
        a["author_id"]: [f"{a['name']}{FS}{a['email']}"]
        + [f"{i['name']}{FS}{i['email']}" for i in a["identities"]]
        for a in author_rows
    }

    # ---- choose commit sets --------------------------------------------------
    lo, hi = summary["first_ct"], summary["last_ct"]
    span = max(1, hi - lo)
    window = {"mode": "range", "from_ct": lo + span // 3, "to_ct": lo + 2 * span // 3}
    filters = {"all": {"mode": "range"}, "window": window}

    total = api(args.api, "GET", f"/api/repos/{rid}/commits?per_page=1")["total"]
    per_page = 5
    pages = max(1, ceil(total / per_page))
    wanted_pages = sorted({1, (pages + 1) // 2, pages})
    hashes: list[str] = []
    for p in wanted_pages:
        page = api(args.api, "GET", f"/api/repos/{rid}/commits?page={p}&per_page={per_page}")
        hashes += [c["hash"] for c in page["commits"]]
    hashes = list(dict.fromkeys(hashes))
    filters["manual"] = {"mode": "list", "commits": hashes}
    print(f"filters: all | window {window['from_ct']}..{window['to_ct']} | manual {len(hashes)} commits")

    # ---- choose objects ------------------------------------------------------
    hotspots_files = api(args.api, "POST", f"/api/repos/{rid}/hotspots?is_file=true&limit={args.top_files}",
                         filter_body(filters["all"], "", False))["hotspots"]
    hotspots_dirs = api(args.api, "POST", f"/api/repos/{rid}/hotspots?is_file=false&limit={args.top_dirs + 2}",
                        filter_body(filters["all"], "", False))["hotspots"]
    objects: list[tuple[str, bool]] = [("", False)]
    for d in hotspots_dirs[: args.top_dirs]:
        objects.append((d["path"], False))
    for f in hotspots_files:
        objects.append((f["path"], True))
    # one nested deeper directory, if any top dir has children
    for d in hotspots_dirs[: args.top_dirs]:
        kids = api(args.api, "POST", f"/api/repos/{rid}/objects?parent={d['path']}&limit=50&sort=churn&order=desc",
                   filter_body(filters["all"], "", False))["children"]
        nested = next((k for k in kids if not k["is_file"]), None)
        if nested:
            objects.append((nested["path"], False))
            break
    seen = set()
    objects = [o for o in objects if not (o in seen or seen.add(o))]
    print(f"objects: {len(objects)} ({sum(1 for _, f in objects if f)} files, {sum(1 for _, f in objects if not f)} dirs)")

    # ---- reference scan ------------------------------------------------------
    commits = read_commits(args.gitdir)
    if args.max_commits:
        commits = commits[: args.max_commits]
    wanted = {h.lower() for h in hashes}
    named = {
        "all": lambda h, ct: True,
        "window": lambda h, ct: window["from_ct"] <= ct < window["to_ct"],
        "manual": lambda h, ct: h.lower() in wanted,
    }
    print(f"reference: scanning {len(commits):,} commits with git diff-tree …")
    t0 = time.time()
    refs = aggregate_many(args.gitdir, commits, named, progress=True)
    print(f"reference scan finished in {time.time() - t0:.1f}s")

    # ---- comparisons ---------------------------------------------------------
    rep = Report()
    for fname, filt in filters.items():
        ref = refs[fname]
        print(f"\n=== commit set [{fname}] — reference |H| = {ref['h_count']} ===")
        first = True
        for path, is_file in objects:
            body = filter_body(filt, path, is_file)
            app = api(args.api, "POST", f"/api/repos/{rid}/metrics", body)
            m = app["metrics"]
            r = object_metrics(ref, path, is_file)
            label = f"[{fname}] {'file' if is_file else 'dir'} {path or '<root>'!r}"
            before = rep.failed
            rep.check(f"{label} |H|", app["filter"]["count"], ref["h_count"])
            rep.check(f"{label} added", m["added"], r["added"])
            rep.check(f"{label} removed", m["removed"], r["removed"])
            rep.check(f"{label} growth", m["growth"], r["growth"])
            rep.check(f"{label} churn", m["churn"], r["churn"])
            rep.check(f"{label} modifications", m["modifications"], r["modifications"])
            if is_file:
                rep.check(f"{label} touches", m["touches"], r["touches"])
            ok = rep.failed == before
            rep.line(ok, f"{'file' if is_file else 'dir '} {path or '<root>':<44} "
                         f"|H|={app['filter']['count']:<7} +{m['added']:<9} -{m['removed']:<9} "
                         f"churn={m['churn']:<9} n={m['modifications']}")
            first = False

        # ---- author metrics --------------------------------------------------
        if args.skip_authors:
            continue
        for path, is_file in objects[: 1 + args.top_dirs]:
            app = api(args.api, "POST", f"/api/repos/{rid}/metrics",
                      filter_body(filt, path, is_file, with_breakdown=True))
            breakdown = app["author_breakdown"]
            obj_churn = app["metrics"]["churn"]
            ref_obj = object_metrics(ref, path, is_file)
            for entry in breakdown[:2]:
                aid = entry["author_id"]
                idents = identities_by_author.get(aid, [])
                r = author_metrics(ref, path, is_file, idents)
                label = f"[{fname}] author {entry['name']!r} on {path or '<root>'}"
                authed = api(args.api, "POST", f"/api/repos/{rid}/metrics",
                             filter_body(filt, path, is_file, authors=[aid]))
                am = authed["metrics"]
                before = rep.failed
                rep.check(f"{label} added", am["added"], r["added"])
                rep.check(f"{label} removed", am["removed"], r["removed"])
                rep.check(f"{label} churn", am["churn"], r["churn"])
                rep.check(f"{label} modifications", am["modifications"], r["modifications"])
                expected_own = (r["churn"] / ref_obj["churn"]) if ref_obj["churn"] else 0.0
                rep.check(f"{label} ownership", entry["ownership"], expected_own, tolerance=1e-9)
                rep.line(rep.failed == before,
                         f"author {entry['name'][:24]:<24} on {path or '<root>':<34} "
                         f"λ={am['churn']:<9} n={am['modifications']:<5} ω={entry['ownership'] * 100:.2f}%")

    print(f"\n{'=' * 72}\nchecks passed: {rep.passed}   failed: {rep.failed}")
    if rep.failures:
        print("\nMISMATCHES:")
        for f in rep.failures[:40]:
            print(f"  ✗ {f}")
        return 1
    print("ALL CHECKS PASSED — app and independent reference agree exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
