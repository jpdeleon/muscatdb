# Modular installation design for muscat-db

Date: 2026-07-14

## Outcome

Keep one Python distribution, make its default installation a small database
and observation-log tool, and add capabilities through named extras. Extras are
only half of the change: imports and command/route registration must also stop
loading optional implementations at process startup.

The recommended user-facing installation interface is:

```bash
# CSV observation logs, SQLite database, and core CLI
uv add muscat-db

# FITS scanning, including the Astropy fallback for extension headers
uv add 'muscat-db[scan]'

# Complete web application
uv add 'muscat-db[web]'

# Complete local installation
uv add 'muscat-db[all]'
```

Do not package `prose2`, `timer`, or `harmonic` into these extras. They are
external science engines in dedicated conda environments. muscat-db should
detect and report their readiness separately from its own installation.

## Current coupling

All runtime dependencies are currently mandatory in `pyproject.toml`:

```text
pip/uv install muscat-db
          |
          +-- CLI and database
          +-- FastAPI, Uvicorn, and Jinja2
          +-- Astropy and Astroquery
          +-- PyArrow
          +-- Cryptography
```

The local environment illustrates the cost. Package directories are
approximately 149 MB for PyArrow, 43 MB for Astropy, 32 MB for NumPy, 24 MB for
Astroquery, and 15 MB for Cryptography. The whole `.venv` is approximately
448 MB. These figures are diagnostic, not portable wheel-size guarantees.

The dependency list is not the only source of coupling:

- `web.py` imports photometry, exposure, transit fitting, TTV fitting, LCO,
  transit observability, FOV optimization, and catalog modules at startup.
- `catalog.py` imports FastAPI types and the Astropy-backed `exposure` module.
- `database.py` imports Cryptography at startup, although encryption is needed
  only for stored user credentials and tokens.
- `cli.py` constructs one command tree and imports several command
  implementations at startup.
- `transit_fit.py` imports `yaml`, but PyYAML is not declared directly. It is
  currently available only incidentally through another dependency.

Consequently, merely moving names from `dependencies` to
`optional-dependencies` would produce import errors in otherwise unrelated
workflows.

## Proposed capability modules

### 1. Core

**Recommendation:** Strong

**Files:** `pyproject.toml`, `src/muscat_db/__init__.py`,
`src/muscat_db/{config,instruments,scanner,summarizer,database}.py`,
`src/muscat_db/cli.py`

The core interface should cover:

- configuration and instrument metadata;
- raw primary-header scanning for normal FITS files;
- observation-log CSV generation and summaries;
- SQLite database build, ingestion, and queries;
- core CLI commands.

Keep only small, universally used dependencies in the default installation.
Typer, Rich, and python-dotenv are reasonable core dependencies while the
official interface remains a rich CLI that automatically loads `.env`.

Astropy is not universally needed by the scanner: `scanner.py` already uses a
raw FITS-card parser first and imports Astropy lazily only for extension-header
fallback. That existing seam makes `scan` a natural optional capability.

Cryptography should not remain an unconditional import in the database module.
Credential encryption can use a lazy import and raise a named capability error
only when a caller actually reads or writes an encrypted secret.

### 2. Scan

**Recommendation:** Strong

**Dependencies:** `astropy>=7`

The `scan` extra supplies the robust fallback for multi-extension FITS files
and any scan behavior that needs Astropy. The raw primary-header path can stay
in core, but correctness must take precedence over silently skipping extension
headers.

The command should therefore fail clearly when all of these are true:

1. the requested keys are absent from the primary header;
2. an extension scan is required;
3. the `scan` extra is not installed.

It should report:

```text
This FITS file requires Astropy extension-header support.
Install it with: pip install 'muscat-db[scan]'
```

Silently treating such a file as empty would make a lightweight installation
scientifically unsafe.

### 3. Web

**Recommendation:** Strong for the first release

**Dependencies:** FastAPI, Uvicorn, Jinja2, HTTPX, Cryptography, PyYAML, and the
astronomy dependencies required by all currently exposed pages.

For the first modular release, `[web]` should install a complete web
application. This preserves the current operator interface and avoids pages
that import successfully but fail only after a user submits a form.

Later, if there is a real use case for a database-only web viewer, split route
modules and add a smaller `[web-core]`. Do not expose that extra until its
reduced page set and navigation are intentionally designed.

The web module should validate capabilities once during startup and produce a
single actionable error. Avoid scattered `try/except ImportError` blocks in
individual routes.

### 4. Catalog and field optimization

**Recommendation:** Worth exploring after `[web]`

**Dependencies:** NumPy, Astropy, Astroquery, and PyArrow.

This is the largest dependency group and therefore offers the greatest size
reduction. It currently has weak locality:

- `exposure.py` imports NumPy and Astropy at module load;
- `fov.py` imports NumPy at module load and Astroquery lazily;
- `catalog.py` imports PyArrow lazily for Feather data but also imports
  FastAPI request types and `exposure` at module load.

Deepen this into one astronomy capability whose interface performs coordinate
resolution, catalog access, exposure calculations, and FOV calculations. Its
implementation may keep internal lazy imports. This concentrates dependency
knowledge and capability errors in one place.

Do not create separate extras for every library. Users understand capabilities
such as `catalog` or `web`; they should not need to know that one route happens
to use Feather.

### 5. External science pipelines

**Recommendation:** Preserve the existing process seam

**Files:** `src/muscat_db/{photometry,transit_fit,ttv_fit}.py`

The orchestration code is lightweight Python and can ship with muscat-db. The
actual implementations remain:

```text
muscat-db pipeline runner
          |
          +-- prose adapter    -> conda env prose
          +-- timer adapter    -> conda env timer
          +-- harmonic adapter -> conda env harmonic
```

Installation and readiness are different states:

- **installed:** the muscat-db orchestration and web route exist;
- **ready:** the configured executable/environment is present and passes a
  lightweight version probe;
- **running:** a job has been submitted to the local runner or future queue.

This distinction also gives the planned Celery implementation a stable seam:
the local-process and Celery adapters can satisfy the same runner interface.

## Proposed `pyproject.toml` shape

This is a design sketch, not a drop-in patch. Exact minimum versions should be
verified against the lock file and test matrix.

```toml
[project]
dependencies = [
    "typer>=0.15",
    "rich>=13",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
scan = [
    "astropy>=7",
]
catalog = [
    "numpy>=2",
    "astropy>=7",
    "astroquery>=0.4.7",
    "pyarrow>=17",
]
web = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "jinja2>=3.1",
    "httpx>=0.28",
    "cryptography>=49",
    "pyyaml>=6",
    # Repeat scan/catalog dependencies required by the complete web app.
]
all = [
    # Union of scan, catalog, and web dependencies.
]
```

PEP 621 does not provide a portable way for one extra in the same project to
inherit another extra. Repeating the dependency union in `web` and `all` is
preferable to self-references or custom installer behavior. A test should keep
the unions synchronized.

## Import and composition design

### Before

```text
CLI import ----------------------------> all CLI implementations

web import ---> photometry ---> database ---> cryptography
           +--> exposure ------------------> astropy + numpy
           +--> FOV -----------------------> astroquery
           +--> catalog -------------------> pyarrow
           +--> transit/TTV fitting -------> yaml
```

### After

```text
                    +-------------------------+
                    | composition module      |
                    |                         |
CLI entry ----------| register core commands  |
web entry ----------| register web routes     |
                    | check capabilities once |
                    +------------+------------+
                                 |
                 +---------------+----------------+
                 |               |                |
               core          astronomy       secret storage
                               adapter            adapter
```

The composition module should own capability discovery and registration. That
provides locality: an optional dependency is declared, checked, and explained
in one module. Route and command implementations should not each invent their
own behavior.

Use a dedicated exception, for example `MissingCapabilityError`, carrying the
capability name and install hint. The CLI formats it for a terminal; web
startup formats it for logs. This is a real seam because the two presentation
adapters differ.

## Testing the installation interface

The interface is not modular until isolated installations are tested. Add a
small installation matrix in CI:

| Environment | Install | Required checks |
|---|---|---|
| core | `.[ ]` | import package; CLI help; CSV summary; SQLite build/query |
| scan | `.[scan]` | primary-HDU and extension-HDU FITS scans |
| catalog | `.[catalog]` | coordinate, catalog, exposure, and FOV tests |
| web | `.[web]` | app import; startup; every route registration; GUI smoke tests |
| all | `.[all]` | normal fast test suite |

Also add negative import-contract tests:

- core works when FastAPI, Astropy, Astroquery, PyArrow, and Cryptography are
  absent;
- requesting a missing capability returns the documented install hint;
- a missing scan dependency never turns an extension-header FITS file into a
  successful empty result;
- importing the web application never depends on undeclared transitive
  packages.

The existing slow tests remain separate because they validate external conda
engines and production data, not wheel composition.

## Migration sequence

1. Add import-contract tests for the intended core.
2. Declare PyYAML directly before rearranging other dependencies.
3. Move Cryptography behind the secret-storage seam.
4. Make the Astropy FITS fallback report a missing `scan` capability clearly.
5. Introduce the capability/composition module for CLI and web startup.
6. Move dependencies into `scan`, `catalog`, `web`, and `all` extras.
7. Build fresh environments for every matrix row and run their tests.
8. Update README installation commands and deployment instructions.
9. Keep production on `[all]` for one release, then consider `[web]` after the
   installation matrix has proven equivalent behavior.

Each step can be a small commit with the default production deployment kept
functional throughout.

## Decisions that require discussion

These choices affect the product interface and should not be implemented from
assumptions:

1. **What is core?** This review assumes the default installation includes the
   observation-log/SQLite CLI, not a Python-library-only package.
2. **How should incomplete web installations behave?** The choices are to fail
   startup, hide unavailable pages, or show disabled pages with readiness
   diagnostics. The first release recommendation is a complete `[web]` that
   avoids this ambiguity.
3. **Does raw-only FITS scanning have a supported use case?** If not, Astropy
   belongs in core or `scan` becomes a required operational extra rather than
   an optional behavior.
4. **Should encrypted user settings be available outside the web install?** If
   yes, use a separate `secrets` extra; otherwise keep it inside `web`.
5. **Is `catalog` a public install option or only an internal dependency group
   of `web`?** Publish it only if users run those calculations independently.

## Top recommendation

Start with a small core plus `[scan]`, `[web]`, and `[all]`, while production
continues to install `[all]`. The highest-value architectural change is the
composition module: extras reduce downloaded packages, but only explicit
import registration makes the modules genuinely independent and testable.
