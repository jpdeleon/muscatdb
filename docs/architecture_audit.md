# MuSCAT-DB Architecture Audit

**Date:** 2026-06-30

## Executive Summary

This audit reveals three interconnected architectural debts in the application layer, all fixable and the first two are **prerequisites for your Celery/Redis migration**. The data layer (atomic builds, indexed queries, safe artifact serving) is well-structured.

---

## 1. Current Architecture State

**Layered structure (data flows top→bottom):**

```
Obslog CSVs (`$MUSCAT_OBSLOG_DIR`, shared mount in multi-host deployments)
        │  daily cronjob → database.build_db()  (atomic tmp-file rebuild)
        ▼
muscat.db (SQLite, WAL)   tables: frames → summaries → targets (materialized), 
                                  jobs, db_meta, ephemeris_views, exposure_coeffs
        │  read-mostly
        ▼
FastAPI app  (src/muscat_db/web.py, 3202 lines)  ── Jinja2 server-rendered templates + JSON APIs
        │                         │
        ├── photometry.py (2050)  └── transit_fit.py (~2150)   ── launch external pipelines as background subprocesses
        │        ▼                          ▼
        │   prose2 (conda env "prose")   timer pkg            ── write product files to $HOME/ql/prose/<inst>/<date>/
        ▼
Browser: vanilla JS embedded in templates, polls /…/status every 2s
```

**Stack:** FastAPI + uvicorn, Jinja2 (rendered manually via module-level `Environment`, not FastAPI's templating), SQLite (stdlib `sqlite3`, no ORM), Typer CLI, subprocess-launched external science pipelines in separate conda env. No Redis/Celery yet (planned). No JS build step — all client code is inline in templates.

**Notable Strengths** (keep these):
- Atomic DB rebuild that preserves app-owned tables (`database.py:360-502`)
- Pre-aggregated `summaries` table making `get_dates` sub-second (`database.py:655-674`)
- Safe artifact serving with traversal/extension allowlists (`photometry.py:777-824`)
- Parameterized SQL + ADQL literal escaping (`web.py:103`)
- Env-overridable paths for portability
- Job watchdog for hung runs (`photometry.py:1706`)
- Mtime-keyed render/status caches
- Startup secret-presence check (`web.py:74-84`)

---

## 2. Findings Ranked by Severity

### 🔴 CRITICAL

#### C1 — Job lifecycle/state machine duplicated with drift

**Issue:** `photometry.py` and `transit_fit.py` independently re-implement ~300+ lines of shared job management code:
- In-memory registry (`_JOBS`/`_FIT_JOBS`) and locks
- `sync_jobs`, `job_status`, `_pending_status`/`_persisted_status`
- `cancel_*`, `_kill_after`, `RunDescriptor`, `build_run_id`, `slugify_run_name`
- `_target_dir_name`, `_run_dir_name`, `_parse_run_dir_name`
- `_MAX_FULL_JOBS=1`, `_count_running_full`

**The implementations have already diverged:**
- **Photometry** has the documented `finalizing` grace-window state machine (`photometry.py:1520-1534` `_resolve_job_state`, plus `_log_quiescent`/`_finalize_grace_s`) to keep the live log streaming after parent-exit
- **Transit-fit declares jobs terminal the instant `proc.poll()` returns** (`transit_fit.py:2034-2049`), with **none of the finalizing semantics**

This means **the exact freeze-mid-output bug that CLAUDE.md describes and photometry guards against is still latent on the transit-fit page**. Any future fix to one machine must be hand-ported to the other.

**Files affected:**
- `src/muscat_db/photometry.py` (lines ~1470-2100)
- `src/muscat_db/transit_fit.py` (lines ~1940-2110)

**Recommendation:** Extract a single `jobs.py` runner (`PipelineJob` + `JobRunner` with the finalizing semantics) before the Celery/Redis migration, or you will migrate the bug twice.

---

#### C2 — In-memory subprocess tracking incompatible with planned multi-server architecture

**Issue:** Authoritative live state lives in the web process's RAM (`_JOBS`), tracking OS PIDs via `start_new_session=True`.

**Consequences already visible:**
- Server restart turns running jobs into `"Process lost (server restart)"` errors (`photometry.py:1965-1990`)
- Concurrency enforced by counting live local processes (`_count_running_full`), which cannot coordinate across hosts
- `_MAX_FULL_JOBS=1` is a per-process global, not cluster-aware
- No queue abstraction — pending jobs are a `state='pending'` row drained opportunistically inside `sync_jobs` on each poll (`photometry.py:1992-2050`)

**None of this survives a move to the planned 48/120/120-core multi-server setup.**

**Files affected:**
- `src/muscat_db/photometry.py` (lines ~100-150, ~1470-2100)
- `src/muscat_db/transit_fit.py` (lines ~1-50, ~1940-2110)
- `src/muscat_db/web.py` (lines ~1600-1700, routing to both)

**Recommendation:** Introduce a `JobQueue`/`JobRepository` interface now (even DB-backed) **before** the Celery migration, so route handlers and both pipelines stop touching `subprocess.Popen` and `_JOBS` directly. This interface becomes the Celery seam.

---

### 🟠 HIGH

#### H1 — `web.py` is a 3,202-line god-module

**Issue:** A single file holds unrelated concerns:
- HTTP routing
- Manual HTML rendering
- Outbound NASA/TOI Exoplanet-Archive queries (`transit_fit_query_archive`, `web.py:713-1080`, ~370 lines)
- Local catalog CSV matching (duplicated 4×: `query_local_tois`, `query_local_nasa`, `_query_target_planets_nasa`, `_query_target_planets_toi`, `_query_target_planets_catalog`, `_query_target_coordinates`)
- LCO archive geometry/dataset-matching (`_annotate_lco_archive_results`, ~125 lines of angular-separation clustering)
- Inline scientific math (see H2 below)

**Impact:** Violates the project's own <800-line file rule (CLAUDE.md); hurts testability; mixes concerns.

**Files affected:**
- `src/muscat_db/web.py` (3202 lines, needs decomposition)

**Recommendation:** Split into:
- `catalog.py` — archive + local CSV lookups behind a single resolver
- `lco_archive.py` — LCO dataset discovery and clustering
- `ephemeris_math.py` — scientific computations
- Keep `web.py` as thin routing only, <800 lines

---

#### H2 — Core scientific computation embedded in a route handler

**Issue:** The O-C ephemeris linear fit lives inline in `api_ephemeris_calculate` (`web.py:2699-2892`, ~190 lines):
- Hand-rolled weighted/unweighted least-squares
- Manual variance propagation
- Epoch-centering logic
- **Directly inside the FastAPI endpoint**

**Impact:** 
- Effectively untestable without spinning up the HTTP layer
- Uncertainty math has no unit coverage
- Violates CLAUDE.md ("choose correctness over simplicity", reproducibility focus)

**Files affected:**
- `src/muscat_db/web.py` (lines 2699-2892)

**Recommendation:** Move to an importable `ephemeris_math.py` module with full test coverage. Consider leveraging numpy/astropy rather than hand-summed `Sw/Swx/Swxx`.

---

#### H3 — Pipeline outputs discovered via filename regex (brittle coupling)

**Issue:** `list_outputs` (`photometry.py:386-650`, ~260 lines) reverse-engineers prose2 filenames using regex:
- Site tokens, `_full` mode tokens
- Two generations of summary stems
- Newest-wins tie-breaking
- Band names that may contain underscores

**Impact:** 
- MuSCAT-DB is tightly coupled to prose2's exact naming conventions
- A single rename in prose2 **silently empties the photometry page with no error**
- Fragility class: cross-repo coupling with no interface

**Files affected:**
- `src/muscat_db/photometry.py` (lines 386-650)
- Depends on prose2 naming in `$HOME/ql/prose/<inst>/<date>/`

**Recommendation:** Have prose2 emit a small manifest sidecar (e.g., `<stem>_products.json`) enumerating its outputs and metadata. MuSCAT-DB reads the manifest. Since CLAUDE.md mandates "all photometry functions live in prose2," the manifest writer belongs there and the schema becomes the shared contract.

---

### 🟡 MEDIUM

#### M1 — Unbounded, partially unlocked module-level caches

**Issue:**
- `_CATALOG_CACHE` (`web.py:14`) and `_index_cache` (`web.py:161`) are plain dicts mutated from request handlers
- FastAPI runs `def` (non-async) routes in a threadpool → concurrent access without a lock
- Both grow without bound: `_CATALOG_CACHE` is keyed per target string (every distinct query adds an entry); `_index_cache` stores ~2.85 MB HTML blobs per normalized target
- `cache.py`'s `TTLCache` is locked but has no size cap / LRU eviction

**Impact:** Slow memory leaks over long-lived server operation.

**Files affected:**
- `src/muscat_db/web.py` (lines 14, 161, and all uses)
- `src/muscat_db/cache.py` (TTLCache implementation)

**Recommendation:** Add a bounded LRU cache with automatic eviction; protect unbounded dicts with a lock or move to thread-safe collections.

---

#### M2 — Blocking I/O inside `async` and threadpool route handlers

**Issue:**
- Several `async def` routes do synchronous SQLite + filesystem + outbound HTTP
- `index()` → `get_targets()` loops every target × every date calling `get_photometry_status` and `get_fit_outputs`, each doing disk `stat`/scan (`database.py:746-786`) — **N×M disk walk on the index render**
- `transit_fit_query_archive` and `_query_target_planets_*` call `urllib.request.urlopen` from request threads, occupying the threadpool on slow archive responses

**Impact:** Latency spikes on cold cache; threadpool starvation during archive queries.

**Files affected:**
- `src/muscat_db/web.py` (index route, archive query routes)
- `src/muscat_db/database.py` (get_targets, get_photometry_status, get_fit_outputs)

**Recommendation:**
- Migrate outbound HTTP to `httpx` with explicit timeouts off the request path
- Precompute per-dataset status at build time or cache more aggressively
- Use disk-stat results in queries (e.g., indexed mtime view) rather than scanning

---

#### M3 — Ad-hoc SQLite access with no repository/connection abstraction

**Issue:**
- `sqlite3.connect(...)`/`.close()` open-coded dozens of times across `web.py` and `database.py`
- Inconsistent `timeout` (some 10/30s, many none)
- Inconsistent `row_factory`
- Repeated `PRAGMA`/`executescript(SCHEMA)` on read paths
- `_DB_LOCK` (`web.py:13`) only used by a couple of ephemeris endpoints; every other writer relies on SQLite's own locking

**Impact:** Latent bugs (unclosed connections on exception paths); no centralized tuning; footgun when adding concurrency.

**Files affected:**
- `src/muscat_db/web.py` (scattered conn opens)
- `src/muscat_db/database.py` (scattered conn opens)
- `src/muscat_db/photometry.py` (scattered conn opens)
- `src/muscat_db/transit_fit.py` (scattered conn opens)

**Recommendation:** Create a single `@contextmanager get_conn()` and a thin repository interface for all database access. The project's own `patterns.md` calls for this.

---

#### M4 — No CI pipeline

**Issue:** `.github/` is absent (TODO.md: "add github CI"). Despite:
- An 80%-coverage rule (CLAUDE.md)
- A many-process pipeline with race conditions (photometry job lifecycle)
- Known test gaps (ephemeris math has none)

There is no automated test/lint gating.

**Impact:** Technical debt accumulation; silent regressions; no enforcement of the 80%-coverage rule.

**Files affected:**
- Missing: `.github/workflows/` directory and workflows

**Recommendation:** Add GitHub Actions workflow running:
- `uv run pytest` (fast suite only; slow suite already `pytest.skip`s safely off-host)
- `ruff check .` (linting)
- Could add `coverage` reporting for visibility

The fast suite + slow suite's safe-skip design makes this low-risk to add.

---

#### M5 — 2,700+ line templates with inline JS and a documented manual-sync footgun

**Issue:**
- `transit_fit.html` (2,691 lines) and `photometry.html` (1,489 lines) embed all client logic inline (43–50 `fetch`/listener sites each)
- The `collectOptions`/`restoreOptions`/default-listener triad must be kept in sync by hand
- **CLAUDE.md explicitly warns about this**, which means it has bitten before
- No shared client module despite `/static` already being mounted
- `--reload` flag doesn't watch Jinja2 templates, requiring manual restarts

**Impact:** Maintenance friction; duplication; sync hazard; higher restart friction.

**Files affected:**
- `src/muscat_db/templates/transit_fit.html` (2,691 lines)
- `src/muscat_db/templates/photometry.html` (1,489 lines)

**Recommendation:** Extract a small `static/js/jobPolling.js` and an options-registry helper. Eliminates duplication, removes sync hazard, reduces restart friction.

---

### 🔵 LOW

#### L1 — Pervasive `except Exception: pass`
Multiple locations swallow exceptions silently (catalog queries, status calc, preview loaders, `_get_run_fitted_params`). Violates "never silently swallow errors" rule. Makes pipeline debugging hard.

**Recommendation:** At minimum log at debug level with context.

#### L2 — Imports scattered inside functions and shadowing top-level names
E.g., `import datetime` inside `transit_fit_page` though already imported at module top; repeated local `import yaml`, `import csv`, `import urllib` across handlers.

**Recommendation:** Move all imports to module top; consolidate imports.

#### L3 — `build_run_id` signatures differ between pipelines
- Photometry: `build_run_id(inst, site, mode, run_name)`
- Transit-fit: `build_run_id(site, mode, run_name)`

Small divergence that a shared module (C1) would eliminate.

#### L4 — Possibly-dead code
`get_all_jobs()` exists in both pipeline modules but the Jobs page reads from the DB via `get_persisted_jobs`. Worth confirming and removing.

---

## 3. Scalability & Performance Notes

### Concurrency Model
**Status:** "One heavy job, internally parallel."
- `_MAX_FULL_JOBS=1` deliberately serializes full reductions because each prose run itself fans out via `SequenceParallel`
- Reasonable today, but cap is per-process integer and queue is in-memory/DB-poll hybrid — neither distributes across hosts
- Ties back to C2; this is why the queue abstraction is needed now

### Database Read Path
**Status:** Healthy
- `frames` indexed on `(instrument,obsdate)` and `object`
- Hot pages read ~1000× smaller `summaries` table
- `targets` is materialized at build time
- Main inefficiency: per-dataset on-disk status scan in `get_targets` (see M2)

### Real-Time Updates
**Status:** 2s polling, adequate but not optimal
- SSE/WebSocket for live logs would cut overhead and latency at higher job counts
- A `status-batch` pattern (already present for photometry, `web.py:3082`) should be the default rather than per-job polling

### Daily Full Rebuild
**Status:** Fine at current size but is O(all-history)
- `ingest_date` (`database.py:526`) already supports incremental ingest — prefer it as data grows

---

## 4. Recommended Action Plan (Priority Order)

### Priority 1: Extract shared jobs runner (fixes C1 + latent transit-fit bug)
**Scope:** Create `src/muscat_db/jobs.py` with:
- `PipelineJob` dataclass (name, site, mode, run_name, etc.)
- `JobRunner` class with the full finalizing state machine from photometry
- `_log_quiescent`, `_finalize_grace_s`, grace-window logic
- Shared `build_run_id`, `slugify_run_name`, `_target_dir_name`, `_run_dir_name`, `_parse_run_dir_name`

**Impact:** Port transit-fit to use `JobRunner` immediately, fixing the latent log-freeze bug. Unblocks C2.

---

### Priority 2: Add JobQueue/JobRepository interface (fixes C2, preps for Celery)
**Scope:** Create `src/muscat_db/queue.py` with:
- Abstract `JobQueue` interface (enqueue, peek, dequeue, mark_done, mark_failed)
- Abstract `JobRepository` interface (get_all, get_by_id, save, update_state)
- A `DatabaseJobQueue` implementation for now (uses the `jobs` table)

**Impact:** Sever route handlers and pipelines from subprocess/`_JOBS` directly. This is the Celery seam. Prepares for multi-server migration.

---

### Priority 3: Decompose `web.py` (fixes H1 + H2)
**Scope:** Split into:
- `src/muscat_db/catalog.py` — archive + local CSV lookups behind a resolver interface
- `src/muscat_db/lco_archive.py` — LCO dataset discovery and clustering
- `src/muscat_db/ephemeris_math.py` — O-C fit math (extracted from route) with unit tests
- `src/muscat_db/web.py` — keep <800 lines, thin routing only

**Impact:** Improves testability, modularity, and enforces file-size rule. Unit-testable ephemeris math.

---

### Priority 4: Product manifest contract with prose2 (fixes H3)
**Scope:**
- Ask prose2 maintainers to emit `<stem>_products.json` manifest alongside outputs
- Manifest schema: `{files: [{path, type, band, description}], metadata: {...}}`
- Replace `list_outputs` regex with manifest reading

**Impact:** Removes brittle filename-regex coupling; shared contract makes cross-repo changes safe.

---

### Priority 5: Connection abstraction + bounded cache + CI (fixes M1/M3/M4)
**Scope:**
- Create `src/muscat_db/db.py` with `@contextmanager get_conn()` and repository interface
- Replace scattered `sqlite3.connect` with `get_conn()`
- Replace `_CATALOG_CACHE` and `_index_cache` with a bounded LRU cache
- Add `.github/workflows/test.yml` running `uv run pytest` (fast) + `ruff check .`

**Impact:** Removes connection-handling bugs; frees up memory; enforces coverage rule.

---

## Key Files Referenced

- `src/muscat_db/web.py` — 3,202 lines; HTTP routing + archive queries + ephemeris math
- `src/muscat_db/photometry.py` — 2,050 lines; job management + output discovery
- `src/muscat_db/transit_fit.py` — ~2,150 lines; job management (diverged from photometry)
- `src/muscat_db/database.py` — database access and schema
- `src/muscat_db/cache.py` — TTLCache (unbounded)
- `src/muscat_db/templates/transit_fit.html` — 2,691 lines; embedded client logic
- `src/muscat_db/templates/photometry.html` — 1,489 lines; embedded client logic
- `src/muscat_db/instruments.py` — instrument definitions
- `CLAUDE.md` — project conventions (scientific correctness, reproducibility, no file >800 lines, 80% coverage)
- `TODO.md` — includes "add github CI"

---

## Summary

The data layer (atomic builds, indexed queries, safe artifact serving) is well-structured. The application layer has three interconnected architectural debts:

1. **Job lifecycle duplication with drift** (C1) — already causing latent bugs (transit-fit log freeze)
2. **In-memory state incompatible with multi-server setup** (C2) — breaks the Celery migration plan
3. **Web module god-object with embedded science code** (H1/H2) — poor testability and modularity

**The first two are prerequisites for Celery/Redis migration.** Implementing the recommended action plan sequentially (priorities 1–5) removes these debts and prepares the codebase for production scaling.
