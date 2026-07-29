# Static GitHub Pages documentation snapshot

**Status:** implemented · **Backlog item:** `docs/TODO.md` — "add a static but
navigable github-pages version as visual muscat-db documentation"

The project's documentation, published as a GitHub Page: the pipeline guide as
the landing page, plus a browsable static snapshot of the muscat-db web UI, so
anyone can read how the pipeline works and see what the tool looks like without
running the FastAPI server, the 3 GB `muscat.db`, or the conda photometry/transit
stack. The UI pages document *what the UI looks like*, not a live instance.

## Design decisions

- **The guide is the landing page.** `/guide` is written to the site root and the
  app's own landing page moves to `targets/`. A visitor
  arriving at the Pages URL is reading documentation, not looking at a table of
  observation targets they cannot query. Every internal link is rewritten through
  the route map and every page's `../` prefix is derived from its output
  directory, so both moves propagate without any hand-maintained paths.
- **Real host snapshot.** The build runs where `muscat.db`, the `data/` CSVs, and
  the `~/ql/*` figure trees live (CI runners have none of these), so pages carry
  real data and real figures.
- **Representative subset, not a full mirror.** Every navigation page plus a few
  example detail / drill-down pages (chosen from what actually has data and
  figures on disk). Keeps the published site small.
- **Figures + shells with a banner.** Referenced photometry / transit-fit figure
  PNGs/GIFs are copied so those pages show real plots; live-API pages (ephemeris,
  fov, exposure, lco) render as static shells. Every UI page carries a banner:
  *"Static documentation snapshot — live data & actions are disabled."* The
  landing guide does not, since it documents the pipeline rather than showing
  live data, so the caveat answers a question that page never raises.

## How it works

The builder (`src/muscat_db/static_site.py`) drives the real FastAPI app
(`muscat_db.web:app`) through Starlette's `TestClient` — the same object the test
suite uses — so every page is produced by the real route handlers reading a real
DB. It then rewrites the captured HTML into a self-contained, relatively-linked
static tree.

1. **Enumerate a representative URL set** — no-param nav pages, one
   `/{inst}` + newest `/{inst}/{date}` + first `ccd` per instrument with data,
   plus example `/target`, `/photometry`, and `/transit-fit` pages chosen from
   what has products on disk (reusing prose's `output_dates` /
   `discovered_targets` / `list_outputs` and a walk of `$MUSCAT_TIMER_DIR`).
2. **Capture** each URL and write it as `…/index.html` mirroring the URL.
3. **Copy assets** — the bundled `static/` dir, and every referenced
   `/api/(photometry|transit-fit)/file/…` figure, fetched through the same
   `TestClient` (no path-resolution duplication) and written under `assets/`.
   Missing files are skipped, never fatal.
4. **Rewrite links** with a depth-relative prefix (`../` per URL segment) so the
   result works under a project site, user site, or custom domain with no
   hard-coded base path: strip `static_url` cache-busters, point figure `src`s at
   the local copies, relativize internal nav links (parametric parents resolve to
   the populated example when one exists), and inject the snapshot banner.
5. **Finish** — write `.nojekyll` (required so Pages does not run Jekyll).

### Privacy

Because it is a real-data snapshot, `--scrub-notes` (default **on**) blanks
user-authored target notes and job usernames at the data layer (wrapping
`_get_targets`, `_get_datasets_for_normalized_target`,
`_jobs_with_lco_archive_rows`) before capture, so private text never reaches the
published HTML — including the `data-note` / `data-search` attributes. It also
replaces user-specific host home-directory prefixes in rendered commands and
file paths with `~`. **Review `site/` locally before the first commit.** The
settings page shows token *status* only, never secrets.

## Rebuilding the snapshot (on the host)

```bash
uv run muscat-db build-static-site --out site
#   --db PATH            SQLite database (default muscat.db)
#   --scrub-notes/--keep-notes   blank private notes/usernames (default: scrub)
#   --base-path PREFIX   force root-absolute links (default: depth-relative)
#   --examples N         max example detail pages per parametric route (default 2)
#   --figures/--no-figures       copy referenced figures (default: copy)
```

Preview exactly as Pages serves it:

```bash
cd site && python -m http.server 8080   # browse http://localhost:8080/
```

## Deployment

The static documentation site is built and deployed automatically in GitHub Actions on pushes to `main` and `test` via `.github/workflows/pages.yml`.

In CI, the build runs using a synthetic mock database to generate a lightweight, self-contained documentation snapshot and deploys directly to GitHub Pages via `actions/upload-pages-artifact` and `actions/deploy-pages`.

**One-time setup:** in the repo settings, set Pages → Source → *GitHub Actions*.

## Accepted limitations

- Live-API interactivity (running jobs, LCO submit, ephemeris compute, FOV
  optimize) is non-functional by design — shells + banner.
- Detail pages outside the representative subset are inert links (resolve within
  the site tree, may 404 locally) rather than fully navigable.
- The CI site snapshot runs against a synthetic database and contains representative sample targets.

## Key files

- `src/muscat_db/static_site.py` — the builder.
- `src/muscat_db/cli.py` — `build-static-site` command.
- `.github/workflows/pages.yml` — the GitHub Pages deploy workflow (runs on `main`/`test`).
- `tests/test_static_site.py` — build test suite and synthetic database helper.
