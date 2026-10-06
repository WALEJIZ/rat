"""Quick smoke test: analyse a local clone and print headline metrics."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rat import analyze, config, db, metrics  # noqa: E402


def main() -> None:
    gitdir = Path(sys.argv[1])
    repo_id = 1
    db.init_db()
    conn = db.connect()
    conn.execute("DELETE FROM repos")
    conn.execute(
        "INSERT INTO repos (id, name, source_type, source, status) VALUES (?,?,?,?,?)",
        (repo_id, gitdir.parent.name, "url", str(gitdir), "analyzing"),
    )
    conn.commit()
    conn.close()

    t0 = __import__("time").time()
    stats = analyze.analyze_repo(repo_id, gitdir, progress=lambda p, f, m: None)
    print("stats:", stats, f"wall={__import__('time').time() - t0:.1f}s")

    conn = db.connect()
    repo = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    core = metrics.object_metrics(conn, repo, "", 0, metrics.HFilter(conn, repo_id))
    print("root metrics:", core["metrics"])
    print("authors:", len(metrics.list_authors(conn, repo_id)))
    print("summary:", metrics.repo_summary(conn, repo))
    conn.close()


if __name__ == "__main__":
    main()
