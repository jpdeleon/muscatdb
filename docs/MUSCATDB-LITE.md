# MUSCATDB-LITE — Lightweight Modular Redesign

**Date:** 2026-07-14
**Status:** Architecture + implementation plan (design document)
**Scope:** A from-scratch `muscatdb` package with **identical features**, a **lightweight
footprint**, a **modular** install surface, and a sharpened focus on **security** and
**performance**.

---

## 1. Motivation & goals

`muscat-db` today is a capable but heavy monolith:

- **`web.py` is 4,236 lines** — every page route and every one of 11 API routers'
  handlers live in a single file.
- The **`.venv` is ~458 MB**, and **all dependencies are mandatory**.
- Three heavy libraries dominate the footprint, and each is pulled into the *core* import
  path by accidental coupling rather than genuine need.

The redesign keeps **every feature** while making the install proportional to what a user
actually runs, splitting the god-module into deep single-purpose modules, and turning the
already-strong-but-implicit security and job-lifecycle work into explicit, tested seams.

**Design goals**

1. **Lightweight** — a base install of ~15 MB; heavy science libraries only when their
   feature is installed.
2. **Modular** — optional pages install as named extras; core never imports an absent one.
3. **Secure** — preserve every existing control, close the known gaps, shrink the attack
   surface by shipping less.
4. **Fast** — no N×M disk walks on hot pages, pooled outbound HTTP, streaming logs, lazy
   imports, and a pull-based durable work queue for crash-safe multi-host scale-out (§12).
5. **Identical features** — a feature-parity matrix (§13) proves nothing is lost.

**Approach:** greenfield. A new `muscatdb` package is built fresh and features are ported
module-by-module; the current `src/muscat_db` is retired after parity. Proven *algorithms*
— the finalizing job-state machine, the security surface, the atomic DB rebuild, the O-C
math — are ported deliberately, never blindly reinvented.

---

## 2. Footprint: today vs target

Measured on the production host (`du -sh` of `.venv/lib/.../site-packages/<pkg>`):

| Package | Size | Used by (today) | Genuinely needed by |
|---|---:|---|---|
| pyarrow | **149 MB** | `catalog.py` only (Boyle feather) | `/toi` browser only |
| astropy | 43 MB | scan MEF, `transit_obs`, `exposure`, `fov` | `[obs]` + `[fov]`/`[expcalc]` |
| numpy | 32 MB | `transit_obs`, `exposure`, `fov` | `[obs]` + `[fov]`/`[expcalc]` |
| astroquery | 24 MB | `fov`, `exposure` (lazy) | `[fov]` / `[expcalc]` only |
| cryptography | 15 MB | `database.py` (Fernet, eager) | web token storage (lazy) |
| fastapi+pydantic+uvicorn+jinja2+httpx+rich+typer | ~15 MB | web + CLI | web / CLI |

**The tangle that forces heavy deps into core:**

```
web.py ─┬─ import exposure          (eager)  → numpy + astropy
        ├─ import fov               (eager)  → numpy
        └─ import catalog ─ import exposure   → numpy + astropy   (catalog.py:30)
lco.py ──── import catalog          (CORE reaching into an extra)  (lco.py:27)
database.py ─ import cryptography   (eager)  → Fernet always loaded (database.py:16)
```

Severing four edges — `web→{exposure,fov}`, `catalog→exposure`, `lco→catalog`,
`database→cryptography` — drops astropy, numpy, astroquery, pyarrow and cryptography out of
the base import path. **Result: base web ≈ 15 MB vs today's ~450 MB.**

---

## 3. Install matrix & extras

Same distribution, capability extras (PEP 621 `optional-dependencies`):

| Distribution | Adds | Enables |
|---|---|---|
| `muscatdb` (base) | typer, rich, python-dotenv | CLI: scan (raw header), obslog, database build/ingest/query |
| `muscatdb[web]` | fastapi, uvicorn, jinja2, httpx, cryptography, pyyaml | Web GUI core: index, target, logs, photometry, transit-fit, ephemeris+TTV, jobs, guide, settings |
| `muscatdb[obs]` | astropy, numpy | **LCO page** (schedule/archive/download + visibility + monitor), observability pre-checks, **scan MEF** fallback, transit observability |
| `muscatdb[toi]` | pyarrow | `/toi` browser + Boyle rotation catalog + ExoFOP confirm |
| `muscatdb[nexsci]` | *(stdlib only)* | `/nexsci` browser |
| `muscatdb[fov]` | astroquery, astropy, numpy | `/fov` Gaia pointing optimizer |
| `muscatdb[expcalc]` | astroquery, astropy, numpy | `/exposure` calculator |
| `muscatdb[all]` | union of the above | everything (today's behaviour) |
| `muscatdb[cluster]` | psycopg | PostgreSQL control-plane adapter for multi-host distributed execution (§12); orthogonal to `[all]`, installed on the web host and every worker host. Single-host runs on SQLite with no extra. |

```bash
uv add muscatdb                 # CLI + database only
uv add 'muscatdb[web]'          # lightweight web app (~15 MB base; no LCO)
uv add 'muscatdb[web,obs]'      # + LCO page, visibility, MEF scan
uv add 'muscatdb[all]'          # complete install (production default for release 1)
```

**Notes**

- PEP 621 cannot make one extra inherit another, so `[fov]`, `[expcalc]`, and `[all]`
  repeat the astropy+numpy union. A unit test asserts the unions stay synchronized.
- `[nexsci]` carries **no heavy dependency** — it is an extra purely for *feature modularity*
  (its browser code + page are opt-in), not for dependency weight. Its scatter plot is
  Plotly-from-CDN (frontend), and its catalog is a stdlib-parsed CSV.
- **The entire LCO page requires `[obs]`.** Although the LCO REST client itself is stdlib,
  visibility plots and the pre-submit observability check (both astropy) are integral to
  responsible scheduling, so the LCO router is mounted only when `obs` is present. `[obs]` is
  effectively the *observation* capability: LCO + transit observability + MEF scan.
- External science engines (`prose2`, `timer`, `harmonic`) are **not** packaged. They stay
  in their own conda environments; `muscatdb` detects and reports their *readiness*
  separately from its own *installation* (see §7).
- Production keeps `[all]` for the first release; a genuinely lightweight deployment adopts
  `[web]` (+ selected extras) once the isolated-install matrix (§14) proves parity.

---

## 4. Package layout & module responsibilities

```
src/muscatdb/
  __init__.py        loads .env once (find_dotenv, usecwd) before any submodule reads os.environ
  config.py          env-var registry (single source of truth) + typed accessors + startup status
  errors.py          MissingCapabilityError(capability, install_hint)  — the capability seam
  capabilities.py    has_capability(cap) / require(cap); import-probe based, cached

  core/                                  # base install — stdlib + typer/rich/dotenv only
    instruments.py   frozen instrument metadata (CCDs, prefixes, well/gain/scale/aperture)
    names.py         normalize_target_name, alias resolution, angular_sep_arcsec  (DEP-FREE)
    scan/
      headers.py     raw FITS-card parser (stdlib bytes reader); NO astropy on the fast path
      scanner.py     scan_date / scan_missing / scan_all / scan_yesterday; ProcessPool parse
    obslog.py        group contiguous frame runs by object/exptime/readmode -> obslog CSV
    db/                                  # CATALOG store: derived, read-mostly, daily-rebuilt
      conn.py        get_conn() context manager, WAL, busy timeout, row_factory policy
      schema.py      catalog SCHEMA (frames/summaries/targets) + PRAGMA user_version migrations
      build.py       atomic rebuild (<db>.tmp + os.replace); PURE derived swap — no app-table preservation
      ingest.py      ingest_date incremental single inst/date
      repos.py       frames / summaries / targets read repositories
    control/                             # CONTROL PLANE: durable, mutable, concurrent (§12)
      store.py       ControlStore interface + SqliteControlStore | PostgresControlStore ([cluster])
      queue.py       durable work queue: enqueue · claim (SKIP LOCKED) · lease/heartbeat · reclaim
      tables.py      jobs · settings/tokens · notes · overrides · lco_observation_* · ephemeris_views
      secrets.py     Fernet token storage behind a LAZY cryptography import seam
    jobs/
      lifecycle.py   PipelineJob, finalizing grace-window state machine, kill helpers (stdlib)
      runner.py      ONE JobRunner over the WorkQueue + ControlStore: enqueue/claim/status/cancel/delete
    cli.py           typer app: scan*, summary, build-db, ingest-date, serve, worker, restart, htpasswd

  worker/            worker run-loop (§12); no Celery — pulls from the durable queue
    loop.py          claim → lease/heartbeat → launch subprocess → finalize → record (txn)

  pipelines/                             # thin orchestration; the science lives in conda tools
    base.py          Pipeline interface: build_command · write_inputs · discover_outputs · env · markers
    prose.py         photometry adapter (prose2)
    timer.py         transit-fit adapter (timer)
    harmonic.py      ttv-fit adapter (harmonic)
    discovery.py     product discovery: manifest-first (<stem>_products.json), regex fallback

  web/                                   # muscatdb[web]
    app.py           build_app() factory: mounts core routers, gates extras, builds nav model
    middleware.py    auth (loopback-only X-Forwarded-User) + CSRF (same-origin)
    render.py        Jinja2 Environment, _render, static cache-bust, capability-aware nav
    http.py          pooled httpx clients (async+sync) with explicit timeouts + limits
    proxy.py         tess-quicklook reverse proxy (closed APPLICATIONS allowlist)
    resolve.py       CORE target/coord/planet resolver: httpx TAP + ADQL, stdlib — NO astropy
    ephemeris_math.py  pure-Python weighted O-C linear fit (tested; no numpy)
    lco/
      client.py      LCO Observation Portal REST client (httpx; SSRF allowlist)
      monitor.py     restart-safe DB-lease polling + ingest
      scheduling.py  window generation, requestgroup build/validate/submit (ALLOW_SUBMIT gate)
    routers/
      pages.py       /  ·  /logs  ·  /{inst}[/{date}[/ccd{n}]]  ·  /guide
      target.py      /target  + /api/targets/*  + /api/ads/*
      photometry.py  /photometry  + /api/photometry/*
      transit_fit.py /transit-fit + /api/transit-fit/*
      ephemeris.py   /ephemeris   + /api/ephemeris/*  + /api/ttv-fit/*
      jobs.py        /jobs        + /api/jobs/*
      lco.py         /lco/*       + /api/lco/*   (mounted only with [obs])
      settings.py    /settings    + /api/settings/*
    static/  templates/

  extras/                                # each optional; imported only when installed
    obs/       visibility.py (transit_obs) · simbad.py (SkyCoord.from_name) · mef.py     [astropy,numpy]
    toi/       browser.py (load_toi + Boyle + membership) · routes.py (/toi, /api/exofop/*)  [pyarrow]
    nexsci/    browser.py (load_nexsci + membership) · routes.py (/nexsci)                    [stdlib]
    fov/       optimizer.py (Gaia pointing) · routes.py (/fov, /api/fov/*)                    [astroquery]
    expcalc/   calculator.py (exposure + coeff store) · routes.py (/exposure, /api/exposure/*) [astroquery]
```

**Module-size discipline:** every module targets 200–400 lines (800 hard cap), replacing
the current 4,236-line `web.py`, 2,246-line `transit_fit.py`, and 1,792-line `catalog.py`.

---

## 5. The capability seam (how extras stay optional)

Three small modules make an extra genuinely optional — not just a dependency line, but an
import that never runs when absent.

**`errors.py`**

```python
class MissingCapabilityError(RuntimeError):
    def __init__(self, capability: str, install_hint: str):
        self.capability = capability
        self.install_hint = install_hint            # e.g. "uv add 'muscatdb[toi]'"
        super().__init__(f"{capability} not installed. Install: {install_hint}")
```

**`capabilities.py`** — probes importability once and caches:

```python
def has_capability(cap: str) -> bool: ...            # try import the extra's marker module
def require(cap: str) -> None:                        # raise MissingCapabilityError if absent
```

**`web/app.py`** — the composition root. It always mounts core routers, and for each extra
either mounts its router or registers a fallback that returns the install-hint page, so a
bookmarked `/toi` link on a lightweight install renders an actionable message instead of a
bare 404:

```python
def build_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    mount_core_routers(app)                          # pages, target, photometry, ...
    for cap, module in EXTRA_ROUTERS.items():        # obs→lco router, toi, nexsci, fov, expcalc
        if has_capability(cap):
            app.include_router(import_module(module).router)
        else:
            register_install_hint(app, cap)          # GET route -> hint page (chosen UX)
    app.state.nav = build_nav_model()                # marks which extras are available
    return app
```

**UX (chosen):** the nav link is **hidden** when its capability is absent; visiting the
route returns a small **install-hint page** naming the feature and the exact command. One
`MissingCapabilityError` type; the **CLI** formats it for a terminal, **web startup** for
logs, the **route** for HTML — three presentation adapters justify the single seam.

**Import discipline that makes it real** (the lesson from today's tangle):

- `core/*` and `web/*` never `import` an `extras/*` module at top level.
- Shared helpers an extra *and* core both need live in `core/names.py` (dep-free) — never
  in the extra. (This is what today's `lco.py → catalog._angular_sep_arcsec` violates.)
- Cross-capability calls go through a narrow interface resolved at call time
  (e.g. the core target resolver asks `obs` for SIMBAD name resolution via `require("obs")`
  then a lazy import), never a module-level import. The LCO router is itself `obs`-gated, so
  the astropy visibility/observability code inside it is only ever imported when `[obs]` is
  present.

---

## 6. Core deep modules

### 6.1 Scan → obslog → database

The pipeline is file-mediated (CSV hand-off), which keeps each stage independently testable:

- **Scan** (`core/scan/`): a fast raw-bytes FITS-card reader parses primary headers with no
  astropy. Multi-extension (MEF) files fall back to astropy **through the `obs` seam** — and
  if `[obs]` is absent, scan **fails loudly** with the install hint rather than silently
  emitting an empty result (a scientifically-unsafe outcome the design forbids). CPU-bound
  parsing runs under `ProcessPoolExecutor`; `max_workers=1` remains a deliberate serial path
  for the LCO monitor thread.
- **Obslog** (`core/obslog.py`): groups contiguous frame runs by object/exptime/read-mode.
- **Catalog database** (`core/db/`): `build.py` performs the atomic rebuild (`<db>.tmp` +
  `os.replace`) that never blocks a running server. Because mutable app data now lives in the
  **separate control plane** (§12), this store holds *only* derived observation tables
  (frames/summaries/targets), so the rebuild is a **pure disposable swap** — the old
  "preserve 9 app-owned tables across the DROP" dance is gone, which removes a real fragility.
  `schema.py` replaces today's ad-hoc `ALTER TABLE` probes with **`PRAGMA user_version`
  versioned migrations**.

### 6.2 Data stores

Two stores with different access patterns, split on purpose (§12):

- **Catalog (SQLite)** — a single `get_conn()` context manager is the only way to open it
  (WAL, 30 s busy timeout, explicit `row_factory` policy); free-function repositories return
  `list[dict]`. Build/ingest keep their two specialized connections (they need
  `create_aggregate("coord_repr")` and build-time PRAGMAs) — the two intentional exceptions.
- **Control plane (`ControlStore`)** — all mutable/concurrent state (jobs+queue, settings,
  tokens, notes, overrides, lco_*, ephemeris_views) behind one interface with an SQLite
  adapter (single-host default) and a PostgreSQL adapter (`[cluster]`, multi-host). The web
  host and every worker read/write it transactionally; there is no separate "jobs writer."

### 6.3 The unified `JobRunner` (dedupes today's triplication)

Today photometry, transit-fit, and ttv-fit each copy ~400–600 lines of orchestration
(`sync_jobs` + queue-drain + status layering + cancel/delete). The redesign keeps the
already-extracted **shared math** (`lifecycle.py`: finalizing grace-window state machine,
run-id/kill helpers) and adds **one `JobRunner`** that owns the whole lifecycle over any
`Pipeline`, a durable `WorkQueue`, and the `ControlStore` (§12):

```python
class JobRunner:
    def __init__(self, pipeline: Pipeline, queue: WorkQueue,
                 store: ControlStore, finalize: FinalizeConfig): ...
    def enqueue(self, job: PipelineJob) -> str: ...    # web host: durable insert + NOTIFY
    def claim_and_run(self) -> None: ...               # worker: SKIP-LOCKED claim → lease → subprocess
    def status(self, job_id: str) -> JobStatus: ...    # single indexed query
    def cancel(self, job_id: str) -> None: ...         # revoke if queued; process-group SIGTERM if running
    def delete(self, run_id: str) -> None: ...
```

Photometry/transit-fit/ttv-fit collapse to a `Pipeline` adapter + a `FinalizeConfig`
(photometry sets `success_marker`+`partial_failure_marker`; timer keeps the parent return
code authoritative). **Leverage:** one interface, three pipelines; a lifecycle fix lands
once. The web host only ever **enqueues**; **workers** claim and run — the same code whether
one worker runs on ut2 or many run across the cluster. The `WorkQueue`/`ControlStore` and
their SQLite/Postgres adapters are detailed in §12.

---

## 7. Pipelines interface & the manifest contract

`pipelines/base.py` defines the adapter interface every external engine satisfies:

```python
class Pipeline(Protocol):
    conda_env: str                                     # "prose" | "timer" | "harmonic"
    def build_command(self, spec) -> list[str]: ...
    def write_inputs(self, run_dir, spec) -> None: ...  # YAML/INI/CSV inputs
    def discover_outputs(self, run_dir) -> list[Product]: ...
    def env(self) -> dict[str, str]: ...                # TMPDIR redirect, inherited .env
    markers: FinalizeConfig                             # success / partial-failure log lines
```

Each engine is discovered by `_conda_env_python(env)` and launched detached
(`start_new_session=True`) with its log tee'd to the run directory.

**Manifest-first discovery (fixes the brittle cross-repo coupling, audit H3).** Today
`photometry.list_outputs` reverse-engineers prose2 filenames with layered regexes; a single
rename in prose2 silently empties the photometry page. The redesign reads a
`<stem>_products.json` manifest emitted by the engine when present, and falls back to the
current regex only for legacy runs. The manifest schema becomes the shared contract:

```json
{ "files": [{ "path": "...", "type": "lightcurve|plot|log", "band": "g", "desc": "..." }],
  "metadata": { "engine": "prose2", "version": "...", "target": "...", "date": "..." } }
```

**Installation vs readiness vs running** are three distinct states surfaced to the user:

- *installed* — the `muscatdb` orchestration + route exist;
- *ready* — the configured conda env/executable is present and passes a version probe;
- *running* — a job has been claimed from the durable queue by a worker (§12).

---

## 8. Web layer

- **App factory** (`web/app.py`): the composition root from §5. No module-level side effects;
  the app is built by a function so tests and isolated-install rows can construct variants.
- **Rendering** (`web/render.py`): the manual Jinja2 `Environment` (with mtime cache-busted
  `static_url`) and `_render` helper carry over; the nav is a **capability-aware model** so
  templates render only installed pages.
- **Core resolver vs SIMBAD** (the key dependency cut): `web/resolve.py` keeps target-name
  normalization, alias resolution, and archive/TAP coordinate+planet resolution on
  **httpx + stdlib** (no astropy). The one place today's `catalog.py` reaches for astropy —
  `exp_calc.resolve_target_coords` → `SkyCoord.from_name` (SIMBAD) — moves to
  `extras/obs/simbad.py` and is called only when `[obs]` is present. This severs
  `catalog → exposure → astropy` at the root.
- **LCO** (`web/lco/`, `routers/lco.py`): the Observation-Portal REST client moves from
  `urllib` to pooled httpx with timeouts; the SSRF download allowlist and per-user encrypted
  tokens are preserved. **The entire LCO page requires `[obs]`** — its router is mounted only
  when the `obs` capability is present (visibility and the pre-submit observability check are
  integral to responsible scheduling), and the nav link hides / the route returns the
  install-hint otherwise. The restart-safe monitor daemon likewise runs only under `[obs]`.
- **Companion proxy** (`web/proxy.py`): the closed reverse proxy for `/tess-quicklook`
  (loopback-only backend, static `APPLICATIONS` registry) carries over unchanged in intent.

---

## 9. Extras

| Extra | Deps | Modules | Routes | Depends on core |
|---|---|---|---|---|
| `obs` | astropy, numpy | `visibility`, `simbad`, `mef` (+ gates `routers/lco.py`, `web/lco/*`) | `/lco/*`, `/api/lco/*` | names, resolve, db, scan seam |
| `toi` | pyarrow | `browser`, `routes` | `/toi`, `/api/exofop/check_confirmed` | resolve, names, db |
| `nexsci` | *(stdlib)* | `browser`, `routes` | `/nexsci` | resolve, names, db |
| `fov` | astroquery, astropy, numpy | `optimizer`, `routes` | `/fov`, `/api/fov/*` | names, instruments |
| `expcalc` | astroquery, astropy, numpy | `calculator`, `routes` | `/exposure`, `/api/exposure/*` | instruments, db |

Each extra owns its router and registers via §5. The TOI/NExScI browsers reuse the **core**
`resolve.py` and `names.py` for membership/aliasing; the pyarrow Boyle-catalog read stays
lazy inside `toi/browser.py`. HARPS/JWST/spectra membership (shared by the core target page
*and* the browsers) lives in core `resolve.py`, so the browsers add features on top rather
than owning shared logic.

---

## 10. Security architecture

**Preserved controls (ported verbatim in intent):**

- **Auth**: nginx HTTP Basic Auth → forwards `X-Forwarded-User`, trusted **only when the TCP
  peer is loopback** (defeats header spoofing on a `0.0.0.0` bind). WebSocket CSWSH guard
  recomputed from the handshake (middleware doesn't run for WS scopes).
- **CSRF**: same-origin (Origin/Referer vs Host) on state-changing routes → 403 otherwise.
- **Secrets at rest**: per-user LCO + ADS tokens Fernet-encrypted; key derived from
  `MUSCAT_DB_SECRET` (passphrase → SHA-256 → urlsafe-b64). No hardcoded secrets; every
  secret sourced from env/`.env`.
- **SSRF**: LCO downloads allowlist `https` + `*.lco.global`/`*.amazonaws.com` before
  fetching; the companion proxy chooses destinations only from a static registry and strips
  inbound `X-Forwarded-*`.
- **Path traversal**: artifact serving `resolve()`s then `relative_to(base)`, 404 on escape.
- **SQL**: parameterized throughout; the four interpolated statements use trusted module
  constants (PRAGMA/table names), never user input. ADQL literals escaped.
- **htpasswd**: file `0640 root:www-data`; passwords piped to `openssl passwd` via stdin so
  they never appear in `ps`/`/proc`.

**Added / strengthened:**

- **Fail-fast secret validation**: when `[web]` + settings are enabled, validate
  `MUSCAT_DB_SECRET` at startup rather than lazily on first token read.
- **nginx ↔ uvicorn shared secret**: an optional shared header closes the documented gap
  where another local account could reach loopback `:8001` directly and present a forged
  `X-Forwarded-User`.
- **Security headers**: `Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy`, `X-Frame-Options` (app- or nginx-level).
- **Boundary validation**: Pydantic request models on API endpoints replace manual
  `dict`/`request.json()` parsing (schema-validated, fail-fast).
- **Smaller attack surface**: the base install ships none of astropy/numpy/astroquery/
  pyarrow and mounts none of the extra routers.
- **Control-plane isolation** (multi-host, §12): workers connect to PostgreSQL with a
  **least-privilege role** (only the control-plane tables it needs, no superuser), over the
  private cluster network / TLS. Because workers write state transactionally to the DB rather
  than through an app callback, there is no internal write endpoint to authenticate or abuse.

---

## 11. Performance architecture

- **Kill the N×M disk walk on index render** (audit M2): the home page currently calls
  per-target × per-date disk `stat`/scan to compute photometry/fit status. Materialize that
  status into the DB at build/ingest (or an mtime-keyed cache) so the index is a single
  indexed query.
- **Read path** stays healthy: WAL, `frames` indexed on `(instrument,obsdate)`+`object`,
  pre-aggregated `summaries`, materialized `targets`.
- **Bounded caches**: reuse the thread-safe `LRUCache`/`TTLCache`; no unbounded module dicts.
- **Outbound HTTP off the request path**: pooled httpx (async+sync) with explicit timeouts
  and connection limits; no `urllib` on request threads.
- **Live logs via SSE/WebSocket** as the default instead of 2 s polling; batched status as
  the norm (today's per-job polling becomes the exception).
- **Fast cold start**: lazy imports + the app factory mean the base app never imports an
  absent extra; astropy/numpy load only when an `[obs]`/`[fov]`/`[expcalc]` path runs.
- **Concurrency**: the per-pipeline cap is a **cluster-wide** SQL claim (`SKIP LOCKED`, §12),
  correct across hosts — not a per-process integer.
- **Frontend**: extract shared JS (`jobPolling`, an options-registry helper) to remove the
  documented `collectOptions`/`restoreOptions` hand-sync footgun, and split the 2–3k-line
  templates (`ephemeris.html` 2,895 · `transit_fit.html` 2,696 · `photometry.html` 1,515).

---

## 12. Distributed execution (durable work queue)

Photometry / transit-fit / TTV work spreads across a multi-host cluster (ut2 + ut3/ut6, later
ut4/ut5/ut7): a handful of heavy, internally-parallel jobs per day. For **this** workload the
robustness-and-performance optimum is a **pull-based durable work queue over a transactional
control plane — not a message broker.** Celery / Redis / RabbitMQ are deliberately **not
used**: for a few named jobs a day they add a broker, a result backend, and visibility-timeout
/ lost-message edge cases (more moving parts, more failure modes, a second source of truth
competing with the DB) while buying nothing a durable SQL queue doesn't already give.

### One execution model (no dual path, no flag)

Every worker — co-located on ut2 or remote on ut3/ut6 — runs the **same** loop:

1. **Claim** the next job for its pipeline with an atomic skip-locked update (PostgreSQL
   `SELECT … FOR UPDATE SKIP LOCKED`; SQLite `BEGIN IMMEDIATE` + a guarded `UPDATE`),
   enforcing a **cluster-wide per-pipeline concurrency cap counted in the same transaction**.
   Exactly one worker ever owns a job.
2. **Lease + heartbeat**: the claim writes a lease with an expiry and the worker heartbeats
   while running. A crashed / rebooted worker's lease expires and the job is **automatically
   reclaimed** — no stuck `running` rows, no manual retry channel.
3. **Execute**: launch the conda subprocess (prose / timer / harmonic), own its process group,
   and resolve the **`finalizing` grace window locally** (the worker holds the process and the
   log mtime).
4. **Record transactionally**: state transitions are written to the *same* store as the queue
   — **no dual-write, no single-writer HTTP callback, no second source of truth.**

Single-host = one worker process on ut2. Multi-host = the same worker on more hosts. There is
**no `LocalDispatcher`/`CeleryDispatcher` fork and no `MUSCAT_CELERY_ENABLED` flag** — the only
variable is how many workers run and where.

### Two stores, cleanly split (a robustness win, not just scale)

- **Catalog store — SQLite (`muscat.db`).** frames / summaries / targets: derived,
  read-mostly, **rebuilt atomically each day and fully disposable.** Local to the web host,
  WAL reads. Because it now holds *only* derived data, the daily rebuild is a pure file swap
  with **no app-owned-table preservation** — the fragile "copy 9 tables across the DROP" dance
  is gone.
- **Control plane — the system of record for everything mutable/concurrent**: the job
  queue + state + leases, per-user settings/tokens, notes, overrides, `lco_observation_*`,
  `ephemeris_views`. One `ControlStore` interface, two adapters selected by
  `MUSCAT_CONTROL_PLANE`:
  - **SQLite (default, single-host).** Zero infra; WAL + busy-timeout handles concurrent
    *local* worker + web processes correctly. Optimal for the common single-host researcher.
  - **PostgreSQL (`[cluster]`, multi-host).** The moment work leaves ut2, SQLite-over-NFS is
    unsafe; Postgres is built for concurrent networked writers. Workers on any host connect
    directly with least-privilege DB credentials — the transactional claim + state write
    replace the entire callback channel.

Splitting the **disposable catalog** from the **durable control plane** means the daily
rebuild can never endanger user data (different stores) and a control-plane change never
touches the catalog.

### Signalling & live logs (still no Redis)

- **Instant dispatch**: workers `LISTEN` on a control-plane channel; enqueue issues a `NOTIFY`
  (Postgres) or a lightweight local wakeup (SQLite). Idle workers pick up in milliseconds
  without polling; a slow fallback poll covers any missed signal.
- **Live logs**: pipelines append to log files on the **shared mount**; the web host tails
  them and streams to the browser over **SSE**. No pub/sub bus, no log lines in the database.

### Install & supervision

| Distribution | Adds | Role |
|---|---|---|
| `muscatdb[cluster]` | psycopg | PostgreSQL control-plane adapter — installed on the web host **and** every worker host of a multi-host deployment |

The worker is a **run-mode**, `muscatdb worker --pipeline photometry` (a CLI command), not a
separate package: a worker host installs `muscatdb[cluster]` plus whatever capabilities it
executes. **Redis and Celery appear nowhere.** Web and workers run as **systemd units**
(survive reboot, unlike the `muscatdbgui` tmux session), each pinning `OMP_NUM_THREADS` /
`MKL_NUM_THREADS` / `OPENBLAS_NUM_THREADS` to the host's real core budget so a heavy prose run
never oversubscribes (ut2's ambient `OMP_NUM_THREADS=100` would swamp 28 threads ~100×).

### Topology (multi-host)

```
ut2 (web host)                          ut3 / ut6 / … (worker hosts)
  FastAPI: validate → enqueue (txn)       muscatdb worker (systemd)
  reads catalog (local SQLite)            claim (SKIP LOCKED) + lease/heartbeat
  reads logs (shared mount) → SSE         owns subprocess + finalizing
        │                                 writes state → control plane (txn)
        └──────── PostgreSQL control plane (ut2) ────────┘
   shared mount: OBSLOG / DATA / PROSE / TIMER / TTV — identical paths on every host
   (host-local scratch: MUSCAT_TMPDIR)
```

### Robustness properties

- **Exactly-one execution** per job (atomic claim); idempotent, `run_id`-keyed run dirs make a
  reclaimed job safe to relaunch into the same directory.
- **Crash-safe**: worker/host death → lease expiry → automatic reclaim; a web-process restart
  never loses jobs (workers are independent, state is durable).
- **No lost updates / no NFS-SQLite corruption**: every concurrent write hits one transactional
  store.
- **Cluster-wide concurrency cap** enforced in SQL — correct across hosts, replacing the
  per-process `_MAX_FULL_JOBS` integer.
- **Backpressure**: excess jobs wait durably in the queue; nothing is dropped on a worker
  outage.

### Performance properties

- Millisecond dispatch via `NOTIFY`; status is a single indexed query; live logs are a file
  tail → SSE (compositor-friendly, zero polling).
- Horizontal scale is trivial: add a host, install `muscatdb[cluster]`, point it at Postgres +
  the shared mount, start the worker unit — no broker to scale or monitor.
- The dominant cost is the science subprocess itself; queue overhead is negligible, so the
  complexity budget is spent on core-pinning and instant pickup, not broker plumbing.

### Invariants the design honours

- **Shared mounts**: `MUSCAT_OBSLOG_DIR`, `MUSCAT_DATA_DIR`, `MUSCAT_PROSE_DIR`,
  `MUSCAT_TIMER_DIR`, `MUSCAT_TTV_DIR` resolve to one shared location on every host;
  `MUSCAT_TMPDIR` stays host-local scratch (`.env.example` already documents this split).
- **Conda envs unchanged** (`prose` / `timer` / `harmonic` per worker host); the science stays
  in the external engines, never copied into `muscatdb`.
- **LCO monitor** already elects a single active worker via a control-plane lease, so it is
  multi-host-safe as-is.

---

## 13. Feature-parity matrix (proves "identical features")

Every current page/route → its home in the redesign. **Core** pages work on `[web]`;
**extra** pages require their capability.

| Feature / page | Today | New home | Install |
|---|---|---|---|
| Home / targets table / search | `web.py` `/` | `web/routers/pages.py` | `[web]` |
| ObsLog → dates → CCD → frames | `/logs`, `/{inst}[/{date}[/ccd{n}]]` | `pages.py` | `[web]` |
| Target page (aliases, TIC, HARPS/JWST/spectra, ADS) | `/target` + `/api/targets/*` + `/api/ads/*` | `routers/target.py` + `web/resolve.py` | `[web]` |
| Photometry (run/status/cancel/delete/download) | `/photometry` + `/api/photometry/*` | `routers/photometry.py` + `pipelines/prose.py` + `JobRunner` | `[web]` |
| Transit fit (new/continue/secondary eclipse) | `/transit-fit` + `/api/transit-fit/*` | `routers/transit_fit.py` + `pipelines/timer.py` | `[web]` |
| Ephemeris O-C + provenance + export | `/ephemeris` + `/api/ephemeris/*` | `routers/ephemeris.py` + `web/ephemeris_math.py` | `[web]` |
| TTV fit (harmonic) | `/api/ttv-fit/*` | `routers/ephemeris.py` + `pipelines/harmonic.py` | `[web]` |
| Jobs (history, live log, rerun) | `/jobs` + `/api/jobs/*` | `routers/jobs.py` + `core/jobs/*` | `[web]` |
| LCO schedule / archive / download + monitor | `/lco/*` + `/api/lco/*` | `routers/lco.py` + `web/lco/*` (obs-gated) | `[web,obs]` |
| LCO **visibility** + observability pre-check | `transit_obs` via `/api/lco/*` | `extras/obs/visibility.py` | `[web,obs]` |
| Settings (encrypted LCO/ADS tokens) | `/settings` + `/api/settings/*` | `routers/settings.py` + `core/db/secrets.py` | `[web]` |
| Guide (Mermaid) | `/guide` | `pages.py` | `[web]` |
| tess-quicklook proxy | `proxy.py` | `web/proxy.py` | `[web]` |
| Scan MEF-header fallback | `scanner` (lazy astropy) | `core/scan` + `extras/obs/mef.py` | base + `[obs]` |
| TOI browser + Boyle + ExoFOP | `/toi` + `/api/exofop/*` | `extras/toi/*` | `[toi]` |
| NExScI browser | `/nexsci` | `extras/nexsci/*` | `[nexsci]` |
| FOV optimizer | `/fov` + `/api/fov/*` | `extras/fov/*` | `[fov]` |
| Exposure calculator | `/exposure` + `/api/exposure/*` | `extras/expcalc/*` | `[expcalc]` |
| CLI scan/build-db/ingest/serve/htpasswd | `cli.py` | `core/cli.py` | base |

---

## 14. Greenfield port plan (phases)

Each phase is small, independently testable, and lands while the live daily-cron +
auto-deploy system keeps running on the old package.

- **P0 — Scaffold.** New `muscatdb` package beside `muscat_db`; `pyproject.toml` extras
  matrix; `errors.py` + `capabilities.py`; CI rows for isolated extra installs.
- **P1 — Core base.** config, instruments, names, scan (raw + obs seam), obslog,
  db (conn/schema/build/ingest/repos/secrets), cli. *Parity:* `scan` + `build-db` reproduce
  a byte-equivalent DB against the same CSVs.
- **P2 — Jobs core.** lifecycle + `ControlStore` (SQLite adapter) + the durable `WorkQueue`
  (SKIP-LOCKED claim, lease/heartbeat, reclaim) + the unified `JobRunner` + the `muscatdb
  worker` run-mode; port the finalizing machine; unit-test crash/reclaim and the concurrency
  cap against fixtures. Single-host runs one co-located worker.
- **P3 — Pipelines.** prose/timer/harmonic adapters over `Pipeline`; manifest-first
  discovery with regex fallback.
- **P4 — Web core.** app factory, middleware, render, http, proxy, resolve, ephemeris_math,
  lco (client/monitor/scheduling), and the seven core routers (pages, target, photometry,
  transit-fit, ephemeris, jobs, settings); the LCO router lands in P5 as an `obs`-gated
  mount. *Parity:* each route responds equivalently.
- **P5 — Extras.** obs (visibility/simbad/mef **+ the `obs`-gated LCO router**), toi, nexsci,
  fov, expcalc — each with capability gate, install-hint fallback, and an isolated-install CI
  row. Verify LCO is unreachable (nav hidden, route → install-hint) without `[obs]`.
- **P6 — Frontend.** shared JS modules, capability-aware nav, template split.
- **P7 — Security hardening.** startup secret validation, nginx↔app shared secret, security
  headers, Pydantic request models, httpx-only outbound.
- **P8 — Cutover.** run old + new in parallel, diff outputs, flip the `serve` entrypoint,
  retire `muscat_db`.
- **P9 — Multi-host scale-out (§12).** Add the `PostgresControlStore` adapter (`[cluster]`)
  behind the same `ControlStore`/`WorkQueue` interface; stand up PostgreSQL on ut2, run the
  **unchanged** worker loop on ut3/ut6 as systemd units with core-pinned
  OMP/MKL/OpenBLAS caps, set `MUSCAT_CONTROL_PLANE=postgres`. No code change to the runner or
  pipelines — only the store adapter and where workers run.

---

## 15. Testing & verification

- **Isolated-install matrix** (fresh venv per row): `base / web / obs / toi / nexsci / fov /
  expcalc / cluster / all`. Each row: import the package, run CLI `--help`, build+query a
  SQLite DB from CSVs, and exercise that row's routes/features.
- **Import-contract tests**: the base package imports with astropy, numpy, astroquery,
  pyarrow, cryptography, and **psycopg absent**; requesting a missing capability returns the
  documented install hint; a MEF scan without `[obs]` **never** silently yields an empty
  result; importing `[web]` never depends on an undeclared transitive package.
- **Queue/robustness tests** (§12): the `WorkQueue` runs against **both** the SQLite and
  Postgres adapters — atomic claim never double-executes; a killed worker's lease expires and
  the job is reclaimed and completes; the per-pipeline concurrency cap holds under concurrent
  claims; a web-process restart loses no job; operations are idempotent under replay.
- **Parity tests**: the §13 matrix is all-green; route responses match the current app.
- **Security tests**: a forged `X-Forwarded-User` from a non-loopback peer is rejected;
  SSRF, path traversal, and CSRF attempts are blocked; secret-absent fails fast; the worker
  Postgres role cannot exceed its control-plane grants.
- **Performance checks**: the index render issues no N×M disk walk; base-app cold-start time
  is recorded.
- **On-host slow suite** (`uv run pytest -m slow`): real prose/timer/harmonic runs validate
  end-to-end parity on the production host (these `pytest.skip` cleanly off-host).

---

## 16. Migration & rollback

- The new package is developed on the `test` branch; production stays on `muscat_db[all]`.
- **One-time control-plane extract**: the current `muscat.db` mixes derived and app-owned
  tables. Cutover copies the app-owned rows (jobs, settings/tokens, notes, overrides, lco_*,
  ephemeris_views) into the new `ControlStore` (SQLite by default) once; thereafter the
  catalog `muscat.db` is rebuilt as pure derived data. Product directories, obslog CSVs, and
  the catalog rebuild are otherwise unchanged.
- Cutover flips only the `serve` entrypoint; rollback is reverting the entrypoint (the old
  `muscat.db` is left intact until the new control plane is verified).
- `muscat_db` is retired only after the isolated-install matrix and the parity suite are
  green and one release has run production on the new package under `[all]`.

---

## Appendix — source of the port (critical files)

`src/muscat_db/{web.py, database.py, photometry.py, transit_fit.py, ttv_fit.py, jobs.py,
job_store.py, catalog.py, exposure.py, fov.py, lco.py, lco_monitor.py, transit_obs.py,
ephemeris_math.py, scanner.py, summarizer.py, auth.py, proxy.py, http_client.py, cache.py,
coord.py, config.py, instruments.py, cli.py}` · `pyproject.toml` · `deploy/nginx.conf` ·
`.env.example` · `docs/architecture_audit.md` · `docs/DEPLOYMENT.md`.
