# Add LCO Scheduling And Download Page

## Summary

Add a new `/lco` page for LCO archive downloads and a complete observation
submission workflow (from ephemeris → IPP dry-run → live submission). The
feature is integrated with `ephemeris.html` via saved ephemeris view links and
isolates all LCO API logic in a small backend module so it can be split out
later.

References: LCO example repo `observation-portal-api-examples`, especially
MUSCAT requestgroup submission and proposal/requestgroup/IPP examples; LCO
archive frames endpoint `https://archive-api.lco.global/frames/`.

## Key Changes

- Add `src/muscat_db/templates/lco.html` and route `GET /lco`.
- Add nav link `LCO` in `base.html`.
- Add an ephemeris action link to `/lco?view=<slug>` after ephemeris view state
  is saved.
- Add env vars to `config.py` and `.env.example`:
  - `LCO_API_TOKEN`, secret, required for live LCO portal calls.
  - `MUSCAT_LCO_DIR`, optional download root.
  - `MUSCAT_LCO_ALLOW_SUBMIT`, safety gate for live submission.
- If `MUSCAT_LCO_DIR` is set, save downloads under
  `<MUSCAT_LCO_DIR>/<instrument>/<date>/`.
- If `MUSCAT_LCO_DIR` is unset, save downloads under `/data/<instrument>/<date>/`
  as requested.
- Do not delete or overwrite existing FITS files; if a filename exists, return a
  clear "already exists" status unless the user explicitly checks an overwrite
  option.

## Backend API

- Add an isolated `muscat_db/lco.py` helper module for:
  - token loading from `LCO_API_TOKEN`
  - authenticated Observation Portal requests
  - archive frame search
  - server-side frame download
  - requestgroup payload construction
- Add JSON endpoints in `web.py`:
  - `GET /api/lco/config`: reports whether token and download root are
    configured, without exposing secrets.
  - `GET /api/lco/proposals`: proxies
    `https://observe.lco.global/api/proposals/`.
  - `GET /api/lco/requestgroups?proposal=...`: proxies requestgroup status
    lookup.
  - `POST /api/lco/windows`: accepts ephemeris view slug, selected target/planet,
    date range, duration, and padding; returns candidate transit windows.
  - `POST /api/lco/ipp`: builds the requestgroup payload and calls
    `requestgroups/max_allowable_ipp/`.
  - `POST /api/lco/submit`: submits the same validated payload to
    `requestgroups/`. The UI disables the submit button until a successful
    dry-run IPP response exists for the current payload.
  - `GET /api/lco/archive/frames`: searches
    `https://archive-api.lco.global/frames/` with user-selected filters.
  - `POST /api/lco/archive/download`: downloads selected frame URLs to the
    configured server path and returns per-file results.
- Validate all user inputs server-side: proposal id, target name, RA/Dec, UTC
  date/time strings, exposure counts/times, reduction level, date range, frame
  ids, and download paths.

## Page Behavior

- `lco.html` has two main work areas:
  - **Schedule Observations**: load proposals, load ephemeris view from `?view=`,
    choose target/planet, generate batch transit windows across a UTC date range,
    configure imaging request, dry-run IPP, then submit.
  - **Download LCO Data**: filter archive frames by proposal, target,
    site/telescope/instrument, reduction level, date range, and result limit;
    display frames in a table; download selected files server-side.
- Submission form supports generic imaging v1:
  - MUSCAT mode with `2M0-SCICAM-MUSCAT`, g/r/i/z exposure times, sync/async
    exposure mode, and narrowband in/out positions.
  - Sinistro-style imaging with `1M0-SCICAM-SINISTRO`, filter, exposure time,
    and exposure count.
- Use LCO example payload structure:
  - `target`, `constraints`, `configurations`, `instrument_configs`, `windows`,
    `location`, `proposal`, `ipp_value`, `operator`, and `observation_type`.
- Require dry-run first:
  - user builds payload.
  - page calls max allowable IPP endpoint for a dry-run.
  - page shows payload and IPP response.
  - final submit button is enabled only after the dry-run succeeds and inputs
    have not changed. Live submission also requires the server to be configured
    with `MUSCAT_LCO_ALLOW_SUBMIT=1`.

## Test Plan

- Add backend unit tests for LCO payload construction for MUSCAT and Sinistro
  imaging.
- Add tests for missing `LCO_API_TOKEN`, invalid payloads, invalid paths,
  existing file handling, and archive download path resolution.
- Add FastAPI route tests with mocked LCO HTTP responses for proposals, IPP
  dry-run, submit, archive search, and download. Test that live submission is
  rejected when the `MUSCAT_LCO_ALLOW_SUBMIT` gate is not enabled.
- Add template/route smoke test that `/lco` renders and nav includes the page.
- Add ephemeris integration test that saved view URLs can produce
  `/lco?view=<slug>` links.
- Manually test browser flow with mocked or harmless API responses before using a
  real LCO token.

## Assumptions

- LCO token is server-side only via `LCO_API_TOKEN`; users will not paste or
  persist tokens in the browser.
- Downloads save files to server storage but do not automatically import them
  into muscat.db or launch photometry.
- Batch windows are generated from ephemeris `t0` and `period` over a
  user-selected UTC date range, with user-configurable pre/post padding.
- Date directory for downloaded LCO files uses the frame observation day
  converted to a compact date directory under the selected instrument path.
