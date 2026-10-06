"""Metric queries - direct SQL implementations of the brief's formulas.

Glossary (matching the brief):
  H      commit set: non-merge commits reachable from HEAD, restricted by a
         time range (``H_{i,j}``) or a manually selected list of commits
  o      object: a file ``(path, is_file=1)`` or directory ``(path, is_file=0)``;
         the repository root is the directory ``""``
  l+     added lines, l- removed lines
  delta  growth  = l+ - l-
  churn  = l+ + l-
  n      modifications: commits in H with churn(o) > 0
  eta    modification frequency = n / |H|
  rho    churn rate = churn / |H|
  author metrics: n(a), churn(a), ownership = churn(a) / churn(o)

Directory and repository metrics come "for free" because ``rat.analyze`` stores
directory rows pre-aggregated over all descendants.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

DAY = 86400


# --------------------------------------------------------------------------
# Commit sets / filters
# --------------------------------------------------------------------------

class FilterError(ValueError):
    pass


class HFilter:
    """A commit set H plus an optional author filter."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        repo_id: int,
        *,
        mode: str = "range",
        from_ct: int | None = None,
        to_ct: int | None = None,
        commit_hashes: Iterable[str] | None = None,
        author_ids: Iterable[int] | None = None,
    ):
        if mode not in ("range", "list"):
            raise FilterError(f"Unknown commit-set mode: {mode!r}")
        self.conn = conn
        self.repo_id = repo_id
        self.mode = mode
        self.from_ct = from_ct
        self.to_ct = to_ct
        self.author_ids = sorted({int(a) for a in (author_ids or [])})
        self.commit_hashes: list[str] = []
        self.matched_hashes: list[str] = []
        self.unknown_hashes: list[str] = []

        if mode == "list":
            hashes = [h.strip().lower() for h in (commit_hashes or []) if h and h.strip()]
            self.commit_hashes = hashes
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _hs (commit_id INTEGER PRIMARY KEY)")
            conn.execute("DELETE FROM _hs")
            if hashes:
                exact: dict[str, int] = {}
                for i in range(0, len(hashes), 500):
                    chunk = hashes[i : i + 500]
                    qs = ",".join("?" * len(chunk))
                    for row in conn.execute(
                        f"SELECT id, hash FROM commits WHERE repo_id = ? AND hash IN ({qs})",
                        (repo_id, *chunk),
                    ):
                        exact[row["hash"]] = row["id"]
                # Abbreviated hashes: resolve unique prefixes so users can paste
                # `git log --oneline` output directly.
                ambiguous: list[str] = []
                unresolved: list[str] = []
                resolved: list[str] = []
                for h in hashes:
                    if h in exact:
                        resolved.append(h)
                        continue
                    if len(h) >= 40:
                        unresolved.append(h)
                        continue
                    rows = conn.execute(
                        "SELECT id, hash FROM commits WHERE repo_id = ? AND hash LIKE ? || '%' LIMIT 3",
                        (repo_id, h),
                    ).fetchall()
                    if len(rows) == 1:
                        exact[rows[0]["hash"]] = rows[0]["id"]
                        resolved.append(rows[0]["hash"])
                    elif rows:
                        ambiguous.append(h)
                    else:
                        unresolved.append(h)
                if ambiguous:
                    sample = ", ".join(ambiguous[:5])
                    raise FilterError(
                        f"{len(ambiguous)} abbreviated commit hash(es) are ambiguous: {sample}"
                        + ("..." if len(ambiguous) > 5 else "")
                    )
                self.unknown_hashes = unresolved
                conn.executemany(
                    "INSERT OR IGNORE INTO _hs (commit_id) VALUES (?)",
                    [(cid,) for cid in set(exact.values())],
                )
                self.matched_hashes = resolved
            if self.unknown_hashes:
                sample = ", ".join(self.unknown_hashes[:5])
                raise FilterError(
                    f"{len(self.unknown_hashes)} commit(s) not found in this repository: {sample}"
                    + ("..." if len(self.unknown_hashes) > 5 else "")
                )

    # -- SQL fragments ---------------------------------------------------

    def commits_where(self, alias: str = "") -> tuple[str, list]:
        """WHERE fragment (without leading WHERE) for the commits table."""
        col = f"{alias}." if alias else ""
        parts = [f"{col}repo_id = ?"]
        params: list[Any] = [self.repo_id]
        if self.mode == "range":
            if self.from_ct is not None:
                parts.append(f"{col}ct >= ?")
                params.append(self.from_ct)
            if self.to_ct is not None:
                parts.append(f"{col}ct < ?")
                params.append(self.to_ct)
        else:
            parts.append(f"{col}id IN (SELECT commit_id FROM _hs)")
        return " AND ".join(parts), params

    def changes_where(self, alias: str = "c") -> tuple[str, list]:
        """WHERE fragment (without leading WHERE) for the changes table.

        Uses the denormalised ``ct`` column so no join is needed for time
        ranges; manual commit lists use the ``_hs`` temp table.
        """
        parts = [f"{alias}.repo_id = ?"]
        params: list[Any] = [self.repo_id]
        if self.mode == "range":
            if self.from_ct is not None:
                parts.append(f"{alias}.ct >= ?")
                params.append(self.from_ct)
            if self.to_ct is not None:
                parts.append(f"{alias}.ct < ?")
                params.append(self.to_ct)
        else:
            parts.append(f"{alias}.commit_id IN (SELECT commit_id FROM _hs)")
        return " AND ".join(parts), params

    def author_join(self) -> str:
        return (
            " JOIN commits m ON m.id = c.commit_id AND m.repo_id = c.repo_id"
            " JOIN authors a ON a.id = m.author_id "
            if self.author_ids
            else ""
        )

    def author_cond(self) -> tuple[str, list]:
        if not self.author_ids:
            return "", []
        qs = ",".join("?" * len(self.author_ids))
        return f" AND COALESCE(a.canonical_id, a.id) IN ({qs})", list(self.author_ids)

    @property
    def is_all(self) -> bool:
        return self.mode == "range" and self.from_ct is None and self.to_ct is None and not self.author_ids

    @property
    def signature(self) -> str:
        if self.mode == "range":
            sig = f"r:{self.from_ct}:{self.to_ct}"
        else:
            joined = ",".join(sorted(self.matched_hashes))
            sig = f"l:{len(self.matched_hashes)}:{hashlib.md5(joined.encode()).hexdigest()}"
        if self.author_ids:
            sig += f":a{','.join(map(str, self.author_ids))}"
        return sig

    def commit_count(self) -> int:
        where, params = self.commits_where()
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM commits WHERE {where}", params
        ).fetchone()
        return row["n"]

    def bounds(self) -> tuple[int | None, int | None]:
        where, params = self.commits_where()
        row = self.conn.execute(
            f"SELECT MIN(ct) AS lo, MAX(ct) AS hi FROM commits WHERE {where}", params
        ).fetchone()
        return row["lo"], row["hi"]

    def describe(self) -> dict:
        lo, hi = self.bounds()
        return {
            "mode": self.mode,
            "from": self.from_ct,
            "to": self.to_ct,
            "from_iso": iso(self.from_ct) if self.from_ct is not None else None,
            "to_iso": iso(self.to_ct) if self.to_ct is not None else None,
            "author_ids": self.author_ids,
            "count": self.commit_count(),
            "first_ct": lo,
            "last_ct": hi,
            "first_iso": iso(lo),
            "last_iso": iso(hi),
        }


def iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Tiny LRU cache for expensive aggregates
# --------------------------------------------------------------------------

_cache: "OrderedDict[str, Any]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def _cache_set(key: str, value) -> None:
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > config.CACHE_ENTRIES:
            _cache.popitem(last=False)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------------------
# Authors
# --------------------------------------------------------------------------

def author_directory(conn: sqlite3.Connection, repo_id: int) -> dict[int, dict]:
    """Return canonical-author id -> {name, email, identities: [...], raw_ids: [...]}."""
    out: dict[int, dict] = {}
    rows = conn.execute(
        "SELECT id, name, email, canonical_id, name_mm, email_mm FROM authors WHERE repo_id = ? ORDER BY id",
        (repo_id,),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        root = r["canonical_id"] if r["canonical_id"] in by_id else r["id"]
        entry = out.setdefault(
            root,
            {
                "author_id": root,
                "name": by_id[root]["name"] if root in by_id else r["name"],
                "email": by_id[root]["email"] if root in by_id else r["email"],
                "identities": [],
                "raw_ids": [],
            },
        )
        entry["raw_ids"].append(r["id"])
        if r["id"] != root:
            entry["identities"].append({"name": r["name"], "email": r["email"]})
    return out


def list_authors(conn: sqlite3.Connection, repo_id: int) -> list[dict]:
    """Author list with per-author aggregates over the full history."""
    authors = author_directory(conn, repo_id)
    stats: dict[int, dict] = {
        aid: {"commits": 0, "added": 0, "removed": 0, "churn": 0, "modifications": 0}
        for aid in authors
    }
    for row in conn.execute(
        """SELECT COALESCE(a.canonical_id, a.id) AS aid, COUNT(*) AS commits
             FROM commits m JOIN authors a ON a.id = m.author_id
            WHERE m.repo_id = ? GROUP BY aid""",
        (repo_id,),
    ):
        if row["aid"] in stats:
            stats[row["aid"]]["commits"] = row["commits"]
    for row in conn.execute(
        """SELECT COALESCE(a.canonical_id, a.id) AS aid,
                  SUM(c.adds) AS adds, SUM(c.dels) AS dels,
                  SUM(CASE WHEN c.adds + c.dels > 0 THEN 1 ELSE 0 END) AS mods
             FROM changes c
             JOIN commits m ON m.id = c.commit_id AND m.repo_id = c.repo_id
             JOIN authors a ON a.id = m.author_id
            WHERE c.repo_id = ? AND c.path = '' AND c.is_file = 0
            GROUP BY aid""",
        (repo_id,),
    ):
        if row["aid"] in stats:
            stats[row["aid"]].update(
                added=row["adds"] or 0,
                removed=row["dels"] or 0,
                churn=(row["adds"] or 0) + (row["dels"] or 0),
                modifications=row["mods"] or 0,
            )
    total_churn = sum(s["churn"] for s in stats.values()) or 0
    result = []
    for aid, info in authors.items():
        s = stats[aid]
        result.append(
            {
                **info,
                **s,
                "ownership": (s["churn"] / total_churn) if total_churn else 0.0,
            }
        )
    result.sort(key=lambda a: (-a["churn"], a["name"]))
    return result


# --------------------------------------------------------------------------
# Core object metrics
# --------------------------------------------------------------------------

_METRIC_FIELDS = """
        COALESCE(SUM(c.adds), 0)                                        AS adds,
        COALESCE(SUM(c.dels), 0)                                        AS dels,
        COALESCE(SUM(c.adds + c.dels), 0)                               AS churn,
        COALESCE(SUM(CASE WHEN c.adds + c.dels > 0 THEN 1 ELSE 0 END),0) AS mods,
        COUNT(*)                                                        AS touches,
        MIN(c.ct)                                                       AS first_ct,
        MAX(c.ct)                                                       AS last_ct
"""


def _object_rows(
    conn: sqlite3.Connection,
    repo: sqlite3.Row,
    path: str,
    is_file: int,
    hf: HFilter,
    *,
    group_by_author: bool = False,
) -> list[sqlite3.Row]:
    where, params = hf.changes_where("c")
    cond, extra = hf.author_cond()
    join = (
        " JOIN commits m ON m.id = c.commit_id AND m.repo_id = c.repo_id JOIN authors a ON a.id = m.author_id "
        if (group_by_author or hf.author_ids)
        else ""
    )
    sql = (
        "SELECT " + _METRIC_FIELDS
        + (", COALESCE(a.canonical_id, a.id) AS aid" if group_by_author else "")
        + f" FROM changes c{join}"
        + f" WHERE {where} AND c.path = ? AND c.is_file = ?{cond}"
    )
    params = [*params, path, is_file, *extra]
    if group_by_author:
        sql += " GROUP BY aid"
    return conn.execute(sql, params).fetchall()


def object_metrics(
    conn: sqlite3.Connection,
    repo: sqlite3.Row,
    path: str,
    is_file: int,
    hf: HFilter,
    *,
    with_timeline: bool = True,
    with_breakdown: bool = True,
    bucket_seconds: int | None = None,
) -> dict:
    """All metrics for one object over commit set H (formulas from the brief)."""
    cache_key = f"obj:{repo['id']}:{repo['analyzed_at']}:{path}:{is_file}:{hf.signature}:{int(with_timeline)}:{int(with_breakdown)}:{bucket_seconds}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    h_count = hf.commit_count()

    # Fast path: unfiltered history -> per-object totals are precomputed.
    if hf.is_all and not hf.author_ids:
        row = conn.execute(
            "SELECT * FROM totals WHERE repo_id = ? AND path = ? AND is_file = ?",
            (repo["id"], path, is_file),
        ).fetchone()
        adds = row["adds"] if row else 0
        dels = row["dels"] if row else 0
        mods = row["modifications"] if row else 0
        touches = row["touches"] if row else 0
        first_ct = row["first_ct"] if row else None
        last_ct = row["last_ct"] if row else None
    else:
        rows = _object_rows(conn, repo, path, is_file, hf)
        r = rows[0] if rows else None
        adds = r["adds"] if r else 0
        dels = r["dels"] if r else 0
        mods = r["mods"] if r else 0
        touches = r["touches"] if r else 0
        first_ct = r["first_ct"] if r else None
        last_ct = r["last_ct"] if r else None

    churn = adds + dels
    result: dict[str, Any] = {
        "object": {
            "path": path,
            "type": "file" if is_file else "dir",
            "name": path.rsplit("/", 1)[-1] if path else "repository (root)",
        },
        "filter": hf.describe(),
        "metrics": {
            "added": adds,
            "removed": dels,
            "growth": adds - dels,
            "churn": churn,
            "modifications": mods,
            "modification_frequency": (mods / h_count) if h_count else 0.0,
            "churn_rate": (churn / h_count) if h_count else 0.0,
            "commits": h_count,
            "touches": touches,
            "first_ct": first_ct,
            "last_ct": last_ct,
            "first_iso": iso(first_ct),
            "last_iso": iso(last_ct),
        },
        "author_metrics": None,
        "author_breakdown": [],
        "timeline": [],
    }

    # -- per-author breakdown -------------------------------------------
    if with_breakdown:
        rows = _object_rows(conn, repo, path, is_file, hf, group_by_author=True)
        directory = author_directory(conn, repo["id"])
        breakdown = []
        for r in rows:
            info = directory.get(r["aid"], {"name": "?", "email": "?"})
            a_churn = (r["adds"] or 0) + (r["dels"] or 0)
            breakdown.append(
                {
                    "author_id": r["aid"],
                    "name": info.get("name", "?"),
                    "email": info.get("email", "?"),
                    "added": r["adds"] or 0,
                    "removed": r["dels"] or 0,
                    "growth": (r["adds"] or 0) - (r["dels"] or 0),
                    "churn": a_churn,
                    "modifications": r["mods"] or 0,
                    "ownership": (a_churn / churn) if churn else 0.0,
                }
            )
        breakdown.sort(key=lambda x: (-x["churn"], x["name"]))
        result["author_breakdown"] = breakdown[:100]

        if hf.author_ids:
            selected = [b for b in breakdown if b["author_id"] in hf.author_ids]
            if selected:
                agg = {
                    "author_ids": hf.author_ids,
                    "name": " + ".join(sorted({b["name"] for b in selected}))[:120],
                    "added": sum(b["added"] for b in selected),
                    "removed": sum(b["removed"] for b in selected),
                    "growth": sum(b["growth"] for b in selected),
                    "churn": sum(b["churn"] for b in selected),
                    "modifications": sum(b["modifications"] for b in selected),
                    "ownership": sum(b["ownership"] for b in selected),
                }
                w, p = hf.commits_where("m")
                author_in = ",".join("?" * len(hf.author_ids))
                agg["commits"] = conn.execute(
                    f"""SELECT COUNT(*) AS n FROM commits m JOIN authors a ON a.id = m.author_id
                        WHERE {w} AND COALESCE(a.canonical_id, a.id) IN ({author_in})""",
                    p + list(hf.author_ids),
                ).fetchone()["n"]
                result["author_metrics"] = agg

    # -- timeline --------------------------------------------------------
    if with_timeline:
        result["timeline"] = object_timeline(conn, repo, path, is_file, hf, bucket_seconds)

    _cache_set(cache_key, result)
    return result


def pick_bucket(span_seconds: float) -> int:
    if span_seconds <= 60 * DAY:
        return DAY
    if span_seconds <= 4 * 365 * DAY:
        return 7 * DAY
    return 30 * DAY


def object_timeline(
    conn: sqlite3.Connection,
    repo: sqlite3.Row,
    path: str,
    is_file: int,
    hf: HFilter,
    bucket_seconds: int | None = None,
) -> list[dict]:
    lo, hi = hf.bounds()
    if bucket_seconds is None:
        bucket_seconds = pick_bucket(max(1, (hi or 0) - (lo or 0)))
    rows = _timeline_query(conn, hf, path, is_file, bucket_seconds)

    # commits per bucket (all commits of H, regardless of object activity)
    cw, cp = hf.commits_where("m")
    commit_rows = conn.execute(
        f"SELECT (m.ct / ?) AS bucket, COUNT(*) AS n FROM commits m WHERE {cw} GROUP BY bucket ORDER BY bucket",
        [bucket_seconds, *cp],
    ).fetchall()
    commits_by_bucket = {r["bucket"]: r["n"] for r in commit_rows}

    buckets = sorted(set(commits_by_bucket) | {r["bucket"] for r in rows})
    by_bucket = {r["bucket"]: r for r in rows}
    out = []
    for b in buckets:
        r = by_bucket.get(b)
        out.append(
            {
                "t": b * bucket_seconds,
                "t_iso": iso(b * bucket_seconds),
                "commits": commits_by_bucket.get(b, 0),
                "added": (r["adds"] or 0) if r else 0,
                "removed": (r["dels"] or 0) if r else 0,
                "churn": ((r["adds"] or 0) + (r["dels"] or 0)) if r else 0,
                "growth": ((r["adds"] or 0) - (r["dels"] or 0)) if r else 0,
                "modifications": (r["mods"] or 0) if r else 0,
            }
        )
    return out


def _timeline_query(
    conn: sqlite3.Connection,
    hf: HFilter,
    path: str,
    is_file: int,
    bucket_seconds: int,
) -> list[sqlite3.Row]:
    where, params = hf.changes_where("c")
    cond, extra = hf.author_cond()
    sql = f"""SELECT (c.ct / ?) AS bucket,
                   SUM(c.adds) AS adds, SUM(c.dels) AS dels,
                   SUM(CASE WHEN c.adds + c.dels > 0 THEN 1 ELSE 0 END) AS mods,
                   COUNT(DISTINCT c.commit_id) AS commits
              FROM changes c{hf.author_join()}
             WHERE {where} AND c.path = ? AND c.is_file = ?{cond}
             GROUP BY bucket ORDER BY bucket"""
    return conn.execute(sql, [bucket_seconds, *params, path, is_file, *extra]).fetchall()


# --------------------------------------------------------------------------
# Trees, children, hotspots
# --------------------------------------------------------------------------

def prefix_range(parent: str) -> tuple[str, str] | None:
    """Lexicographic range covering every descendant of ``parent``."""
    if parent == "":
        return None
    lo = parent + "/"
    hi = parent + "0"
    return lo, hi


def _prefix_cond(parent: str) -> tuple[str, list[str]]:
    rng = prefix_range(parent)
    if rng is None:
        return "c.path <> ''", []
    return "c.path >= ? AND c.path < ?", [rng[0], rng[1]]


def aggregated_objects(
    conn: sqlite3.Connection,
    repo: sqlite3.Row,
    hf: HFilter,
    *,
    parent: str | None = None,
    is_file: int | None = None,
    order_by: str = "churn",
    limit: int | None = None,
) -> list[dict]:
    """Aggregate every object in H[F]/H[D], optionally below ``parent``."""
    key = f"agg:{repo['id']}:{repo['analyzed_at']}:{hf.signature}:{parent}:{is_file}:{order_by}:{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    order_map = {
        "churn": "churn DESC",
        "added": "adds DESC",
        "removed": "dels DESC",
        "growth": "growth DESC",
        "modifications": "mods DESC",
        "path": "path ASC",
    }
    order_sql = order_map.get(order_by, "churn DESC")

    if hf.is_all:
        sql = "SELECT path, is_file, adds, dels, churn, modifications AS mods, touches FROM totals WHERE repo_id = ?"
        params: list[Any] = [repo["id"]]
        if parent is not None:
            rng = prefix_range(parent)
            if rng is not None:
                sql += " AND path >= ? AND path < ?"
                params += [rng[0], rng[1]]
            else:
                sql += " AND path <> ''"
        if is_file is not None:
            sql += " AND is_file = ?"
            params.append(is_file)
        sql += f" ORDER BY {order_sql}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
    else:
        where, wparams = hf.changes_where("c")
        cond, extra = hf.author_cond()
        sql = f"""SELECT c.path AS path, c.is_file AS is_file,
                         SUM(c.adds) AS adds, SUM(c.dels) AS dels,
                         SUM(c.adds + c.dels) AS churn,
                         SUM(CASE WHEN c.adds + c.dels > 0 THEN 1 ELSE 0 END) AS mods,
                         COUNT(*) AS touches
                    FROM changes c{hf.author_join()}
                   WHERE {where}{cond}"""
        params = [*wparams, *extra]
        if parent is not None:
            pcond, pparams = _prefix_cond(parent)
            sql += f" AND {pcond}"
            params += pparams
        if is_file is not None:
            sql += " AND c.is_file = ?"
            params.append(is_file)
        sql += f" GROUP BY c.path, c.is_file ORDER BY {order_sql}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    out = [
        {
            "path": r["path"],
            "is_file": bool(r["is_file"]),
            "added": r["adds"] or 0,
            "removed": r["dels"] or 0,
            "growth": (r["adds"] or 0) - (r["dels"] or 0),
            "churn": r["churn"] if "churn" in r.keys() else (r["adds"] or 0) + (r["dels"] or 0),
            "modifications": r["mods"] or 0,
            "touches": r["touches"] or 0,
        }
        for r in rows
    ]
    _cache_set(key, out)
    return out


def children_of(
    conn: sqlite3.Connection,
    repo: sqlite3.Row,
    hf: HFilter,
    parent: str,
    *,
    q: str | None = None,
    sort: str = "churn",
    order: str = "desc",
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """Immediate children of a directory, with their metrics over H."""
    # Child objects come from the objects table (a prefix range scan).
    sql = "SELECT path, is_file, binary FROM objects WHERE repo_id = ?"
    params: list[Any] = [repo["id"]]
    rng = prefix_range(parent)
    if rng is not None:
        sql += " AND path >= ? AND path < ?"
        params += [rng[0], rng[1]]
    else:
        sql += " AND path <> ''"
    children: list[dict] = []
    prefix = parent + "/" if parent else ""
    for r in conn.execute(sql, params):
        rest = r["path"][len(prefix):]
        if "/" in rest:
            continue  # not an immediate child
        children.append(
            {"path": r["path"], "name": rest, "is_file": bool(r["is_file"]), "binary": bool(r["binary"])}
        )

    if q:
        ql = q.lower()
        children = [c for c in children if ql in c["name"].lower()]

    metrics = {
        m["path"]: m
        for m in aggregated_objects(
            conn, repo, hf, parent=parent, is_file=None, order_by="churn", limit=None
        )
        if "/" not in m["path"][len(prefix):]
    }
    for c in children:
        m = metrics.get(c["path"])
        c.update(
            {
                "added": m["added"] if m else 0,
                "removed": m["removed"] if m else 0,
                "growth": m["growth"] if m else 0,
                "churn": m["churn"] if m else 0,
                "modifications": m["modifications"] if m else 0,
                "touches": m["touches"] if m else 0,
            }
        )

    reverse = order == "desc"
    key = {
        "name": lambda c: (not c["is_file"], c["name"].lower()),
        "path": lambda c: c["path"],
        "churn": lambda c: c["churn"],
        "added": lambda c: c["added"],
        "removed": lambda c: c["removed"],
        "growth": lambda c: c["growth"],
        "modifications": lambda c: c["modifications"],
    }.get(sort, lambda c: c["churn"])
    children.sort(key=key, reverse=reverse)

    total = len(children)
    return {"parent": parent, "total": total, "children": children[offset : offset + limit]}


def hotspots(
    conn: sqlite3.Connection,
    repo: sqlite3.Row,
    hf: HFilter,
    *,
    is_file: bool = True,
    order_by: str = "churn",
    limit: int = 25,
    parent: str | None = None,
) -> list[dict]:
    """Top objects by ``order_by``; optionally restricted to a subtree."""
    return aggregated_objects(
        conn,
        repo,
        hf,
        parent=parent,
        is_file=1 if is_file else 0,
        order_by=order_by,
        limit=limit,
    )


# --------------------------------------------------------------------------
# Commit listing
# --------------------------------------------------------------------------

def list_commits(
    conn: sqlite3.Connection,
    repo: sqlite3.Row,
    *,
    page: int = 1,
    per_page: int = 50,
    q: str | None = None,
    hf: HFilter | None = None,
    sort: str = "date_desc",
) -> dict:
    per_page = max(1, min(per_page, 500))
    where = ["m.repo_id = ?"]
    params: list[Any] = [repo["id"]]
    if hf is not None:
        w, p = hf.commits_where("m")
        where.append(w)
        params += p
    if q:
        like = f"%{q}%"
        where.append("(m.hash LIKE ? OR m.subject LIKE ? OR a.name LIKE ? OR a.email LIKE ?)")
        params += [f"{q}%", like, like, like]
    sql_where = " AND ".join(where)
    total = conn.execute(
        f"""SELECT COUNT(*) AS n FROM commits m JOIN authors a ON a.id = m.author_id
            WHERE {sql_where}""",
        params,
    ).fetchone()["n"]

    order_sql = "m.ct DESC, m.id DESC" if sort == "date_desc" else "m.ct ASC, m.id ASC"
    rows = conn.execute(
        f"""SELECT m.hash, m.ct, m.subject,
                   COALESCE(ca.name, a.name) AS author_name,
                   COALESCE(ca.email, a.email) AS author_email,
                   COALESCE(c.adds, 0) AS adds, COALESCE(c.dels, 0) AS dels
              FROM commits m
              JOIN authors a ON a.id = m.author_id
              LEFT JOIN authors ca ON ca.id = a.canonical_id
              LEFT JOIN changes c ON c.repo_id = m.repo_id AND c.commit_id = m.id
                                  AND c.path = '' AND c.is_file = 0
             WHERE {sql_where}
             ORDER BY {order_sql}
             LIMIT ? OFFSET ?""",
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    commits = [
        {
            "hash": r["hash"],
            "ct": r["ct"],
            "ct_iso": iso(r["ct"]),
            "subject": r["subject"],
            "author_name": r["author_name"],
            "author_email": r["author_email"],
            "added": r["adds"],
            "removed": r["dels"],
            "churn": r["adds"] + r["dels"],
        }
        for r in rows
    ]
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "commits": commits,
    }


# --------------------------------------------------------------------------
# Repo summary
# --------------------------------------------------------------------------

def repo_summary(conn: sqlite3.Connection, repo: sqlite3.Row) -> dict:
    cache_key = f"summary:{repo['id']}:{repo['analyzed_at']}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    root = conn.execute(
        "SELECT * FROM totals WHERE repo_id = ? AND path = '' AND is_file = 0", (repo["id"],)
    ).fetchone()
    authors = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(canonical_id, id)) AS n FROM authors WHERE repo_id = ?",
        (repo["id"],),
    ).fetchone()["n"]
    files = conn.execute(
        "SELECT COUNT(*) AS n FROM objects WHERE repo_id = ? AND is_file = 1", (repo["id"],)
    ).fetchone()["n"]
    dirs = conn.execute(
        "SELECT COUNT(*) AS n FROM objects WHERE repo_id = ? AND is_file = 0", (repo["id"],)
    ).fetchone()["n"]
    binaries = conn.execute(
        "SELECT COUNT(*) AS n FROM objects WHERE repo_id = ? AND is_file = 1 AND binary = 1",
        (repo["id"],),
    ).fetchone()["n"]

    total_commits = repo["commit_count"] or 0
    root_adds = root["adds"] if root else 0
    root_dels = root["dels"] if root else 0
    result = {
        "commits": total_commits,
        "authors": authors,
        "files": files,
        "directories": dirs,
        "binaries": binaries,
        "added": root_adds,
        "removed": root_dels,
        "growth": root_adds - root_dels,
        "churn": root_adds + root_dels,
        "modifications": root["modifications"] if root else 0,
        "churn_rate": ((root_adds + root_dels) / total_commits) if total_commits else 0.0,
        "first_ct": root["first_ct"] if root else None,
        "last_ct": root["last_ct"] if root else None,
        "first_iso": iso(root["first_ct"]) if root else None,
        "last_iso": iso(root["last_ct"]) if root else None,
    }
    _cache_set(cache_key, result)
    return result


def tree_data(
    conn: sqlite3.Connection,
    repo: sqlite3.Row,
    hf: HFilter,
    *,
    top_files: int = 400,
) -> dict:
    """Directory aggregates (full hierarchy) plus the top files, for treemap/sunburst."""
    key = f"tree:{repo['id']}:{repo['analyzed_at']}:{hf.signature}:{top_files}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    dirs = aggregated_objects(conn, repo, hf, parent=None, is_file=0, order_by="churn")
    files = aggregated_objects(conn, repo, hf, parent=None, is_file=1, order_by="churn", limit=top_files)
    out = {"dirs": dirs, "files": files}
    _cache_set(key, out)
    return out
