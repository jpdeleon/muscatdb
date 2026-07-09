# API Route Consistency Audit

Date: 2026-07-08
Framework: **FastAPI** (already in use — not Flask)

## Problem

Routes follow two different prefix conventions:

| Convention | Routes |
|---|---|
| `/api/` prefix | target, ephemeris, fov, lco, settings, ads, exposure/target |
| Direct prefix | photometry, transit-fit, exposure, jobs |

Even within `/api/`, pluralization is inconsistent: `/api/target/` (singular) vs `/api/targets/` (plural, mixed in same module).

## Full Route Inventory

### Pages (HTML responses, not strictly APIs)

```
/                           index
/target                     target detail
/logs                       observation logs
/guide                      guide
/workflow                   redirect
/photometry                 photometry page
/transit-fit                transit fit page
/toi                        TOI catalog browse
/nexsci                     NExScI catalog browse
/exposure                   exposure calculator
/fov                        field-of-view tool
/ephemeris                  ephemeris page
/settings                   settings
/lco                        LCO main
/lco/schedule               LCO schedule
/lco/archive                LCO archive
/jobs                       jobs page
/{instrument}               instrument obslog
/{instrument}/{obsdate}     date-level obslog
/{instrument}/{obsdate}/ccd{ccd}  CCD-level obslog
```

### JSON APIs — Inconsistent (no `/api/` prefix)

```
/photometry/run             POST
/photometry/command         POST
/photometry/cancel          POST
/photometry/delete          POST
/photometry/status          GET
/photometry/status-batch    POST
/photometry/file/{...}      GET  (serves files)
/photometry/download-all/{...} GET

/transit-fit/run            POST
/transit-fit/cancel         POST
/transit-fit/delete         POST
/transit-fit/logp           POST
/transit-fit/status         GET
/transit-fit/query-archive  GET
/transit-fit/file/{...}     GET
/transit-fit/download-all/{...} GET

/exposure/calculate         POST
/exposure/calibrate         POST
/exposure/lookup-mags       POST
/exposure/status            GET
/exposure/coeffs/{inst}     GET

/jobs/status                GET
/jobs/rerun                 POST
/jobs/log/{...}             GET
/jobs/lco-archive/scan      POST
/jobs/lco-archive/ingest-date POST
```

### JSON APIs — Consistent (under `/api/`)

```
/api/target/harps-rv        GET
/api/target/publications    GET
/api/targets/export.csv     GET
/api/targets/{obj}/note     PUT / DELETE
/api/targets/{obj}/identified PUT

/api/ephemeris/targets      GET
/api/ephemeris/target-info  GET
/api/ephemeris/calculate    POST
/api/ephemeris/view         POST
/api/ephemeris/view/{slug}  GET

/api/fov/optimize           POST
/api/fov/resolve-target     POST
/api/fov/observable         GET

/api/lco/config             GET
/api/lco/proposals          GET
/api/lco/requestgroups      GET
/api/lco/visibility         GET
/api/lco/windows            POST
/api/lco/ipp                POST
/api/lco/submit             POST
/api/lco/split-ipp          POST
/api/lco/split-submit       POST
/api/lco/archive/frames     GET
/api/lco/archive/download   POST
/api/lco/archive/download/{job_id} GET

/api/settings/lco-token-status  GET
/api/settings/lco-token         POST
/api/settings/ads-token-status  GET
/api/settings/ads-token         POST

/api/ads/config             GET

/api/exposure/target/{target} GET  (nested under /api/)
```

## Surface Area

| Location | Count |
|---|---|
| Route definitions (`web.py`) | 82 routes |
| `fetch()` calls in templates | ~53 |
| Test `client.get/post()` calls | ~66 |
| Test assert/decorator references | ~17 |
| Total test functions referencing routes | ~95 |

## What Would Need to Change

### Decision 1: Namespace placement

Option A — move all JSON APIs under `/api/`:

```
Current                     Proposed
/photometry/run             /api/photometry/run
/transit-fit/status         /api/transit-fit/status
/exposure/calculate         /api/exposure/calculate
/jobs/status                /api/jobs/status
```

Option B — keep status-quo and only fix pluralization.

### Decision 2: Pluralization

- `/api/target/` → `/api/targets/` (consistent with existing `/api/targets/{obj}/note`)

### Decision 3: Restructuring in web.py

Current: all routes on `@app.get/post/...`
Proposed: organized into `APIRouter` instances with `prefix`:

```python
photometry_router = APIRouter(prefix="/api/photometry")
transit_fit_router = APIRouter(prefix="/api/transit-fit")
exposure_router = APIRouter(prefix="/api/exposure")
jobs_router = APIRouter(prefix="/api/jobs")
target_router = APIRouter(prefix="/api/targets")
```

Then `app.include_router(...)` each.

### Changes required in templates

All `fetch()` call URLs in these files must be updated:

- `photometry.html` (7 calls)
- `transit_fit.html` (8 calls)
- `exposure.html` (3 calls)
- `jobs.html` (5 calls)
- `base.html` — loading-spinner `/api/` path detection (line 38)

### Changes required in tests

- `test_photometry.py` (~48 route references)
- `test_web.py` (~18 route references)
- `test_transit_fit_runs.py` (~5 route references)
- `test_frontend_input_audit.py` (~11 endpoint pattern assertions)

## Recommendation

1. **Low effort, high value**: Only add OpenAPI docs by adding route tags/summaries now — FastAPI already generates `/docs`.
2. **Medium effort**: Choose a prefix convention and implement one router at a time in separate PRs, each with test updates.
3. **High effort**: Rename all routes in one shot — high regression risk due to the number of hardcoded strings across templates and tests.
