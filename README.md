# RAT — Repository Analysis Tool

A web application that ingests git repositories (remote URLs or zip uploads),
analyses their entire history once, and serves the metrics defined in the
COMS3011A test brief for every **file**, **directory**, **repository**, **commit
set** and **author** — fast enough to stay interactive on a 100k-commit repo.

![stack](https://img.shields.io/badge/stack-FastAPI%20%2B%20SQLite%20%2B%20React%2BECharts-4c8)

---

## Features

**Ingestion**

- Remote repository by URL — full (`--bare`) clone, so the whole history is analysed.
- Zip upload containing the repository **including its `.git` directory**.
- Multiple repositories side by side, each analysed independently.
- Background jobs with live progress, cancellation on delete and one-click
  re-analysis (a fresh full pass — bounded by the same streaming speed).

**Metrics — everything the brief defines**

- Per-file: added `l⁺`, removed `l⁻`, growth `δ = l⁺ − l⁻`, churn `λ = l⁺ + l⁻`,
  modifications `n`, touches.
- Per-directory: recursive sums over all descendants;
  modifications counted per commit that changed anything below the directory.
- Repository = metrics of the root directory.
- Commit-set metrics for every object: `l⁺(H,o)`, `l⁻(H,o)`, `δ(H,o)`, `λ(H,o)`,
  `n(H,o)`, modification frequency `η = n/|H|`, churn rate `ρ = λ/|H|`.
- Authors: `n(H,o,a)`, `λ(H,o,a)`, ownership `ω = λ(H,o,a)/λ(H,o)`, plus
  per-author totals over the full history.

**Commit-set filters**

- Whole history (default), a time range `H(i,j)` on committer time, or a manual
  commit list picked in the UI (search / date / sort, multi-page selection,
  paste hashes — abbreviated hashes are resolved too).
- Optional author filter and object scoping; all filters compose.

**Author merging**

- `.mailmap` application: identities that the repository's mailmap resolves to
  the same person are grouped in one click.
- Manual merge / unmerge of any author identities; survives re-analysis.

**Dashboard**

- KPIs with formula popovers, commit timeline, file-type treemap / sunburst,
  ownership donut, author bars, hottest files & directories, full object
  explorer with breadcrumbs, commits browser.
- Exports: metrics as CSV/JSON for any selection, children as CSV.
- Dark/light theme, keyboard shortcuts (`?` shows all formulas), empty and
  error states everywhere, responsive layout.

---

## Quickstart

A **prebuilt dashboard is committed** (`frontend/dist/`), so the fastest start
needs only **Python 3.11+**:

```bash
# 1. backend deps
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt

# 2. run — API + dashboard on http://127.0.0.1:8000
cd backend && python3 -m rat
```

or simply `scripts/run.sh`.

Rebuild the dashboard only if you change `frontend/src` (**Node 18+** needed):

```bash
cd frontend && npm ci && npm run build
```

Development mode: `cd frontend && npm run dev` serves Vite on
`http://127.0.0.1:5173` and proxies `/api/*` to the backend.

### Try it out

1. Open `http://127.0.0.1:8000`.
2. On the home page paste a repository URL (e.g.
   `https://github.com/DaveGamble/cJSON.git`) or drop a zip that contains the
   repository's `.git` folder, and watch the progress bar.
3. When the repository shows **ready**, open the Overview / Explore / Authors /
   Commits tabs.

---

## Metric semantics (as implemented, matching the brief)

| Symbol | Definition |
| --- | --- |
| `H` | non-merge commits reachable from `HEAD` (merge commits are excluded) |
| `H(i,j)` | commits with `i ≤ committer time < j` |
| `l⁺, l⁻` | added / removed lines of the object in one commit |
| `δ = l⁺ − l⁻` | growth |
| `λ = l⁺ + l⁻` | churn |
| `n = Σₕ 𝟙[λₕ,ₒ > 0]` | commits that changed the object (for directories: changed anything below it) |
| `η = n/\|H\|`, `ρ = λ/\|H\|` | modification frequency, churn rate |
| `ω = λ(H,o,a)/λ(H,o)` | author ownership of a file or directory |

Implementation notes:

- **Rename detection at 50 %** (`-M50%`, `diff.renameLimit` raised to 20 000 so
  detection is exact even on very large commits): a rename + edit is credited
  entirely to the **new** path; the old path simply ends without a removal.
- A **deletion** is recorded as removals on the deleted path.
- **Binary files are not measured**: their line counts are always 0 (pure binary
  commits do therefore not increment `n`); they are still listed and marked.
  The text/binary classification follows git itself, **including the repository's
  committed `.gitattributes`** — e.g. git.git declares its `t/t0110/url-*` test
  fixtures `binary`, so they stay unmeasured even though they contain no NUL
  bytes, exactly as in a normal local clone of git.git.
- The **initial commit** is included (`--root`), so a file's first content
  counts as additions.
- Author identity is the `(name, email)` pair recorded on the commit
  (`%an/%ae`); `%aN/%aE` (mailmap-resolved) names are stored alongside so
  `.mailmap` merging works for bare clones and for uploaded work trees alike.

---

## Architecture

```
frontend/ (React 18 + Vite + ECharts)   backend/ (FastAPI + SQLite)
        │  /api/*                             │
        └──────────────► uvicorn ◄────────────┘
                          │  daemon threads
                    ingestion jobs ──► git subprocesses (streamed)
                          │
                    SQLite (WAL): repos / commits / authors / objects /
                                  changes / totals / idseq
```

**Analysis is a single streaming pass per repository** and never loads a commit
into memory twice:

1. *Metadata pass* — `git log --no-merges --root -M50% --pretty=…` yields
   `(hash, author, mailmap names, committer time, subject)`.
2. *Diff pass* — `git log --no-merges --root -M50% --numstat -z --pretty=…`
   streams `-z` records; bare renames are detected by the empty path form,
   binary `-`/`-` entries are stored with zero counts.
3. Each file change of a commit is rolled up into **all its ancestor
   directories** inside the same transaction, which makes every directory
   (including the repository root) a point lookup at query time.
4. `totals` materialises full-history aggregates per object for the common
   unfiltered case; filtered queries scan the `changes` index on committer time
   or on a temp table of commit ids. Responses for equal filter signatures are
   memoised in an in-process LRU (invalidated per `analyzed_at`).
5. Commit ids come from an atomic SQLite sequence (`idseq`) so several analyses
   can run concurrently on one database without id collisions.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/repos/url` · `POST /api/repos/zip` | ingest a repository |
| `GET /api/repos` · `GET /api/repos/{id}` | list / status & progress |
| `POST /api/repos/{id}/reanalyze` · `DELETE /api/repos/{id}` | maintain |
| `POST /api/repos/{id}/metrics` | metrics for one object over one commit set (`H`) |
| `POST /api/repos/{id}/objects` · `/tree` · `POST /hotspots` | browsing |
| `GET /api/repos/{id}/commits` · `/search` · `/summary` | history |
| `GET/POST /api/repos/{id}/authors[...]` | author list, merge, unmerge, `.mailmap` |
| `POST /api/repos/{id}/export` | CSV / JSON export |

---

## Performance (measured on this machine)

| Repository | Commits in `H` | Objects | Change rows | Analysis time |
| --- | ---: | ---: | ---: | ---: |
| cJSON | 955 | 296 | 3 007 | 0.3 s |
| Redis | 11 874 | 3 086 | 61 643 | 10 s |
| git.git | 61 101 | 7 659 | 254 298 | 31 s |

API latency on git.git (the 100k-commit-class repo): 1–5 ms warm for all views;
≤ 340 ms even cache-cold for a full-repository timeline + author breakdown, and
16–34 ms cache-cold for a manual 100-commit set.

---

## Correctness & verification

The repository ships the tooling used to validate the implementation against
git ground truth. All of it is runnable:

```bash
cd backend
python3 tests/edge_cases.py     # 46 assertions on synthetic repositories
python3 tests/verify.py --repo cJSON --gitdir ../data/clones/cjson
python3 tests/verify.py --repo redis --gitdir ../data/clones/redis
python3 tests/verify.py --repo git   --gitdir ../data/clones/git
```

- **`edge_cases.py`** builds small deterministic repositories covering pure
  renames, rename + edit, sub-50 % rewrites (delete + add), binary files,
  `.gitattributes`-declared binaries, deletions, paths with tabs and spaces,
  merge exclusion, time-window and manual commit-list filters (including
  abbreviated hashes), `.mailmap` merging/unmerging and two concurrent
  analyses on one database. **Result: 46/46 checks pass.**

- **`verify.py`** compares the running API against `tests/reference.py`, a
  deliberately independent oracle: it recomputes the metrics from per-commit
  `git diff-tree -r --root -M50% --numstat -z` output with its own
  aggregation code, over three commit sets (all history, a time window, a
  manual hash list) × many objects (root, top dirs, nested dirs, hotspots
  files) plus per-author metrics and ownership `ω`.  `--gitdir` is any fresh
  clone of the same repository: the oracle then sees the same
  `.gitattributes` semantics a normal user's clone does.

  | Repository | Checks | Result |
  | --- | ---: | --- |
  | cJSON | 218 | ✅ all passed |
  | Redis | 233 | ✅ all passed |
  | git.git | 228 | ✅ all passed |

---

## Repository layout

```
backend/
  rat/            config, db schema, git helpers, ingestion jobs, analyzer,
                  metrics+filter engine, FastAPI app
  tests/          reference.py (oracle), verify.py (API vs oracle),
                  edge_cases.py (synthetic scenarios), smoke.py
frontend/
  src/            React app: App (hash routing, polling), pages (Home,
                  Overview, Explore, Authors, Commits), charts, filters, ui kit
scripts/          run.sh (one-command start), seed_demo.sh,
                  reanalyze_all.sh (maintenance helpers)
data/             runtime only (clones, uploads, rat.sqlite3) — git-ignored
```

## License

MIT — see `LICENSE`.
