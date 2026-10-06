# Repo Analysis Tool (RAT)

A multi-repository web dashboard that measures the git metrics from the
COMS3011A test brief — **file (2.1)**, **directory (2.2)**, **repository (2.3)**,
**commit set (2.4)** and **author (2.5)** metrics — over any number of ingested
repositories.

Ingest a repository by **deep cloning a remote URL**, **uploading a zip** that
contains its `.git`, or **pointing at a local path**. The server reads the
history once into a compact columnar index, after which every metric, filter
change and chart is answered instantly from that index.

The backend is Python (Flask + NumPy); the frontend is dependency-free
HTML/CSS/vanilla JS served by the same process. There is no build step, no
database and no external service.

---

## 1. Quick start

**Requirements**

- Python 3.10 or newer (developed and tested on 3.12)
- `git` available on your `PATH` (`git --version` should work — any recent version)

**Run it** (two commands, one of them is the server):

```bash
pip install -r requirements.txt     # Flask + NumPy, nothing else
python3 backend/app.py              # start the server
```

Then open **http://127.0.0.1:8000** in your browser.

Server options:

```bash
python3 backend/app.py --port 9000        # listen on a different port
python3 backend/app.py --host 0.0.0.0     # reachable from other machines
python3 backend/app.py --data /tmp/rat    # use a different data directory
python3 backend/app.py --debug            # Flask debug mode
```

All runtime state (ingested repos, indexes, registry) lives in `./data`, which
is git-ignored. Delete it — or a single `data/repos/<id>` folder — to reset.

---

## 2. Using the dashboard

### Adding a repository

Go to the repository list (`#/` on first load) and click **Add repository**.
The form has three ingestion modes:

| Mode | What it does |
| --- | --- |
| **Remote URL** | Full-history bare clone of e.g. `https://github.com/owner/project.git`; the clone runs in the background with live progress. |
| **Zip upload** | Upload a `.zip` whose contents include the repository (a `.git` directory or a bare repo). Upload limit 8 GB. |
| **Local path** | An existing repository on this machine, e.g. `/home/you/projects/project` (or `file:///…`). It is symlinked, never modified. |

Optionally set a display name and the **analyse ref** (branch/tag/commit that
defines H̄ — defaults to `HEAD`). Ingestion and analysis run as a background
job shown live in the UI; when the repository badge turns **ready** you can
open it.

### Views

- **Overview** — repository metrics (2.3) for the current commit set, a
  timeline of l+ / l− / churn / cumulative δ, a calendar heatmap, a treemap of
  the directory tree (2.2), top authors and recent commits.
- **Explorer** — browse the directory tree (2.2) and open any file/directory
  panel (2.1) with its metrics l+, l−, δ, λ, n, η, ρ, ownership per author,
  rename history and first/last modification dates.
- **Commits** — the full history of the analysed ref, pageable and searchable.
  Tick **☑ select commits**, choose commits (or "select all on page") and
  click **Use as commit set** to make a manual commit set H (2.4) that drives
  every other view.
- **Authors** — per-author metrics (2.5), churn bars and per-file ownership.

### Filters

The filter bar on every page controls the commit set:

- **All history** → H̄, every non-merge commit reachable from the reference.
- **Time window** → H_t / H_i,j, committer date ≥ *t* or in [*i*, *j*).
- **Manual selection** → H, the hand-picked commits from the Commits view.
- **ref** — switch the reference (any branch or tag; HEAD by default).
- **All authors** — restrict to selected authors.
- **Reset** — back to H̄ over HEAD.

### Search and export

The global search box in the top bar (or press `/`) finds files, directories
and commits; clicking a result deep-links into the right view. Every table has
a **CSV export** button.

### Author identity merging

`git`'s `.mailmap` is applied automatically during analysis. On top of that,
the Authors view lets you group identities manually (e.g. two emails by the
same person); the grouping is stored per repository and applied everywhere.

---

## 3. Metric reference (brief §2 mapping)

| Spec | Object | Metrics shown |
| --- | --- | --- |
| 2.1 File | file (Explorer panel) | l+ added, l− removed, δ = l+ − l− growth, λ = l+ + l− churn, n modifications, η = n/|H| mod frequency, ρ = λ/|H| churn rate, first/last modified, present at ref, size (binaries unmeasured) |
| 2.2 Directory | directory (Explorer + treemap) | the same aggregates summed over the subtree, plus files, subdirs, dirs touched |
| 2.3 Repository | whole repo (Overview) | |H|, l+, l−, δ, λ, files touched, dirs touched, authors, first/last commit date, merge commits excluded |
| 2.4 Commit set | filter bar + Commits view | H̄ (all non-merge commits reachable from ref), H_t (date ≥ t), H_i,j (window), manual list H; size, time span, unparsed commits |
| 2.5 Author | Authors view + ownership ω | commits, l+, l−, δ, λ, files/dirs touched, first/last, ω = share of λ per file |

Conventions used by the engine:

- Renames are detected at **50% similarity** and attributed to the new path.
- **Binary and submodule changes are not measured** (they are listed with zero
  line counts and flagged in the UI).
- Deleted files count their full removal as l− in the deleting commit.
- **Merge commits carry no diff** and are excluded from H̄/λ etc.; their
  parents are still traversed for reachability.
- Commits that exist in the full graph but were never diffed (only reachable
  from a ref other than the analysed one) are reported as **unparsed** and
  excluded from metrics — the UI shows the count instead of silently wrong
  numbers, and re-analysing from that ref measures them.

---

## 4. Verifying the numbers (included tools)

`data/` is git-ignored, so the test fixtures are not shipped. Create one first:

```bash
git clone --bare https://github.com/DaveGamble/cJSON.git data/repos/cjson.git
```

### End-to-end API smoke test

```bash
python3 tools/smoke_api.py                         # uses data/repos/cjson.git
python3 tools/smoke_api.py /path/to/other/repo.git # any other fixture
```

Runs the whole pipeline — local ingestion, background analysis job, every API
endpoint, filtering, author merging, paging, manual commit lists and CSV
export — against a scratch data directory (`data/tests/api`). Prints a check
report and exits non-zero on any failure. Last run: **57 checks, 0 failures**.

### Independent metric cross-check

`tools/verify_metrics.py` recomputes the same metrics with a deliberately
naive implementation (each commit diffed with its own `git diff-tree --root
-r -M50% --numstat -z`, directory metrics accumulated by literal recursion,
commit sets resolved via `git rev-list`) and compares them against the engine's
index. It needs an existing index, i.e. a repository you have already ingested
(the smoke test above creates one):

```bash
python3 tools/verify_metrics.py data/tests/api/repos/cjson/repo.git \
                                --index data/tests/api/repos/cjson/index
```

For any repository ingested through the dashboard:

```bash
python3 tools/verify_metrics.py data/repos/<repo-id>/repo.git \
                                --index data/repos/<repo-id>/index
```

Useful flags: `--commits N` (limit the number of commits diffed one-by-one),
`--samples N`, `--json`. Exits non-zero on any mismatch. Last run: **138
checks, 0 failures**.

---

## 5. REST API

The dashboard is a thin client of the REST API under `/api`; everything the UI
does is scriptable. Shared filter parameters on metric endpoints:

`mode` (`all` | `range` | `list`), `ref`, `from` / `to` (epoch seconds,
`from` inclusive, `to` exclusive), `commits` (list mode), `authors` (ids).

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | liveness + version |
| `GET /api/repos` · `POST /api/repos` · `POST /api/repos/zip` | list / add (URL or local path) / add (zip upload) |
| `GET` · `DELETE /api/repos/<id>` · `POST /api/repos/<id>/reanalyze` | inspect / remove / re-ingest |
| `GET /api/repos/<id>/refs` · `/overview` · `/browse` · `/tree` | references, repository metrics (2.3), object panel (2.1/2.2), treemap |
| `GET /api/repos/<id>/commits` · `/commit/<rev>` | paged history, single commit detail |
| `GET /api/repos/<id>/authors` · `/identities` · `POST /api/repos/<id>/merges` | author metrics (2.5), identity list, manual identity merging |
| `GET /api/repos/<id>/search?q=…` | files, dirs and commits |
| `GET /api/repos/<id>/export.csv?kind=files\|dirs\|authors\|commits` | CSV export |

Example:

```bash
curl "http://127.0.0.1:8000/api/repos/<id>/overview?mode=range&from=1230768000&to=1420070400"
```

---

## 6. Project layout

```
backend/
  app.py                entry point: serves frontend/ and mounts the API
  rat/
    gitcmd.py           safe git subprocess wrappers (streaming reader)
    logparse.py         streaming parser for `git log --numstat -z` output
    analysis.py         ingestion -> columnar index builder
    repoindex.py        mmap'd columnar index + commit-set resolution
    metrics.py          vectorised metric computations (2.1-2.5)
    registry.py         multi-repo registry, background jobs, index cache
    ingest.py           clone / zip / local-path ingestion
    api.py              Flask REST API (blueprint)
frontend/
  index.html            single page app shell
  css/app.css           design system
  js/                   zero-dependency ES modules (hash router + views,
                        hand-rolled SVG charts)
tools/
  smoke_api.py          end-to-end API check
  verify_metrics.py     metric cross-check against raw git
data/                   runtime state, git-ignored (created on first run)
```

## 7. Implementation notes

- **One streaming pass** `git log <ref> --numstat -z -M50%` collects every
  file-change row; a second `git log --all` pass records the full commit graph
  for reachability, and `%aN`/`%aE` apply `.mailmap`.
- The index stores parallel NumPy arrays (memory-mapped at query time): file
  ids are lexicographic, so every directory is a contiguous range and subtree
  aggregates are single slice reductions.
- A per-repository LRU caches loaded indexes, which is what makes switching
  repositories instant.

## 8. Performance

Measured on this machine (UI-initiated clone + analysis):

| Repository | Commits | Files | Analysis time | Index size |
| --- | --- | --- | --- | --- |
| cJSON | 1,211 | 377 | 0.3 s | 278 KB |
| git/git | 85,930 | 7,482 | 31.9 s | 16.7 MB |

Analysis runs once per repository in a background job; afterwards all
dashboard interactions (filter changes, view switches, drill-downs) are served
from the memory-mapped index.

## 9. Troubleshooting

- **Port already in use** — `python3 backend/app.py --port 9000`.
- **Start from a clean slate** — stop the server and delete `data/`.
- **Large clone looks stuck** — progress is shown in the UI; big repositories
  (e.g. git/git, ~86k commits) take roughly half a minute to analyse.
- **A commit shows "not part of the analysed history"** — it is only reachable
  from a ref other than the analysed one; re-analyse the repository from that
  ref to measure it.
