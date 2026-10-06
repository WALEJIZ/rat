"""Author identity management: .mailmap based merging and manual merges.

Each distinct raw ``(name, email)`` pair is an author row.  ``canonical_id``
points at the representative author of a merge group (``NULL`` = independent).
Merges are single-level by construction: everything always points at a root.
"""

from __future__ import annotations

import sqlite3


def _rows(conn: sqlite3.Connection, repo_id: int) -> dict[int, sqlite3.Row]:
    return {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, name, email, name_mm, email_mm, canonical_id FROM authors WHERE repo_id = ?",
            (repo_id,),
        )
    }


def _root(rows: dict[int, sqlite3.Row], aid: int) -> int:
    seen = set()
    while aid in rows and rows[aid]["canonical_id"] and aid not in seen:
        seen.add(aid)
        aid = rows[aid]["canonical_id"]
    return aid


def snapshot_merges(conn: sqlite3.Connection, repo_id: int) -> dict[tuple[str, str], tuple[str, str]]:
    """Capture merges as member-identity -> canonical-identity, for re-analysis."""
    rows = _rows(conn, repo_id)
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for aid, r in rows.items():
        if r["canonical_id"]:
            root = _root(rows, aid)
            if root in rows:
                out[(r["name"], r["email"])] = (rows[root]["name"], rows[root]["email"])
    return out


def restore_merges(
    conn: sqlite3.Connection, repo_id: int, mapping: dict[tuple[str, str], tuple[str, str]]
) -> int:
    """Re-apply a :func:`snapshot_merges` result after a re-analysis."""
    if not mapping:
        return 0
    rows = _rows(conn, repo_id)
    by_identity = {(r["name"], r["email"]): aid for aid, r in rows.items()}
    applied = 0
    for member, root in mapping.items():
        mid = by_identity.get(member)
        rid = by_identity.get(root)
        if mid and rid and mid != rid:
            conn.execute("UPDATE authors SET canonical_id = ? WHERE id = ?", (rid, mid))
            applied += 1
    conn.commit()
    return applied


def merge_authors(conn: sqlite3.Connection, repo_id: int, member_ids: list[int], into_id: int) -> dict:
    rows = _rows(conn, repo_id)
    if into_id not in rows:
        raise ValueError("The chosen canonical author does not exist in this repository.")
    root = _root(rows, into_id)
    merged = 0
    for mid in member_ids:
        if mid not in rows or mid == root:
            continue
        # keep the single-level invariant: children of mid now point at root
        conn.execute(
            "UPDATE authors SET canonical_id = ? WHERE repo_id = ? AND canonical_id = ?",
            (root, repo_id, mid),
        )
        conn.execute("UPDATE authors SET canonical_id = ? WHERE id = ?", (root, mid))
        merged += 1
    conn.commit()
    return {"merged": merged, "canonical_id": root}


def unmerge_authors(conn: sqlite3.Connection, repo_id: int, author_ids: list[int]) -> dict:
    """Dissolve the merge groups the selected authors belong to (or lead)."""
    changed = 0
    for aid in author_ids:
        cur = conn.execute(
            "UPDATE authors SET canonical_id = NULL WHERE repo_id = ? AND id = ?",
            (repo_id, aid),
        )
        changed += cur.rowcount
        cur = conn.execute(
            "UPDATE authors SET canonical_id = NULL WHERE repo_id = ? AND canonical_id = ?",
            (repo_id, aid),
        )
        changed += cur.rowcount
    conn.commit()
    return {"unmerged": changed}


def apply_mailmap(conn: sqlite3.Connection, repo_id: int) -> dict:
    """Merge identities that share a .mailmap-resolved (name, email).

    ``git log`` applies the repository's .mailmap when producing ``%aN``/``%aE``,
    so identities that .mailmap maps to the same person share ``(name_mm,
    email_mm)``.  The representative is the identity equal to the resolved one
    when present, otherwise the most prolific one.
    """
    rows = _rows(conn, repo_id)
    if not rows:
        return {"groups": 0, "merged": 0}

    commits_per_author: dict[int, int] = {}
    for r in conn.execute(
        "SELECT author_id, COUNT(*) AS n FROM commits WHERE repo_id = ? GROUP BY author_id",
        (repo_id,),
    ):
        commits_per_author[r["author_id"]] = r["n"]

    groups: dict[tuple[str, str], list[int]] = {}
    for aid, r in rows.items():
        key = (r["name_mm"] or r["name"], r["email_mm"] or r["email"])
        groups.setdefault(key, []).append(aid)

    merged_total = 0
    groups_merged = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        canonical = None
        for aid in members:
            r = rows[aid]
            if (r["name"], r["email"]) == key:
                canonical = aid
                break
        if canonical is None:
            canonical = max(members, key=lambda aid: (commits_per_author.get(aid, 0), -aid))
        root = _root(rows, canonical)
        changed = False
        for aid in members:
            if aid == root:
                continue
            if rows[aid]["canonical_id"] == root:
                continue
            conn.execute(
                "UPDATE authors SET canonical_id = ? WHERE repo_id = ? AND canonical_id = ?",
                (root, repo_id, aid),
            )
            conn.execute("UPDATE authors SET canonical_id = ? WHERE id = ?", (root, aid))
            merged_total += 1
            changed = True
        if changed:
            groups_merged += 1
    conn.commit()
    return {"groups": groups_merged, "merged": merged_total}
