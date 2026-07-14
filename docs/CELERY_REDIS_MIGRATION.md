# Celery + Redis Migration Procedure for MuSCAT-DB

## Goal

Migrate MuSCAT-DB background execution from in-process `subprocess.Popen` tracking to a Celery + Redis architecture that:

- preserves current photometry and transit-fit behavior
- preserves the `finalizing` live-log semantics already implemented in `src/muscat_db/jobs.py`
- supports single-host rollout first, then multi-server workers
- keeps the web UI, `jobs` table, and output directories consistent during and after migration

This is a migration plan, not a record that Celery or Redis is already deployed. The latest host-level observations and the proposed worker placement are maintained in [SERVER_INVENTORY.md](SERVER_INVENTORY.md). Re-verify that inventory immediately before installing services or enabling the Celery feature flag; host load, package availability, firewall rules, and NTP state are operational facts that can change.

This procedure is updated for the current codebase and host inventory as of 2026-07-13.

---

## Decisions (recorded 2026-07-13)

These four decisions were made by the maintainer and govern the rest of this plan. Where they conflict with earlier prose below, these win.

1. **Driver: multi-server scale.** The migration exists to spread photometry and transit-fit work across the multi-host cluster (ut3/ut6, later ut4/ut5/ut7 per [SERVER_INVENTORY.md](SERVER_INVENTORY.md)), not merely to harden the single host. This is what justifies Celery's added complexity. On a single host alone, Celery is mostly downside versus the existing `claim_slot()` serialization.

2. **Job DB: single-writer callback.** Workers on other hosts must **not** write `muscat.db` directly (SQLite file-locking over NFS is unreliable → corruption / lost updates on the ~3 GB DB). All job-state writes are routed through **one writer on ut2**. Remote workers report lifecycle transitions back to ut2, which is the sole process that mutates the `jobs` table. SQLite stays as the durable UI-facing store (moving `jobs` to Postgres/Redis remains a non-goal).

   - **Net-new code this implies (Phase 8, not Phase 1):** a `JobStateReporter` that POSTs state transitions to an internal ut2 endpoint, with:
     - authentication on that endpoint (LAN workers mutating the jobs table),
     - idempotent, `run_id`-keyed, retryable reports so a transient ut2 outage does not leave stuck `running` rows,
     - `finalizing` resolution owned by the **worker** (it holds the process and the log mtime); ut2 stops resolving lifecycle itself and just persists what the worker reports.
   - **Phase 1 defers all of the above:** on single-host ut2 the worker and DB are co-located, so the worker writes local SQLite exactly as today — no callback channel, no network, no auth. The callback layer is added only when workers move off-host.

3. **Supervision: systemd units per host.** Redis and every Celery worker run as systemd services with restart policies (not under the existing `muscatdbgui` tmux session, which supervises nothing and does not survive reboot). This requires root/sudo on each participating host. Worker units must pin `OMP_NUM_THREADS` / `MKL_NUM_THREADS` / `OPENBLAS_NUM_THREADS` to the host's real core budget — the ambient `OMP_NUM_THREADS=100` (see SERVER_INVENTORY.md risk #2) would otherwise oversubscribe ut2's 28 threads ~100×.

4. **Rollout: single-host on ut2 first.** Stand up Redis + workers on ut2 only, prove the `finalizing` / cancel / status contract across the web↔worker process boundary behind `MUSCAT_CELERY_ENABLED`, and only then expand to multi-host (Phase 8). This cleanly separates "prove the execution-boundary contract" from "add the network."

### Scope adjustment from these decisions

- **Drop the hash-based percentage ramp** (`MUSCAT_CELERY_RAMP_PERCENT`, Appendix A). That pattern targets high-volume web traffic needing statistical exposure; this workload is a handful of named jobs per day. A simple per-job or global on/off flag is easier to reason about and to cancel cleanly. Appendix A's `JobRouter` dual-path is retained (it is the local↔celery seam); only the percentage-ramp machinery is cut unless explicitly reinstated.

### Phase 1 definition of done (ut2 only)

- Redis + one `photometry` worker (concurrency 1) + one `transit_fit` worker (concurrency 1) as **systemd units on ut2**, with the OMP/MKL/OpenBLAS thread caps pinned in the unit.
- `claim_slot()` retired in favor of **queue concurrency = 1** (broker-native serialization replaces the SQLite slot gate).
- `finalizing` grace logic runs **inside the worker**; the DB still persists `running` until the log is quiescent.
- Cancel works for both queued (Celery revoke) and running (process-group SIGTERM) jobs.
- All gated behind `MUSCAT_CELERY_ENABLED`; the local `Popen` path is untouched when the flag is off.
- Its real payoff on a single host is narrow but worth proving: **jobs survive a web-process restart** because the worker is a separate systemd-supervised process, not a `Popen` child of the web app.

---

## Current State To Preserve

Before changing anything, keep these invariants in mind:

1. Photometry and transit-fit both already share the finalizing lifecycle logic in [`src/muscat_db/jobs.py`](../src/muscat_db/jobs.py).
2. The persistence seam already exists in [`src/muscat_db/job_store.py`](../src/muscat_db/job_store.py). That is the swap point for Celery/Redis.
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

- [`src/muscat_db/photometry.py`](../src/muscat_db/photometry.py)
- [`src/muscat_db/transit_fit.py`](../src/muscat_db/transit_fit.py)

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

Do not begin on the full multi-server topology.

First run Celery + Redis on the current server:

1. web process still on existing tmux-managed host
2. Redis local to host
3. Celery workers local to host

The current web process is managed in the `muscatdbgui` tmux session. Keep Redis and Celery under an explicit service/startup procedure as they are introduced; do not assume that the existing tmux session provides process supervision for them.

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
2. register the verified worker candidates (ut3, ut4, and ut6), keeping ut4 on trial/light queues until its post-restoration smoke test and stability window pass
3. pin queues by capability if needed
4. ensure shared file paths are identical or mounted compatibly on all worker hosts

This is critical because current outputs and logs are file-path dependent:

- photometry outputs
- transit-fit outputs
- raw data locations
- conda env paths

Do not move to multi-host workers until those paths are verified identical or abstracted.

**For concrete host specifications, recommended role assignment (redis broker, photometry workers, transit-fit workers), and operational prerequisite checklist, see [SERVER_INVENTORY.md](SERVER_INVENTORY.md).** That document includes live CPU/RAM/OS/NTP specs and risk mitigation for the multi-server rollout.

### Step 25. Add routing by capability

For multi-host deployment, define Celery routing rules such as:

- `photometry.full` -> hosts with `prose` and high I/O capacity
- `photometry.test` -> lighter worker pool
- `transit_fit.full` -> hosts with `timer` env
- `transit_fit.test` -> lighter worker pool

Queue naming should reflect capability, not just pipeline name, if the hardware split becomes important.

The initial host mapping should prefer ut6/ut3 for full production work, use the newly restored ut4 for low-concurrency test or overflow queues, and leave ut5/ut7 capacity-gated according to live load. This is an initial operational policy, not static routing: recheck host availability before worker startup.

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

---

## Appendix A: Job Router And Dual-Mode Design

> **Superseded in part by [Decisions](#decisions-recorded-2026-07-13):** the `JobRouter` local↔celery seam below is retained, but the **hash-based percentage ramp** (`MUSCAT_CELERY_RAMP_PERCENT`, `_choose_backend` sharding, and the "Gradual Ramp Strategy" section) is **cut** for this workload — use a simple global/per-job on/off flag instead. The ramp code is kept here only as reference in case it is ever reinstated.

To keep the current system running while validating Celery, implement a **job router** that dispatches to either `local` (multiprocessing) or `celery` backend based on configuration and a gradual ramp strategy.

### Router Concept

```python
# src/muscat_db/job_router.py

class JobRouter:
    """Routes jobs to local or Celery backend based on config and gradual ramp."""

    def dispatch_photometry(self, job_payload: dict, run_id: str) -> str:
        """
        Returns: dispatch_id (DB row key or Celery task_id)
        """
        backend = self._choose_backend(pipeline='photometry', run_id=run_id)
        if backend == 'local':
            return self._local_dispatch.dispatch_photometry(job_payload, run_id)
        else:
            return self._celery_dispatch.dispatch_photometry(job_payload, run_id)

    def dispatch_transit_fit(self, job_payload: dict, run_id: str) -> str:
        """
        Returns: dispatch_id
        """
        backend = self._choose_backend(pipeline='transit_fit', run_id=run_id)
        if backend == 'local':
            return self._local_dispatch.dispatch_transit_fit(job_payload, run_id)
        else:
            return self._celery_dispatch.dispatch_transit_fit(job_payload, run_id)

    def cancel(self, run_id: str) -> None:
        """Cancel a job, regardless of backend."""
        job = self._job_store.get_by_run_id(run_id)
        if job.backend == 'local':
            self._local_dispatch.cancel(run_id)
        else:
            self._celery_dispatch.cancel(job.dispatch_id)

    def get_status(self, run_id: str) -> dict:
        """Unified status, regardless of backend."""
        job = self._job_store.get_by_run_id(run_id)
        if job.backend == 'local':
            return self._local_status.get(run_id)
        else:
            return self._celery_status.get(job.dispatch_id)

    def _choose_backend(self, pipeline: str, run_id: str) -> str:
        """
        Determines which backend to use.

        Rules:
        - If MUSCAT_CELERY_ENABLED=0, always 'local'
        - If MUSCAT_CELERY_ENABLED=1:
          - Use hash(run_id) % 100 to pick percentage of jobs for Celery
          - Control percentage via MUSCAT_CELERY_RAMP_PERCENT (0-100)
          - Example: MUSCAT_CELERY_RAMP_PERCENT=10 -> 10% to Celery, 90% local
        """
        if not self._celery_enabled:
            return 'local'

        ramp_percent = int(os.getenv('MUSCAT_CELERY_RAMP_PERCENT', '0'))
        if ramp_percent == 0:
            return 'local'
        if ramp_percent >= 100:
            return 'celery'

        # Deterministic hash-based sharding
        hash_value = int(hashlib.md5(run_id.encode()).hexdigest(), 16)
        if (hash_value % 100) < ramp_percent:
            return 'celery'
        return 'local'
```

### Environment Variables

Add to the deployment configuration:

```bash
# Feature flag: enable Celery backend
MUSCAT_CELERY_ENABLED=0|1

# Gradual ramp: percentage of new jobs routed to Celery (0-100)
# Start at 0, gradually increase as validation succeeds
MUSCAT_CELERY_RAMP_PERCENT=0

# Existing Celery variables (from Phase 2)
MUSCAT_REDIS_URL=redis://localhost:6379/0
MUSCAT_CELERY_RESULT_URL=redis://localhost:6379/1
MUSCAT_CELERY_PHOT_QUEUE=photometry
MUSCAT_CELERY_FIT_QUEUE=transit_fit
```

### Database Backend Field

The `jobs` table already gains a `backend` column in Step 2:

```sql
ALTER TABLE jobs ADD COLUMN backend TEXT NOT NULL DEFAULT 'local';
```

This is the single source of truth for which system owns the job.

### Dispatch Backends

#### LocalDispatcher

Keep the existing behavior intact:

```python
# src/muscat_db/backends/local_dispatch.py

class LocalDispatcher:
    """Current multiprocessing backend."""

    def dispatch_photometry(self, job_payload: dict, run_id: str) -> str:
        # Write DB row as pending
        job_id = self._job_store.create_job(
            run_id=run_id,
            backend='local',
            state='pending',
            params=json.dumps(job_payload)
        )
        # Enqueue in local queue (existing `_JOBS` dict)
        photometry._JOBS[run_id] = {
            'payload': job_payload,
            'created_at': time.time()
        }
        return run_id  # use run_id as dispatch_id for local

    def cancel(self, run_id: str) -> None:
        # Mark DB as cancelled
        # sync_jobs() will clean up the local queue entry
        self._job_store.update_state(run_id, 'cancelled')
```

#### CeleryDispatcher

New Celery backend:

```python
# src/muscat_db/backends/celery_dispatch.py

class CeleryDispatcher:
    """Celery task backend."""

    def dispatch_photometry(self, job_payload: dict, run_id: str) -> str:
        # Write DB row as pending
        job_id = self._job_store.create_job(
            run_id=run_id,
            backend='celery',
            state='pending',
            params=json.dumps(job_payload)
        )

        # Dispatch Celery task
        task = tasks.run_photometry_task.apply_async(
            kwargs={'job_payload': job_payload, 'run_id': run_id},
            queue=self._phot_queue,
            task_id=run_id  # use run_id as Celery task_id for consistency
        )

        # Save dispatch_id to DB
        self._job_store.update_dispatch_id(run_id, task.id)
        return task.id

    def cancel(self, task_id: str) -> None:
        # Try to revoke before execution
        self._celery_app.control.revoke(task_id, terminate=False)
        # Worker will check DB state and terminate subprocess if needed
```

### Status Resolution

Implement unified status that works for both backends:

```python
# src/muscat_db/job_status.py

class UnifiedJobStatus:
    """Resolves status from DB, runtime, and logs regardless of backend."""

    def get(self, run_id: str) -> dict:
        job = self._job_store.get_by_run_id(run_id)

        if job.backend == 'local':
            return self._resolve_local(job)
        else:
            return self._resolve_celery(job)

    def _resolve_local(self, job: Job) -> dict:
        """Use existing sync_jobs() logic."""
        # Check if process is in _JOBS dict
        # Poll process, check log mtime, resolve state
        ...

    def _resolve_celery(self, job: Job) -> dict:
        """Check Celery task state + DB + logs."""
        task = self._celery_app.AsyncResult(job.dispatch_id)

        # Hierarchy: DB state if terminal, else Celery state, else pending
        if job.state in ('done', 'error', 'cancelled'):
            return {'state': job.state, 'return_code': job.return_code}

        if task.state == 'PENDING':
            return {'state': 'pending', 'backend': 'celery'}

        if task.state == 'STARTED':
            # Get worker info and log tail from runtime store
            runtime = self._runtime_store.get(run_id)
            return {
                'state': 'running',
                'worker_id': runtime.get('worker_id'),
                'pid': runtime.get('pid'),
                'log_tail': self._get_log_tail(job.log_file)
            }

        if task.state == 'RETRY':
            return {'state': 'running', 'backend': 'celery'}

        # Task failed or was revoked
        if task.state in ('FAILURE', 'REVOKED'):
            self._job_store.update_state(run_id, 'cancelled' if task.state == 'REVOKED' else 'error')
            return {'state': job.state, 'error': str(task.info)}

        return {'state': 'running', 'backend': 'celery'}
```

### Gradual Ramp Strategy

The hash-based sharding in `_choose_backend()` enables safe gradual rollout:

```python
# Day 1: Deploy code, start Celery infrastructure
MUSCAT_CELERY_ENABLED=1
MUSCAT_CELERY_RAMP_PERCENT=0  # No traffic yet

# Day 2: Monitor infrastructure, test manually
# (run test jobs via explicit Celery dispatch)

# Day 3: Start 5% gradual ramp
MUSCAT_CELERY_RAMP_PERCENT=5
# ~5 out of every 100 new jobs hash to Celery

# Day 7: If all metrics green, ramp to 25%
MUSCAT_CELERY_RAMP_PERCENT=25

# Day 14: If still stable, ramp to 50%
MUSCAT_CELERY_RAMP_PERCENT=50

# Day 21: If no issues, go 100%
MUSCAT_CELERY_RAMP_PERCENT=100
# Now all new jobs use Celery; old local jobs still respected

# Week 4+: Once old local jobs complete, remove LocalDispatcher code
```

### Monitoring And Metrics

Track during rollout:

```python
# src/muscat_db/metrics.py

class JobMetrics:
    def record_dispatch(self, backend: str, pipeline: str):
        """Increment dispatch counter."""
        self.gauges[f'{pipeline}.dispatch.{backend}'].inc()

    def record_completion(self, backend: str, pipeline: str,
                          runtime_secs: float, success: bool):
        """Record completion time and success/failure."""
        self.histograms[f'{pipeline}.runtime.{backend}'].observe(runtime_secs)
        self.gauges[f'{pipeline}.success.{backend}'].inc() if success \
            else self.gauges[f'{pipeline}.failure.{backend}'].inc()

    def get_summary(self) -> dict:
        """Return dispatch distribution and latency summary."""
        return {
            'photometry': {
                'local_jobs': self.gauges['photometry.dispatch.local'].get(),
                'celery_jobs': self.gauges['photometry.dispatch.celery'].get(),
                'local_avg_secs': self.histograms['photometry.runtime.local'].mean(),
                'celery_avg_secs': self.histograms['photometry.runtime.celery'].mean(),
                'local_success_rate': ...,
                'celery_success_rate': ...
            },
            'transit_fit': { ... }
        }
```

Add a metrics dashboard or endpoint that shows real-time dispatch distribution and latency comparison. This makes it obvious when Celery is ready to ramp.

### Rollback

If Celery exhibits issues, rollback is simple:

```bash
# Set ramp to 0
MUSCAT_CELERY_RAMP_PERCENT=0

# All new jobs route to local
# Existing Celery jobs continue via workers

# Once all Celery jobs finish, shut down workers
celery -A muscat_db.celery_app control shutdown
```

No code changes needed; existing DB row tracking ensures both backends remain functional.

### Testing The Dual-Mode Path

```python
# tests/test_dual_mode.py

@pytest.mark.parametrize('backend,ramp', [
    ('local', 0),
    ('celery', 100),
])
def test_dispatch_and_status_both_backends(backend, ramp, app, job_store):
    """Ensure router and status work for both backends."""
    os.environ['MUSCAT_CELERY_RAMP_PERCENT'] = str(ramp)

    router = JobRouter(celery_enabled=True, job_store=job_store)

    payload = {'pipeline': 'photometry', 'inst': 'muscat3', ...}
    run_id = 'test-run-1'

    dispatch_id = router.dispatch_photometry(payload, run_id)
    assert dispatch_id is not None

    job = job_store.get_by_run_id(run_id)
    assert job.backend == ('local' if ramp == 0 else 'celery')

    status = router.get_status(run_id)
    assert status['state'] == 'pending'

def test_cancel_works_on_both_backends(router):
    """Cancellation is transparent to backend."""
    # Dispatch to local
    dispatch_local = router.dispatch_photometry(payload, 'local-job')

    # Dispatch to Celery
    dispatch_celery = router.dispatch_photometry(payload, 'celery-job')

    # Both cancel
    router.cancel('local-job')
    router.cancel('celery-job')

    # Both show cancelled state
    assert router.get_status('local-job')['state'] == 'cancelled'
    assert router.get_status('celery-job')['state'] == 'cancelled'
```

This design lets you confidently run both backends in parallel, validate Celery under production traffic, and roll back instantly if needed.
