# Security and Performance Audit — 2026-07-17

## Executive summary

This read-only audit identified three high-severity security issues and several
performance and resource-control weaknesses. The live deployment was confirmed
to bind uvicorn to `127.0.0.1:8001` behind nginx on `127.0.0.1:8000`, which
limits remote exposure. However, local users on the shared host can bypass nginx
authentication by connecting directly to uvicorn.

No source code, FITS files, or production database contents were modified during
the audit. A consistent daily SQLite backup was created at
`/raid_ut2/home/jerome/temp/muscat.db.backup-2026-07-17.sqlite` before this report
was written.

## Scope and method

The audit covered:

- authentication, authorization, and CSRF boundaries;
- request validation, HTML rendering, and browser-side injection risks;
- SQL, filesystem, subprocess, archive-download, and outbound-network paths;
- background-job lifecycle, concurrency controls, polling, and database access;
- resource limits for compute-heavy, download, catalog, and archive operations;
- the live process and listening-socket configuration;
- the default fast test suite and configured Ruff checks.

The audit did not run the heavyweight `@pytest.mark.slow` science-pipeline tests
or a third-party dependency vulnerability scanner. Findings below are based on
source tracing, targeted runtime measurements, and the existing test suite.

## High-severity findings

### H1. Backend API does not enforce authentication

The HTTP middleware derives `request.state.user` from nginx's forwarded header,
but it allows requests with no authenticated user to continue. Most API routes,
including job launch, cancellation, deletion, scanning, ingestion, archive
download, and target edits, do not require an authenticated user.

Evidence:

- [`web.py` authentication middleware](../src/muscat_db/web.py#L190) records the
  user but does not reject unauthenticated requests.
- [`photometry_run`](../src/muscat_db/web.py#L4204) launches work without an
  authentication check.
- [`transit_fit_run`](../src/muscat_db/web.py#L1774) has the same behavior.
- [`auth.py`](../src/muscat_db/auth.py#L11) explicitly notes that another local
  account can bypass nginx by connecting directly to uvicorn.
- Non-nginx `serve` and `restart` default to `0.0.0.0` in
  [`cli.py`](../src/muscat_db/cli.py#L604) and
  [`cli.py`](../src/muscat_db/cli.py#L622).

Impact:

- On the current shared host, any local account able to reach
  `127.0.0.1:8001` can invoke protected operations without nginx credentials.
- A deployment started without `--nginx` exposes the same unauthenticated API
  on all network interfaces by default.
- If live LCO submission is enabled, an unauthenticated direct request may reach
  operations backed by the server-wide LCO token.

Recommendation:

1. Add a fail-closed authentication dependency or middleware covering all
   protected pages and API routes.
2. Explicitly allowlist only genuinely public endpoints, such as static assets
   or a minimal health check.
3. Use a Unix-domain socket or a secret authenticated header between nginx and
   uvicorn so another local account cannot impersonate the proxy.
4. Change the non-nginx default bind address to `127.0.0.1`; require an explicit
   option for network exposure.

### H2. CSRF protection covers only two state-changing endpoints

The LCO-token and ADS-token settings endpoints enforce the existing same-origin
check, but the remaining POST, PUT, and DELETE endpoints do not. Affected
operations include job launch, cancellation and deletion, target-note edits,
archive downloads, scans, ingestion, and LCO submission.

Evidence:

- Protected token route:
  [`api_settings_lco_token`](../src/muscat_db/web.py#L2440).
- Unprotected examples:
  [`photometry_cancel`](../src/muscat_db/web.py#L4316),
  [`api_lco_submit`](../src/muscat_db/web.py#L2791), and
  [`api_set_note`](../src/muscat_db/web.py#L4344).
- [`auth.is_same_origin`](../src/muscat_db/auth.py#L65) correctly documents why
  HTTP Basic Auth and CORS preflight behavior are not sufficient defenses.

Impact:

An attacker-controlled site can cause a logged-in browser to send authenticated
state-changing requests. FastAPI can parse an attacker-controlled JSON body
declared as `text/plain`, allowing a browser "simple request" that avoids a CORS
preflight.

Recommendation:

1. Enforce Origin/Referer validation centrally for every unsafe HTTP method.
2. Add a CSRF token for defense in depth, especially for irreversible or
   externally consequential operations.
3. Add route-inventory tests that fail whenever a new unsafe endpoint lacks the
   shared authentication and CSRF dependencies.

### H3. Stored DOM XSS is possible on the Jobs page

The Jobs page constructs HTML strings containing job `key`, `type`, `inst`,
`date`, `target`, and `runId` values. Several values are inserted into HTML
attributes and inline JavaScript handlers without escaping.

Evidence:

- [`replaceActions`](../src/muscat_db/templates/jobs.html#L477) concatenates job
  fields into `onclick` handlers and `data-*` attributes.
- [`target_dir_name`](../src/muscat_db/jobs.py#L51) prevents path traversal but
  intentionally does not restrict quotes or JavaScript-significant characters.
- Job-start endpoints accept the target string and persist job metadata.

Impact:

A crafted stored job value can become executable JavaScript when another user
opens or polls the Jobs page. The missing backend authentication increases the
number of actors who can attempt to create such a job record.

Additional lower-confidence sinks exist where external ADS response fields and
error text are assigned to `innerHTML` in
[`target.html`](../src/muscat_db/templates/target.html#L737).

Recommendation:

1. Build buttons and cells with DOM APIs, `textContent`, `dataset`, and
   `addEventListener` instead of HTML-string concatenation and inline handlers.
2. Do not treat path-segment validation as HTML or JavaScript escaping.
3. Replace ADS `innerHTML` assignments with fixed DOM elements and
   `textContent`.
4. Add browser-level tests using quotes, angle brackets, and event-handler
   payloads in every rendered job field.

## Performance and resource-control findings

### P1. Calibration requests create unbounded daemon threads

Every call to [`exposure_calibrate`](../src/muscat_db/web.py#L2041) starts a new
daemon thread. There is no per-instrument deduplication, queue, concurrency cap,
or tracked job state.

Repeated calls can execute several expensive calibrations against the same
instrument simultaneously, increasing CPU, memory, I/O, and database pressure.
Because of H1, this is also an unauthenticated resource-exhaustion path.

Recommendation: move calibration into the shared job system, permit at most one
active calibration per instrument, apply a global concurrency limit, and expose
tracked status and cancellation.

### P2. External catalog and archive batches are unbounded

[`exposure_lookup_mags_batch`](../src/muscat_db/web.py#L2084) accepts an
unbounded star list and creates up to eight external-call workers per request.
Multiple concurrent requests multiply that fan-out.

[`api_lco_archive_download`](../src/muscat_db/web.py#L3028) similarly accepts an
unbounded frame list. Foreground mode processes the entire list in the request
thread. Background mode copies and retains the full frame and result dictionaries
in process memory; see [`start_archive_download`](../src/muscat_db/lco.py#L992).

Recommendation:

- define hard per-request item and serialized-byte limits;
- apply per-user and global queue quotas;
- reject foreground archive downloads beyond a very small threshold;
- persist compact background job metadata rather than retaining full payloads;
- bound external-call concurrency globally, not once per request.

### P3. Redirects can bypass the archive-download URL allowlist

[`_validate_download_url`](../src/muscat_db/lco.py#L660) validates only the
initial URL. [`urllib.request.urlopen`](../src/muscat_db/lco.py#L698) follows HTTP
redirects without applying that validation to each destination.

An allowed LCO or S3 URL that redirects elsewhere could reach an unapproved
host, including internal services, while the response is written into the data
tree.

Recommendation:

1. Use a redirect handler that validates every hop.
2. Require HTTPS after every redirect.
3. Resolve and reject loopback, private, link-local, and otherwise internal IP
   addresses.
4. Prefer an exact set of known LCO archive and bucket hostnames over the broad
   `*.amazonaws.com` suffix.

### P4. ZIP generation is synchronous and has no resource budget

[`_create_zip_response`](../src/muscat_db/web.py#L1872) recompresses all selected
files into a temporary archive before returning the response. There is no
uncompressed-size limit, free-space check, concurrency limit, or reusable archive
cache.

Concurrent download requests can consume substantial CPU and fill
`MUSCAT_TMPDIR`. Client disconnects during generation do not stop the work.

Recommendation: generate large archives as bounded background jobs or stream
them, enforce input-size and free-space budgets, and cache completed archives by
an output-manifest fingerprint for a short period.

### P5. The "lightweight" status endpoint performs full reconciliation

The global progress indicator polls every four seconds while a browser has a
tracked job. [`jobs_status`](../src/muscat_db/web.py#L3959) invokes
`phot.sync_jobs()`, `fit.sync_jobs()`, and `ttv.sync_jobs()` before checking
`active_only`.

At audit time the database contained 302 jobs:

- 210 photometry jobs, including 166 completed jobs;
- 80 transit-fit jobs;
- 7 TTV-fit jobs;
- 5 completed LCO archive jobs.

[`photometry.sync_jobs`](../src/muscat_db/photometry.py#L1923) loads all jobs and
checks every completed photometry job log for partial-failure markers on each
call. Thus an endpoint described as lightweight scales with historical jobs and
filesystem operations.

Recommendation:

1. Make `active_only` a direct read-only query over active states.
2. Reconcile process state on a controlled server-side cadence rather than per
   browser poll.
3. Check terminal logs once during the terminal transition and persist the
   result.
4. Add an index supporting active-job queries if the durable job history is
   expected to grow substantially.

### P6. Schema DDL remains in a read hot path

[`get_targets`](../src/muscat_db/database.py#L1019) runs the complete
`CREATE ... IF NOT EXISTS` schema script on every call before selecting the
materialized target rows.

Measurements against the 2.9 GB production database:

- `get_targets()`: 0.084 seconds for 2,301 rows;
- cold index render: 0.618 seconds and 4.07 MB of HTML;
- cached index render: 0.005 seconds.

The existing rendered-page cache is effective, but schema checks should not be
part of a read path and can introduce avoidable schema locking under load.

Recommendation: perform schema creation and migrations once at startup or build
time, using the same per-database migration guard already used for job-table
migrations.

## Existing strengths

The audit found several strong safeguards that should be retained:

- SQL values are generally parameterized.
- Subprocesses use argument arrays rather than `shell=True`.
- File-serving and output paths use containment checks and extension allowlists.
- Per-user API tokens are encrypted at rest, and local secrets are excluded
  from Git.
- Photometry concurrency slots are persisted across processes.
- The companion proxy requires authentication and validates WebSocket origins.
- Archive downloads are written atomically through sibling `.part` files.
- Job-finalization logic accounts for detached multiprocessing workers and log
  quiescence.

## Validation results

- Fast suite: **735 passed, 1 skipped, 9 slow tests deselected, 5 subtests
  passed** in 107 seconds.
- Ruff: **all checks passed**.
- Live service: uvicorn confirmed on `127.0.0.1:8001`, nginx on
  `127.0.0.1:8000`.
- Active muscat-db processes observed during the audit: reload supervisor,
  multiprocessing resource tracker, and one worker. No additional server or
  heavyweight science pipeline was launched.

## Recommended remediation order

1. Enforce backend authentication and remove the local nginx-bypass boundary.
2. Apply centralized CSRF protection to all unsafe methods.
3. Remove Jobs-page inline HTML/JavaScript construction using untrusted values.
4. Put calibration, archive downloads, catalog fan-out, and ZIP creation behind
   bounded queues and resource budgets.
5. Revalidate every archive-download redirect destination.
6. Separate lightweight status reads from lifecycle reconciliation.
7. Move schema creation and migration out of target-read paths.
