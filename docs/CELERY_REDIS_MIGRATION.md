# Celery + Redis Migration Procedure for MuSCAT-DB

## Goal

Migrate MuSCAT-DB background execution from in-process `subprocess.Popen` tracking to a Celery + Redis architecture that:

- preserves current photometry and transit-fit behavior
- preserves the `finalizing` live-log semantics already implemented in `src/muscat_db/jobs.py`
- supports single-host rollout first, then multi-server workers
- keeps the web UI, `jobs` table, and output directories consistent during and after migration

This procedure is written for the current codebase as of 2026-07-01.

---

## Current State To Preserve

Before changing anything, keep these invariants in mind:

1. Photometry and transit-fit both already share the finalizing lifecycle logic in [`src/muscat_db/jobs.py`](/raid_ut2/home/jerome/github/research/project/muscat-db/src/muscat_db/jobs.py:1).
2. The persistence seam already exists in [`src/muscat_db/job_store.py`](/raid_ut2/home/jerome/github/research/project/muscat-db/src/muscat_db/job_store.py:1). That is the swap point for Celery/Redis.
3. The web UI expects job states like `pending`, `running`, `cancelling`, `finalizing`, `done`, `error`, `cancelled`.
4. `finalizing` is live-view-only. The DB should still persist `running` until the log is quiescent.
5. Photometry must continue running through the `prose` conda environment and transit-fit through the `timer` environment.
6. Output directories and file layout must not change in this migration.
7. Do not move photometry science logic into `muscat-db`; the external pipeline ownership stays in `prose2`.

---

## Target Architecture

The target architecture should be:

1. FastAPI web app:
   - validates requests
   - writes durable job metadata via `JobRepository`
   - enqueues Celery tasks instead of launching local subprocesses
   - reads live/durable status through a unified status service

2. Redis:
   - Celery broker
   - Celery result backend
   - optional lightweight distributed coordination primitives if needed

3. Celery worker processes:
   - execute photometry and transit-fit launch tasks
   - own subprocess spawning and monitoring
   - update job metadata in the DB during lifecycle transitions

4. SQLite `jobs` table:
   - remains the UI-facing durable source of truth during the migration
   - stores job metadata, state, return code, elapsed, params, run identifiers

5. File system:
   - remains the source of logs and science outputs
   - live log tailing still reads the same log files the pipelines append to

Do not make Celery results the primary UI state source. Use Redis/Celery for orchestration and worker execution, but keep the `jobs` table as the user-visible durable state until a later full repository redesign.

---

## Phase 1: Introduce Explicit Job Backend Abstractions

### Step 1. Add a queue backend interface separate from the current DB store

Create a new module, for example `src/muscat_db/job_backend.py`, with:

- `JobDispatcher`: enqueue/cancel/inspect remote work
- `JobRuntimeStore`: live worker metadata keyed by canonical job key
- `JobControl`: revoke/terminate worker execution

Keep `JobRepository` in `job_store.py` as the durable DB layer.

Required methods:

- `dispatch_photometry(job_payload) -> dispatch_id`
- `dispatch_transit_fit(job_payload) -> dispatch_id`
- `revoke(job_key, terminate: bool) -> None`
- `runtime_status(job_key) -> dict | None`
- `register_runtime(job_key, runtime_info) -> None`
- `clear_runtime(job_key) -> None`

`runtime_status` must be able to return:

- Celery task id
- worker hostname
- subprocess pid
- subprocess pgid if available
- last heartbeat timestamp
- current live state

### Step 2. Extend the `jobs` table schema

Add columns to the existing `jobs` table:

- `dispatch_id TEXT NOT NULL DEFAULT ''`
- `worker_id TEXT NOT NULL DEFAULT ''`
- `pid INTEGER`
- `heartbeat_at REAL`
- `backend TEXT NOT NULL DEFAULT 'local'`

Rules:

- existing rows must remain readable without migration breakage
- old rows default to `backend='local'`
- new Celery rows use `backend='celery'`

Do not remove existing fields.

### Step 3. Persist task payloads explicitly

Current queued jobs overload `params` with JSON. Keep that, but standardize it.

Define one canonical JSON payload shape for both pipelines:

```json
{
  "pipeline": "photometry|transit_fit",
  "inst": "muscat3",
  "date": "260101",
  "target": "WASP-12b",
  "run_id": "coj-default",
  "run_name": "default",
  "run_type": "full|test",
  "site": "",
  "mode": "",
  "options": {},
  "selected_csvs": []
}
```

Use this exact payload both in DB persistence and Celery dispatch.

---

## Phase 2: Add Celery + Redis Infrastructure

### Step 4. Add dependencies

Add to the project dependencies in `pyproject.toml`:

- `celery`
- `redis`

Optional but useful:

- `flower`

Do not add alternative task frameworks in the same change.

### Step 5. Add configuration

Create a new module `src/muscat_db/celery_app.py` that builds the Celery app from environment variables.

Add env vars:

- `MUSCAT_CELERY_ENABLED=0|1`
- `MUSCAT_REDIS_URL=redis://host:6379/0`
- `MUSCAT_CELERY_RESULT_URL=redis://host:6379/1`
- `MUSCAT_CELERY_DEFAULT_QUEUE=muscatdb`
- `MUSCAT_CELERY_PHOT_QUEUE=photometry`
- `MUSCAT_CELERY_FIT_QUEUE=transit_fit`
- `MUSCAT_CELERY_SOFT_TIME_LIMIT_S`
- `MUSCAT_CELERY_HARD_TIME_LIMIT_S`

Celery defaults:

- `task_track_started = True`
- `worker_prefetch_multiplier = 1`
- `task_acks_late = True`
- `task_reject_on_worker_lost = True`

Do not enable autoscaling or retries yet.

### Step 6. Add local development compose/service docs

Document a single-node local stack:

1. Redis service
2. FastAPI server
3. one photometry worker
4. one transit-fit worker

Even if you do not add Docker Compose immediately, document the equivalent commands in `README.md` or a dedicated ops doc.

Recommended worker split:

- queue `photometry`
- queue `transit_fit`

This prevents one pipeline type from starving the other.

---

## Phase 3: Move Execution Into Celery Tasks

### Step 7. Create Celery task entrypoints

Create `src/muscat_db/tasks.py` with two primary tasks:

- `run_photometry_task(job_payload: dict)`
- `run_transit_fit_task(job_payload: dict)`

Each task must:

1. validate payload shape
2. write `dispatch_id`, `worker_id`, `backend='celery'`, and heartbeat metadata to the DB row
3. spawn the external subprocess using the same command builders already used today
4. write logs to the same log files currently used by the web app
5. monitor the subprocess until terminal or `finalizing`
6. persist terminal state through `JobRepository`
7. clear runtime metadata on completion

Do not call route handlers from tasks. Call extracted pipeline helpers directly.

### Step 8. Extract launch/monitor helpers out of `sync_jobs()`

Right now the launch logic is embedded in:

- [`src/muscat_db/photometry.py`](/raid_ut2/home/jerome/github/research/project/muscat-db/src/muscat_db/photometry.py:1706)
- [`src/muscat_db/transit_fit.py`](/raid_ut2/home/jerome/github/research/project/muscat-db/src/muscat_db/transit_fit.py:1910)

Refactor each pipeline module to expose:

- `prepare_*_launch(payload) -> LaunchSpec`
- `spawn_*_process(spec) -> PipelineJob`
- `monitor_*_process(job, store, runtime_store) -> None`

`sync_jobs()` should stop being responsible for launching queued work. That responsibility moves to Celery workers.

### Step 9. Preserve `finalizing` semantics inside the worker

The Celery worker must reuse:

- `jobs.resolve_job_state`
- pipeline-specific `FinalizeConfig`

Implementation rule:

1. while process is running:
   - DB state = `running`
   - runtime state = `running` or `cancelling`
2. after parent exits but log is not quiescent:
   - DB state remains `running`
   - runtime state reports `finalizing`
3. once log is quiescent:
   - DB state becomes `done|error|cancelled`
   - runtime entry is cleared

This is required so the photometry/transit-fit pages keep streaming the trailing worker output.

### Step 10. Keep subprocess spawning model unchanged initially

Continue using `subprocess.Popen(..., start_new_session=True)` inside the Celery worker in the first migration.

Do not try to replace the science subprocesses with native Celery tasks. The Celery worker should orchestrate the existing external commands, not reimplement them.

---

## Phase 4: Replace In-Process Queue Drain And Registries

### Step 11. Stop using `_JOBS` and `_FIT_JOBS` as authoritative state

Current modules still keep:

- `photometry._JOBS`
- `transit_fit._FIT_JOBS`

Replace them with:

- DB durable row
- runtime metadata from Redis/Celery

Allowed transitional state:

- keep `_JOBS` / `_FIT_JOBS` as in-worker local objects only during the task lifetime
- do not let the web process depend on them anymore

The web process must not assume the launched process lives inside the same Python interpreter.

### Step 12. Change `start_run()` and `start_fit()` to dispatch tasks

Update:

- photometry start route path
- transit-fit start route path

Behavior:

1. validate inputs
2. compute canonical `run_id`
3. write/update DB row as `pending`
4. call Celery task `apply_async(...)`
5. save `dispatch_id`
6. return `queued: true` or `submitted: true`

Do not spawn local subprocesses in the web process when `MUSCAT_CELERY_ENABLED=1`.

Retain the old local path behind a feature flag until rollout is complete.

### Step 13. Replace `sync_jobs()` responsibilities

After Celery dispatch exists, narrow `sync_jobs()` to:

- reconcile stale DB rows
- mark `Process lost` only for legacy local backend rows
- optionally refresh orphan disk-discovered outputs

Remove from `sync_jobs()`:

- queue draining
- pending-job promotion
- subprocess launch
- local concurrency slot arbitration

### Step 14. Replace `_MAX_FULL_JOBS=1` with queue- and worker-level control

Current full-job serialization is process-local.

Replace it with:

1. dedicated Celery queues
2. worker concurrency settings
3. optional distributed lock for the photometry full queue if you still want global serialization

Recommended first rollout:

- photometry full reductions: one Celery worker process with concurrency `1`
- transit-fit full reductions: one Celery worker process with concurrency `1`
- test runs: either same queue or separate lightweight queue if needed later

Do not implement cluster-wide Redis locks until the queue split alone proves insufficient.

---

## Phase 5: Status, Cancellation, And Live Logs

### Step 15. Build a unified status resolver

Create a single status service, for example `src/muscat_db/job_status.py`, used by both web routes.

Resolution order:

1. durable DB row
2. runtime metadata from Celery/Redis
3. log file + output directory fallback

Rules:

- if DB says `pending` and runtime not started: return `pending`
- if runtime says `STARTED` and log active: return `running`
- if runtime says subprocess exited but log active: return `finalizing`
- if DB row terminal: return terminal state

The UI contract must remain unchanged.

### Step 16. Implement cancellation through Celery revoke + process-group terminate

Cancellation must work for both:

- queued-but-not-started tasks
- already-running subprocesses

Required flow:

1. mark DB row as `cancelled`
2. revoke Celery task if not yet running
3. if running, look up stored `pid` / `pgid`
4. send SIGTERM to the subprocess group
5. escalate after grace period using the same `kill_after` semantics

Do not rely on Celery revoke alone for running subprocesses.

### Step 17. Keep live logs file-based

Do not move logs into Redis.

Continue:

- writing the pipeline logs to output directories
- reading tails from the same files in status routes

Add only minimal runtime metadata to Redis:

- current task id
- pid/pgid
- heartbeat
- worker hostname
- current transient state

This avoids changing the current user-facing debugging model.

---

## Phase 6: Rollout Strategy

### Step 18. Add a feature flag and dual path

Gate dispatch mode with:

- `MUSCAT_CELERY_ENABLED=0` -> current local mode
- `MUSCAT_CELERY_ENABLED=1` -> Celery mode

During rollout:

1. deploy code with flag off
2. start Redis and Celery workers
3. enable the flag in staging or one controlled server
4. validate one photometry test run, one photometry full run, one transit-fit test run, one transit-fit full run
5. only then enable for regular traffic

### Step 19. Use single-host production shadow rollout first

Do not begin on the planned 48/120/120-core multi-server topology.

First run Celery + Redis on the current server:

1. web process still on existing tmux-managed host
2. Redis local to host
3. Celery workers local to host

This isolates orchestration changes from multi-host operational changes.

### Step 20. Add operational commands

Add CLI helpers or documented commands for:

1. start Redis
2. start photometry worker
3. start transit-fit worker
4. inspect active Celery tasks
5. revoke a task by `dispatch_id`
6. restart workers safely

At minimum document:

```bash
celery -A muscat_db.celery_app:celery_app worker -Q photometry --concurrency=1
celery -A muscat_db.celery_app:celery_app worker -Q transit_fit --concurrency=1
```

Adjust actual module path to match implementation.

---

## Phase 7: Testing And Acceptance

### Step 21. Add unit tests

Add tests for:

- Celery-enabled dispatch path writes `pending` row and `dispatch_id`
- status resolver maps runtime + DB + log states correctly
- cancellation revokes queued tasks and terminates running subprocesses
- `finalizing` stays non-terminal until log quiescence
- legacy local mode still works when the feature flag is off

### Step 22. Add integration tests

Use a real Redis instance in integration tests if available, otherwise mark them separately.

Minimum integration cases:

1. photometry test run in Celery mode
2. transit-fit test run in Celery mode
3. queued full run transitions `pending -> running -> finalizing -> done`
4. cancel queued job
5. cancel running job
6. worker crash leaves recoverable DB state

### Step 23. Add restart recovery tests

Validate:

1. web server restarts do not lose status of active Celery-dispatched jobs
2. worker restarts leave jobs in a detectable state
3. stale `running` rows can be reconciled on startup or explicit sync

For Celery backend rows, `Process lost (server restart)` must no longer be emitted by the web server just because the web process restarted.

---

## Phase 8: Multi-Server Expansion

### Step 24. Only after single-host success, move workers to separate servers

When the single-host Celery rollout is stable:

1. move Redis to a reachable central service
2. register workers on the 48/120/120-core servers
3. pin queues by capability if needed
4. ensure shared file paths are identical or mounted compatibly on all worker hosts

This is critical because current outputs and logs are file-path dependent:

- photometry outputs
- transit-fit outputs
- raw data locations
- conda env paths

Do not move to multi-host workers until those paths are verified identical or abstracted.

### Step 25. Add routing by capability

For multi-host deployment, define Celery routing rules such as:

- `photometry.full` -> hosts with `prose` and high I/O capacity
- `photometry.test` -> lighter worker pool
- `transit_fit.full` -> hosts with `timer` env
- `transit_fit.test` -> lighter worker pool

Queue naming should reflect capability, not just pipeline name, if the hardware split becomes important.

---

## Implementation Order Summary

Execute in this order:

1. add schema columns and backend abstractions
2. add Celery app and Redis config
3. extract pipeline launch/monitor helpers
4. implement Celery tasks that spawn existing subprocesses
5. switch start routes to dispatch tasks behind `MUSCAT_CELERY_ENABLED`
6. build unified status/cancel path using DB + runtime metadata + log files
7. remove queue-drain logic from `sync_jobs()`
8. test single-host rollout
9. cut over production on one host
10. only then expand to multi-server workers

---

## Non-Goals For This Migration

Do not combine this migration with:

- rewriting the web UI
- moving logs to Redis
- changing output directory layout
- replacing SQLite with another database
- moving photometry science functions into this repo
- introducing Celery beat, retries, or autoscaling in the first cut
- changing Jinja routes or API response contracts unless strictly required

---

## Exit Criteria

The migration is complete when all of the following are true:

1. `MUSCAT_CELERY_ENABLED=1` runs both pipelines without local `Popen` in the web process.
2. Live log streaming still shows `finalizing` correctly.
3. The Jobs page stays consistent with DB-backed state.
4. Web-server restart no longer causes active jobs to appear lost.
5. Cancel works for queued and running jobs.
6. Fast tests pass, and Celery integration tests pass.
7. Single-host production use is stable before multi-server expansion.

