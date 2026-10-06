"""FastAPI application: JSON API + SPA hosting for the Repo Analysis Tool."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import __version__, config
from .authors import apply_mailmap, merge_authors, unmerge_authors
from .db import connect, init_db
from .ingest import (
    DuplicateRepoError,
    IngestError,
    create_repo_from_url,
    create_repo_from_zip,
    delete_repo,
    mark_interrupted_repos,
    reanalyze_repo,
)
from .metrics import (
    FilterError,
    HFilter,
    children_of,
    hotspots,
    iso,
    list_authors,
    list_commits,
    object_metrics,
    repo_summary,
    tree_data,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rat.main")

app = FastAPI(title="Repo Analysis Tool", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    mark_interrupted_repos()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _repo_or_404(conn, repo_id: int):
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Repository not found.")
    return row


def _ready_or_409(repo) -> None:
    if repo["status"] != "ready":
        raise HTTPException(
            409,
            {
                "detail": f"Repository is not ready yet ({repo['status']}).",
                "status": repo["status"],
                "phase": repo["phase"],
                "progress": repo["progress"],
                "message": repo["message"],
                "error": repo["error"],
            },
        )


def _repo_json(repo, *, with_summary: bool = False, conn=None) -> dict:
    out = {
        "id": repo["id"],
        "name": repo["name"],
        "source_type": repo["source_type"],
        "source": repo["source"],
        "status": repo["status"],
        "phase": repo["phase"],
        "progress": repo["progress"],
        "message": repo["message"],
        "error": repo["error"],
        "commit_count": repo["commit_count"],
        "head_hash": repo["head_hash"],
        "analyzed_at": repo["analyzed_at"],
        "created_at": repo["created_at"],
    }
    if with_summary and conn is not None and repo["status"] == "ready":
        try:
            out["summary"] = repo_summary(conn, repo)
        except Exception:  # pragma: no cover - summary must never break listing
            log.exception("summary failed for repo %s", repo["id"])
            out["summary"] = None
    return out


class FilterBody(BaseModel):
    object: str = ""
    object_type: str = "dir"  # 'dir' | 'file'
    mode: str = "range"  # 'range' | 'list'
    from_ct: int | None = None
    to_ct: int | None = None
    commits: list[str] | None = None
    authors: list[int] | None = None
    with_timeline: bool = True
    with_breakdown: bool = True
    bucket_seconds: int | None = None


def _build_filter(conn, repo, body: FilterBody) -> HFilter:
    try:
        return HFilter(
            conn,
            repo["id"],
            mode=body.mode,
            from_ct=body.from_ct,
            to_ct=body.to_ct,
            commit_hashes=body.commits,
            author_ids=body.authors,
        )
    except FilterError as exc:
        raise HTTPException(400, str(exc)) from exc


def _is_file(body: FilterBody) -> int:
    if body.object_type not in ("dir", "file"):
        raise HTTPException(400, "object_type must be 'dir' or 'file'.")
    return 1 if body.object_type == "file" else 0


# --------------------------------------------------------------------------
# Repositories
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/repos")
def api_list_repos() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM repos ORDER BY CASE status WHEN 'ready' THEN 0 ELSE 1 END, id DESC"
        ).fetchall()
        return [_repo_json(r, with_summary=True, conn=conn) for r in rows]
    finally:
        conn.close()


@app.post("/api/repos/url", status_code=201)
def api_add_repo_url(payload: dict) -> dict:
    url = (payload or {}).get("url", "")
    try:
        repo_id = create_repo_from_url(url)
    except DuplicateRepoError as exc:
        raise HTTPException(
            409, {"detail": str(exc), "repo_id": exc.repo_id, "existing": True}
        ) from exc
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": repo_id}


@app.post("/api/repos/zip", status_code=201)
async def api_add_repo_zip(file: UploadFile = File(...)) -> dict:
    config.ensure_dirs()
    name = Path(file.filename or "upload.zip").name
    if not name.lower().endswith(".zip"):
        raise HTTPException(400, "Only .zip uploads are supported.")
    dest = config.UPLOADS_DIR / f"upload-{Path(name).stem}.zip"
    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Upload exceeds the size limit.")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not store the upload: {exc}") from exc
    display = Path(name).stem or "upload"
    try:
        repo_id = create_repo_from_zip(dest, display)
    except IngestError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": repo_id}


@app.get("/api/repos/{repo_id}")
def api_get_repo(repo_id: int) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        return _repo_json(repo, with_summary=repo["status"] == "ready", conn=conn)
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/reanalyze", status_code=202)
def api_reanalyze(repo_id: int) -> dict:
    conn = connect()
    try:
        _repo_or_404(conn, repo_id)
    finally:
        conn.close()
    try:
        reanalyze_repo(repo_id)
    except IngestError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "started"}


@app.delete("/api/repos/{repo_id}", status_code=204)
def api_delete_repo(repo_id: int) -> Response:
    conn = connect()
    try:
        _repo_or_404(conn, repo_id)
    finally:
        conn.close()
    delete_repo(repo_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Authors
# --------------------------------------------------------------------------

@app.get("/api/repos/{repo_id}/authors")
def api_authors(repo_id: int) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        return {"authors": list_authors(conn, repo_id)}
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/authors/merge")
def api_merge_authors(repo_id: int, payload: dict) -> dict:
    members = [int(x) for x in (payload or {}).get("members", [])]
    into = (payload or {}).get("into")
    if into is None:
        raise HTTPException(400, "Provide the canonical author id in 'into'.")
    conn = connect()
    try:
        _repo_or_404(conn, repo_id)
        try:
            return merge_authors(conn, repo_id, members, int(into))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/authors/unmerge")
def api_unmerge_authors(repo_id: int, payload: dict) -> dict:
    members = [int(x) for x in (payload or {}).get("members", [])]
    if not members:
        raise HTTPException(400, "Select at least one author to unmerge.")
    conn = connect()
    try:
        _repo_or_404(conn, repo_id)
        return unmerge_authors(conn, repo_id, members)
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/authors/apply-mailmap")
def api_apply_mailmap(repo_id: int) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        return apply_mailmap(conn, repo_id)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Metrics & exploration
# --------------------------------------------------------------------------

@app.get("/api/repos/{repo_id}/summary")
def api_summary(repo_id: int) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        return repo_summary(conn, repo)
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/metrics")
def api_metrics(repo_id: int, body: FilterBody) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        hf = _build_filter(conn, repo, body)
        return object_metrics(
            conn,
            repo,
            body.object,
            _is_file(body),
            hf,
            with_timeline=body.with_timeline,
            with_breakdown=body.with_breakdown,
            bucket_seconds=body.bucket_seconds,
        )
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/objects")
def api_objects(
    repo_id: int,
    body: FilterBody,
    parent: str = Query(""),
    q: str | None = None,
    sort: str = Query("churn"),
    order: str = Query("desc"),
    limit: int = Query(500, le=5000),
    offset: int = Query(0),
) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        hf = _build_filter(conn, repo, body)
        return children_of(
            conn, repo, hf, parent, q=q, sort=sort, order=order, limit=limit, offset=offset
        )
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/hotspots")
def api_hotspots(
    repo_id: int,
    body: FilterBody,
    is_file: bool = Query(True),
    order_by: str = Query("churn"),
    limit: int = Query(25, le=500),
    parent: str | None = Query(None),
) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        hf = _build_filter(conn, repo, body)
        return {
            "hotspots": hotspots(
                conn, repo, hf, is_file=is_file, order_by=order_by, limit=limit, parent=parent
            )
        }
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/tree")
def api_tree(repo_id: int, body: FilterBody, top_files: int = Query(400, le=2000)) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        hf = _build_filter(conn, repo, body)
        return tree_data(conn, repo, hf, top_files=top_files)
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/search")
def api_search(repo_id: int, q: str = Query(..., min_length=1), limit: int = Query(30, le=200)) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        like = f"%{q}%"
        rows = conn.execute(
            """SELECT path, is_file, binary, last_ct FROM objects
                WHERE repo_id = ? AND path LIKE ? ORDER BY is_file, path LIMIT ?""",
            (repo_id, like, limit),
        ).fetchall()
        return {
            "results": [
                {
                    "path": r["path"],
                    "is_file": bool(r["is_file"]),
                    "binary": bool(r["binary"]),
                    "last_iso": iso(r["last_ct"]),
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@app.get("/api/repos/{repo_id}/commits")
def api_commits(
    repo_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    q: str | None = None,
    sort: str = Query("date_desc"),
    from_ct: int | None = None,
    to_ct: int | None = None,
    authors: str | None = None,
) -> dict:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        author_ids = [int(a) for a in authors.split(",") if a.strip()] if authors else []
        hf = None
        if from_ct is not None or to_ct is not None or author_ids:
            hf = HFilter(
                conn, repo_id, mode="range", from_ct=from_ct, to_ct=to_ct, author_ids=author_ids
            )
        return list_commits(conn, repo, page=page, per_page=per_page, q=q, hf=hf, sort=sort)
    finally:
        conn.close()


@app.post("/api/repos/{repo_id}/export")
def api_export(
    repo_id: int,
    body: FilterBody,
    dataset: str = Query("metrics"),
    fmt: str = Query("csv"),
) -> Response:
    conn = connect()
    try:
        repo = _repo_or_404(conn, repo_id)
        _ready_or_409(repo)
        hf = _build_filter(conn, repo, body)
        stamp = (repo["head_hash"] or "repo")[:10]
        if dataset == "metrics":
            data = object_metrics(conn, repo, body.object, _is_file(body), hf)
            name = f"metrics-{body.object.replace('/', '_') or 'root'}-{stamp}"
            rows = [
                {"section": "metrics", "key": k, "value": v}
                for k, v in data["metrics"].items()
            ] + [
                {**{"section": "author"}, **b} for b in data["author_breakdown"]
            ]
        elif dataset == "children":
            data = children_of(conn, repo, hf, body.object, limit=100000)
            rows = data["children"]
            name = f"children-{body.object.replace('/', '_') or 'root'}-{stamp}"
        elif dataset == "hotspots":
            rows = hotspots(conn, repo, hf, limit=500)
            name = f"hotspots-{stamp}"
        else:
            raise HTTPException(400, f"Unknown dataset {dataset!r}.")
        if fmt == "json":
            return JSONResponse(
                {"repo": repo["name"], "filter": hf.describe(), "rows": rows},
                headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
            )
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return Response(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Static SPA
# --------------------------------------------------------------------------

_DIST = config.FRONTEND_DIST


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    dist = _DIST.resolve()
    if full_path and not full_path.startswith("api/"):
        candidate = (dist / full_path).resolve()
        if str(candidate).startswith(str(dist)) and candidate.is_file():
            # Vite asset names are content-hashed: cache them hard, but make
            # everything else (favicon, etc.) revalidate so a new build is
            # picked up immediately.
            if full_path.startswith("assets/"):
                headers = {"Cache-Control": "public, max-age=31536000, immutable"}
            else:
                headers = {"Cache-Control": "no-cache"}
            return FileResponse(candidate, headers=headers)
    index = dist / "index.html"
    if index.is_file():
        # The entry document must always revalidate, otherwise a browser may
        # keep pointing at a previous bundle after a rebuild.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return JSONResponse(
        {
            "detail": "Frontend build not found. Run `npm install && npm run build` in ./frontend.",
            "api": "/api/health",
        },
        status_code=503,
    )


def run() -> None:
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")
