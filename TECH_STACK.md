# Tech Stack — muscat-db

Date: 2026-07-08

## Overview

Single-host monolithic web application for managing astronomical observation data,
running photometry and transit-fit pipelines, scheduling LCO observations, and
browsing exoplanet catalogs. Serves a research group with ~5 unique instruments
across multiple telescopes.

---

## Language & Runtime

| Layer | Technology |
|---|---|
| Language | **Python >=3.12** |
| Package manager | **uv** (uv.lock, .venv) |
| Build backend | **setuptools >=75** (`pyproject.toml`) |
| CLI entry point | `muscat-db` → `muscat_db.cli:app` |

---

## Web Layer

| Component | Technology |
|---|---|
| Web framework | **FastAPI 0.136.1** (async, `from fastapi import FastAPI`) |
| ASGI server | **uvicorn[standard] >=0.34** |
| Templating | **Jinja2 3.1.6** (server-side rendered HTML) |
| Data validation | **Pydantic >=2.13** (FastAPI dependency) |
| Auth | nginx HTTP Basic Auth (htpasswd) — not handled by FastAPI |

All routes live in a single `app = FastAPI()` instance in `src/muscat_db/web.py` (~5155 lines).
No `APIRouter` modularization is used currently.

---

## Database

| Aspect | Detail |
|---|---|
| Engine | **SQLite3** (stdlib `sqlite3`) |
| File | `muscat.db` (configurable via `MUSCAT_DB_PATH`) |
| Mode | WAL mode for concurrent reads |
| Schema | Created inline via `executescript()` + ALTER TABLE probes |
| Migrations | None — ad-hoc column additions at startup |
| ORM | None — raw SQL throughout |

### Tables (12)

| Table | Purpose |
|---|---|
| `frames` | Individual FITS frame metadata |
| `summaries` | Per-target/date/CCD summaries |
| `targets` | Per-target aggregation |
| `target_notes` | User notes on targets |
| `target_overrides` | Manual identification overrides |
| `jobs` | Background job tracking (photometry + transit fit) |
| `users` | User accounts (multi-user nginx deployment) |
| `db_meta` | Key-value metadata store |
| `exposure_coeffs` | Exposure calibration coefficients |
| `exposure_jobs` | Exposure calc job tracking |
| `ephemeris_views` | Saved ephemeris page states |

---

## Frontend

| Layer | Technology |
|---|---|
| CSS | **Custom** (`static/styles.css`, ~791 lines). No framework. |
| JavaScript | **Vanilla JS** — no React, Vue, Svelte, etc. |
| JS modules | 4 files in `static/js/`: `storage.js`, `format.js`, `jobPolling.js`, plus inline JS |
| Build tools | None — no package.json, webpack, vite, esbuild, TypeScript |
| Plotting | **Plotly** (CDN, TOI/NExScI catalog pages) |
| Diagrams | **Mermaid** (CDN, Guide page) |
| Fonts | System UI stack; monospace: JetBrains Mono / Fira Code |
| Theme | Light/Dark via CSS custom properties + `localStorage` |

Key architectural note: every UI interaction uses imperative vanilla JS with
`fetch()` calls to FastAPI endpoints. There is no reactive framework, no SPA
router, and no component abstraction.

---

## Python Dependencies

### Main

| Package | Purpose |
|---|---|
| `fastapi>=0.115` | Web framework |
| `uvicorn[standard]>=0.34` | ASGI server |
| `jinja2>=3.1` | HTML templating |
| `typer>=0.15` | CLI framework |
| `astropy>=7` | FITS I/O, coordinates, WCS |
| `astroquery>=0.4.7` | Astronomical catalog queries (Gaia, VizieR) |
| `cryptography>=49` | Fernet encryption for per-user API tokens |
| `pyarrow>=17` | FITS-to-parquet conversions |
| `rich>=13` | CLI output formatting |
| `python-dotenv>=1.0` | `.env` file loading |

### Dev

| Package | Purpose |
|---|---|
| `pytest>=9.0.3` | Test framework |
| `pytest-mock>=3.15.1` | Mocking |
| `httpx>=0.28` | Async HTTP client for FastAPI test client |
| `ruff>=0.8` | Linter (Pyflakes rules) |

---

## Deployment

| Component | Technology |
|---|---|
| Reverse proxy | **nginx** (`:8000` → uvicorn `:8001`) |
| Auth | HTTP Basic Auth via htpasswd file |
| Process supervision | **tmux** session `muscatdb-gui` (no systemd/supervisor) |
| Containerization | **None** — bare metal |
| CI/CD | **None** |
| Scheduling | **cron** daily: `muscat-db scan-yesterday && muscat-db build-db` |
| Server | 24-core, 100 GB RAM (single host) |

nginx config: `deploy/nginx.conf`

---

## External Science Pipelines

These live outside the repo and are invoked as subprocesses in separate conda environments:

| Pipeline | Location | Environment | Purpose |
|---|---|---|---|
| **prose2** | `/ut2/jerome/.../ext_tools/prose2` | `conda env prose` | Photometry reduction (`run_photometry.py`) |
| **timer** | `/ut2/jerome/.../ext_tools/timer` | (external) | Transit/inference fitting |

Both are invoked via `subprocess.Popen` with stdout/stderr logged to per-job files.

---

## External API Services

| Service | Purpose |
|---|---|
| **LCO Observation Portal** | Telescope scheduling, IPP, archive downloads |
| **nova.astrometry.net** | WCS solving for muscat/muscat2 (optional) |
| **NASA ADS** | Publication search on target pages |
| **Gaia DR3 (ESA/VizieR)** | FOV optimization star queries |
| **NASA Exoplanet Archive** | Transit/visibility verification |

---

## Architecture Diagram

```
                           ┌─────────────────────────────────────┐
                           │           nginx (:8000)             │
                           │  HTTP Basic Auth + reverse proxy    │
                           └──────────────┬──────────────────────┘
                                          │ proxy_pass :8001
                           ┌──────────────▼──────────────────────┐
                           │   uvicorn (FastAPI)                 │
                           │   Jinja2 templates                  │
                           │   Static files (CSS/JS)             │
                           └───────┬──────────┬──────────────────┘
                                   │          │
                    ┌──────────────▼──┐  ┌────▼──────────────┐
                    │  SQLite3 DB     │  │  Background Jobs   │
                    │  (muscat.db)    │  │  subprocess.Popen  │
                    │  WAL mode       │  │  prose2 / timer    │
                    └─────────────────┘  │  (separate conda)  │
                                         └────────────────────┘
```

## Source Code Layout (`src/muscat_db/`)

| Module | Lines | Purpose |
|---|---|---|
| `web.py` | 5155 | FastAPI routes, all endpoints and page handlers |
| `database.py` | ~1400 | SQLite schema, CRUD, DB build commands |
| `photometry.py` | 2022 | Photometry page backend + job launch |
| `transit_fit.py` | 2184 | Transit fit backend + job launch |
| `lco.py` | 1065 | LCO API integration |
| `fov.py` | 941 | Field-of-view optimizer (Gaia DR3) |
| `cli.py` | 644 | Typer CLI commands |
| `transit_obs.py` | 461 | Transit observability calculations |
| `exposure.py` | ~300 | Exposure time calculator |
| `jobs.py` | 300 | Background job lifecycle |
| `job_store.py` | 212 | Job persistence (Celery seam) |
| `coord.py` | 125 | Coordinate validation + aggregation |
| `config.py` | 113 | Environment variable registry |
| `instruments.py` | 113 | Instrument dataclasses |
| `band_utils.py` | 64 | Filter/band constants |
| `cache.py` | 117 | LRU cache |

**Total: ~24 modules, ~16 KLOC**

---

## Planned / Future

| Technology | Status | Purpose |
|---|---|---|
| **Celery** | Planned | Distributed task queue |
| **Redis** | Planned | Celery broker + result backend |
| **flower** | Optional | Celery monitoring |
| **Multi-server** | Planned | 48/120/120-core workers |

---

## Key Architectural Characteristics

- **Single-host monolithic** — all routes, DB, and job execution on one machine
- **No ORM** — raw SQLite3 with inline SQL throughout
- **No containerization** — runs bare metal
- **No CI/CD** — deployments are manual
- **No frontend build step** — vanilla JS + CSS served as static files
- **Dual runtime** — FastAPI web server (uv run) vs prose photometry (conda env prose)
- **External pipelines** — science code lives in separate repos, invoked as subprocesses
- **Inline schema migrations** — no Alembic; uses ALTER TABLE probes at startup
