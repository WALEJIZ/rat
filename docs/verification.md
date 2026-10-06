# Verification

The metrics engine is validated in three independent layers. Everything below
is part of this repository and can be re-run.

## 1. Synthetic edge-case suite — 46 assertions

`backend/tests/edge_cases.py` builds small, fully deterministic repositories
(fixed timestamps, no network) and asserts exact `(adds, dels, churn,
modifications, touches)` tuples per file, per directory and for the root:

- pure renames (50 % similarity floor) and rename + edit (credited to the new path)
- sub-50 % rewrites resolved as delete + add
- binary files counted as touched but never measured
- files declared binary by a committed `.gitattributes` (no NUL bytes: content
  sniffing alone would call them text — git semantics must win)
- deletions recorded as removals
- paths containing tabs, spaces and leading dashes
- merge commits excluded from `H`; root `modifications` counted per commit, not
  per child
- time-window filters, manual commit lists (incl. abbreviated hashes)
- `.mailmap` resolution and merge/unmerge round-trips
- two concurrent analyses writing to one database without id collisions

```bash
cd backend
python3 tests/edge_cases.py        # 46/46 checks pass
```

## 2. Differential testing against an independent oracle

`backend/tests/reference.py` is a deliberately independent reimplementation:
it recomputes every metric from per-commit
`git diff-tree -r --no-commit-id --root -M50% --numstat -z` output with its own
aggregation code — it shares no code with the app's streamed analyzer.

`backend/tests/verify.py` drives the **live API** against that oracle over

- three commit sets: all history, a time window (`H(i, j)`), a manual hash list;
- many objects per set: root, top-level dirs, nested dirs, hotspot files;
- per-author `n`, `λ`, `ω` and commit metadata.

```bash
cd backend
python3 tests/verify.py --repo cJSON --gitdir ../data/clones/cjson
python3 tests/verify.py --repo redis --gitdir ../data/clones/redis
python3 tests/verify.py --repo git   --gitdir ../data/clones/git
```

| Repository | Commits | Checks | Result |
| --- | ---: | ---: | --- |
| [cJSON](https://github.com/DaveGamble/cJSON) | 955 | 218 | ✅ all passed |
| [Redis](https://github.com/redis/redis) | 11,874 | 233 | ✅ all passed |
| [git.git](https://github.com/git/git) | 61,101 | 228 | ✅ all passed |

`--gitdir` is any fresh clone of the same repository (the oracle resolves
`.gitattributes` exactly like the app's users would from inside a clone).

## 3. Case study: the `.gitattributes` divergence

The first git.git run disagreed on exactly 20 checks — every one on `t` or the
root, all off by +11 / −11. Investigation found exactly two divergent commits
(`05026637f3…`, `6a56993b2e…`): the 11 files `t/t0110/url-*` were being
measured by the oracle but skipped by the app.

Root cause: git.git commits `t/.gitattributes` containing

```
/t0110/url-* binary
```

Those fixtures contain no NUL bytes, so content sniffing alone calls them text;
only the committed attributes declare them binary. Two properties of git
2.43 decided the outcome:

- in a **bare** repository, tree diffs resolve in-tree attributes themselves —
  the app's normal analysis path was already correct;
- in a work tree, attributes are only consulted when the git process runs with
  its cwd inside the work tree. The oracle's clone had become gutted (all files
  staged-deleted), so `.gitattributes` was invisible and the fixtures were
  mis-classified as text.

Fixes: the app now runs work-tree gitdirs with `cwd` at the work-tree root
(`worktree_cwd()` in `backend/rat/gitio.py`, covering ZIP uploads too), the
synthetic `gitattributes` scenario above pins the semantics, and the oracle
clones are restored. Result: git.git **228/228**, matching a real user's clone
byte for byte.

## Performance (same run)

git.git, 61,101 non-merge commits, single streaming pass, SQLite with WAL:
analysis completes in **≈31 s** (7,659 objects — the brief's 100 k-commit-class
requirement). API latency: 1–5 ms warm; ≤ 340 ms cold for a full-repository
timeline + author breakdown.
