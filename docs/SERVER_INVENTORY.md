# Server Inventory & Celery/Redis Topology

**Probed live from the NIS gateway (`muscat-ut2`) on 2026-07-13; `muscat-ut4` was re-probed after returning online.** This is a living, point-in-time reference for the multi-host Celery + Redis rollout planned in [CELERY_REDIS_MIGRATION.md](CELERY_REDIS_MIGRATION.md) (Phase 8: Multi-Server Expansion). It is not a service deployment record: Redis and Celery are not enabled in the application yet, and all host/load/network facts must be rechecked before rollout.

---

## Cluster Inventory

Hosts derive from `/etc/hosts` on the gateway. Two subnets: `157.82.46.x` (West) and `157.82.29.x` (East).

| Host | IP | CPU Model | Phys / Log cores | RAM | OS / Python | Load @ probe | Status |
|------|----|-----------|-----------------|----|-------------|--------------|--------|
| **muscat-ut2** | 157.82.46.83 | Xeon Gold 5120 @2.2GHz (1 socket) | 14 / 28 | 44 GiB (+119 GiB swap) | Ubuntu 26.04 / 3.12.8 | low | **up** — NIS gateway, web GUI (tmux `muscatdb-gui`), NFS server, NTP |
| **muscat-ut3** | 157.82.46.17 | 2× EPYC 7542 32C | 64 / 128 | 125 GiB | Ubuntu 22.04.5 / 3.10.12 | 2.4 (idle-ish) | **up** — `/raid_ut3` exported |
| **muscat-ut4** | 157.82.46.41 | Core i9-10940X @3.3GHz (1 socket) | 14 / 28 | 125 GiB (+127 GiB swap) | Ubuntu 22.04.5 / 3.10.12 | 0.12 | **up** — `/ut2` NFS, science envs, repo, DB, and NTP verified |
| **muscat-ut5** | 157.82.29.73 | 2× Xeon Gold 6338 32C @2.0GHz | 64 / 128 | 251 GiB | Ubuntu 22.04.5 / 3.10.12 | **~215 (saturated)** | **up but heavily loaded** — `/raid_ut5` exported |
| **muscat-ut6** | 157.82.29.74 | 2× Xeon Gold 6338 32C @2.0GHz | 64 / 128 | 251 GiB | Ubuntu 22.04.5 / 3.10.12 | 0.15 (idle) | **up, most available** |
| **muscat-ut7** | 157.82.46.82 | EPYC 9555 64C (1 socket, no SMT) | 64 / 64 | 246 GiB | Ubuntu 26.04 / 3.14.4 | **~225 (saturated)** | **up but heavily loaded** |

**Note on core counts:**
- ut2 (gateway) = 28 logical threads.
- ut3 is a bonus 128-logical host.
- ut4 = 14 physical / 28 logical threads with 125 GiB RAM; it is suitable for trial, light, or overflow work after a stability check.
- ut5 & ut6 (the "120-core" workhorses) = 128 logical threads each.
- ut7 = 64 physical cores (no hyper-threading).

---

## Software State

### Available on all hosts
- Python is available everywhere; versions vary by OS (see table above).
- The initial cluster probe found no installed `redis-server`, `redis-py`, or `celery` package on any host; the ut4 re-probe confirmed that state there. These must be installed and tested before rollout.

### Present on ut2 only
- **`uv` package manager** — used by the web app (`uv run`).
- On workers, either install `uv` per host or use the appropriate shared conda environment to run worker entrypoints instead.

### Required before rollout
- Install Redis on the broker host and the Python `redis`/Celery dependencies in the selected worker environment(s).
- Verify clock sync (NTP) across all participating hosts before Celery scheduling.

---

## Shared Filesystem

The **linchpin assumption** for multi-host workers: `/ut2` and everything under it resolves identically on every host.

### How it works

- On **ut2**: `/ut2` is a symlink to `/raid_ut2/home`. The `/raid_ut2` volume is a local ext4 filesystem on ut2.
- ut2 exports `/raid_ut2` via NFSv4 to all `muscat-ut*` hosts with `rw,sync,no_subtree_check,no_root_squash`.
- Worker hosts reach it via autofs (`/etc/auto.ut2`) which mounts `muscat-ut2:/raid_ut2` at `/mnt_ut2/raid_ut2`.
- The symlink `/ut2 → /raid_ut2/home` resolves to the same physical files on every host.

### Verified accessible on workers
- Conda environments: `$HOME/miniconda3/envs/prose`, `$HOME/miniconda3/envs/timer`, `$HOME/miniconda3/envs/harmonic`
- External tools: `$HOME/github/research/project/ext_tools/prose2`, `$HOME/github/research/project/ext_tools/timer`, `$HOME/github/research/project/ext_tools/harmonic`
- Repo: `$HOME/github/research/project/muscat-db/`
- Database: `$HOME/github/research/project/muscat-db/muscat.db` (**3,066,445,824 bytes** — same size on ut2, ut3, ut4, ut6)

### Cross-mounted raids
Three NFS servers auto-mount each other's storage:
- ut2 exports `/raid_ut2`
- ut3 exports `/raid_ut3` (mounted on ut2/ut6 at `/mnt_ut3/raid_ut3`)
- ut5 exports `/raid_ut5` (mounted on ut2/ut3/ut6 at `/mnt_ut5/raid_ut5`)

### Implication
The Phase-8 shared-path precondition is **already satisfied for ut2/ut3/ut4/ut6**: the required conda environments, external tools, repository, and database resolve through `/ut2`. Any new worker host needs the same autofs mount and NIS domain before it can run Celery tasks.

---

## Recommended Celery/Redis Role Assignment

### Redis broker + result backend → **ut2** (proposed)
- Always-on gateway co-located with the FastAPI web app (minimizes web↔broker latency).
- redis is lightweight; 44 GiB is ample.
- Bind to the LAN interface only if remote workers are actually enabled; protect it with authentication and a firewall restricted to participating `muscat-ut*` hosts.
- TCP port 6379 must be open from ut2 to workers across both subnets (46.x ↔ 29.x).

### Photometry workers (`prose` environment, high FITS I/O) → **ut6** (proposed primary), **ut3** (secondary), **ut4** (trial/overflow)
- Large RAM + currently available (ut6 load 0.15, ut3 load 2.4).
- Direct NFS access to raw data.
- Run at concurrency 1 for full reductions (per migration doc `_MAX_FULL_JOBS=1`).
- Start ut4 on test or overflow tasks; promote it only after a full reduction smoke test and an availability burn-in period.
- Bind to `photometry` Celery queue.

### Transit-fit / TTV workers (`timer`/`harmonic` environments, CPU-heavy MCMC) → **ut6**, **ut3** (proposed primary); **ut4**, **ut5/ut7** (capacity-gated)
- High core counts suit MCMC sampling.
- ut5 and ut7 are **currently saturated** by other users (load ~215–225); do not blindly schedule heavy transit-fit queues there.
- Add load-based routing: prefer ut6/ut3, use ut4 for tests or moderate overflow, and only use ut5/ut7 when their live load permits.
- Optional: apply `nice` limits or cgroups to avoid starving other users' processes.
- Bind to `transit_fit` Celery queue.

### Separate queue strategy
- `photometry` queue → ut6/ut3 workers at concurrency 1 (full serialization per current behavior); ut4 joins only after its smoke test.
- test/light queues → ut4 initially, with explicit low concurrency.
- `transit_fit` queue → ut6/ut3 workers at higher concurrency; ut4/ut5/ut7 only through capacity-aware overflow routing.
- This prevents one pipeline type from starving the other.

### Newly restored host guardrail
- **ut4** passed SSH, `/ut2` autofs, NTP, repository, database, and science-environment checks after returning online. Its uptime was only seven minutes during the probe, so require a smoke test and stability window before assigning production full-pipeline work.

---

## Operational Risks & Prerequisites

### 1. SQLite `muscat.db` on NFS + multiple concurrent writers
**Risk:** SQLite's file-locking semantics are unreliable over NFS. If multiple workers write to the jobs table simultaneously, corruption or lost updates are possible.

**Mitigation:** The migration doc already keeps SQLite as the UI-facing durable truth. Enforce **single-writer discipline**:
- Route all job-state writes through the web host (one writer) or designate one worker as the DB writer.
- Workers query the DB but do not write to it directly; they update job state via the web API or a central job service.
- Later phases (not Phase 1): consider moving `jobs` table to Redis or Postgres if multi-writer patterns become essential.

### 2. `OMP_NUM_THREADS=100` is set in the environment
**Risk:** This environment variable makes `nproc` report 100 on a 28-thread machine. When Celery spawns workers, each worker will oversubscribe by 100× if this is not reset per worker.

**Mitigation:**
- Explicitly unset or pin `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS` in the Celery worker startup script.
- Set them to match the worker's logical core count or a safe fraction (e.g., cores/2 for HT).
- Example: on ut2 with 28 threads, set `OMP_NUM_THREADS=14`.

### 3. OS/Python heterogeneity
**Risk:** Ubuntu 22.04 (py3.10) differs from 26.04 (py3.12–3.14) in glibc version. A compiled venv is not portable across major OS versions.

**Mitigation:**
- **Preferred:** Run workers inside the NFS-shared conda environments (`prose`, `timer`, `harmonic`), which are known to work across ut2/ut3/ut6.
- Install `celery` and `redis` into a dedicated shared conda env (e.g., `celery-env` on the shared `/ut2` path).
- Avoid relying on a single OS-specific venv.
- **Alternative:** Containerize workers (Docker/Singularity) with OS-pinned base images.

### 4. `uv` missing on workers
**Risk:** The web app uses `uv run`; workers do not have `uv` installed.

**Mitigation:**
- Install `uv` on each worker host via package manager or conda, or
- Avoid `uv` on the worker entrypoint path; use conda directly to run Celery workers.

### 5. Redis reachability
**Risk:** Firewall or network segmentation may block TCP 6379 from workers to the redis broker on ut2.

**Mitigation:**
- Verify that ut2 accepts inbound 6379 from `157.82.46.x` and `157.82.29.x`.
- Test connectivity: `redis-cli -h 157.82.46.83 -p 6379 ping` from each worker.
- If firewall rules exist, add exceptions for `muscat-ut*` → ut2:6379.

### 6. Existing load on ut5 and ut7
**Risk:** Both hosts are saturated by other users' jobs (load ~215–225). Heavy transit-fit queues will contend with existing workloads.

**Mitigation:**
- Do not automatically schedule transit-fit tasks on ut5/ut7 in Phase 1.
- Use load-aware routing: only overflow to ut5/ut7 if the primary queue (ut6/ut3) is full.
- Optional: apply process-level `nice` (low priority) or cgroup CPU caps to Celery workers on shared hosts.
- Monitor and adjust queue thresholds based on observed contention.

### 7. ut4 was recently restored
**Risk:** ut4 passed the worker prerequisite checks, but its availability history after restoration is not yet established.

**Mitigation:**
- Run one photometry test and one fit test with low concurrency.
- Confirm logs, outputs, cancellation, and finalizing behavior through the web UI.
- Observe host and NFS stability before promoting it from trial/overflow to regular production work.

### 8. Clock synchronization across hosts
**Risk:** Celery uses wall-clock times for task scheduling, retries, and timeouts. Clock skew between ut2 (redis/scheduler) and workers can cause missed deadlines or confusing error messages.

**Mitigation:**
- Verify NTP is running on all hosts: `systemctl status ntp.service` or `timedatectl status`.
- Check max drift: `ntpstat` or `chronyc tracking`.
- If drift > 1 second, investigate and correct before deploying Celery.

---

## Next Steps

The following actions are ordered deliberately: inventory validation precedes installation, and single-host testing precedes any remote-worker exposure.

1. **Phase 1 (single-host on ut2):**
   - Install `redis-server`, `redis`, `celery` into a shared conda env.
   - Start redis broker on ut2.
   - Implement Celery tasks (see CELERY_REDIS_MIGRATION.md Phase 3).
   - Test local Celery workers on ut2 (photometry + transit-fit queues).

2. **Phase 2 (multi-host rollout, Phase 8 of migration):**
   - Ensure prerequisites 1–8 above are resolved.
   - Register workers on ut6, ut3 (primary).
   - Register ut4 first on test/light queues with low concurrency.
   - Test a full photometry reduction and a transit-fit task across hosts.
   - Enable capacity-aware routing; test overflow to ut4 and, when live load permits, ut5/ut7.

3. **Phase 3 (promote ut4 after burn-in):**
   - Review its smoke-test results and availability history.
   - Promote it from test/light queues to regular overflow work if stable.

---

## See Also

- [CELERY_REDIS_MIGRATION.md](CELERY_REDIS_MIGRATION.md) — full multi-phase migration strategy, including Phase 8 (Step 24) which references this inventory.
- [CLAUDE.md](../CLAUDE.md) — project standards and environment setup.
- `/etc/hosts` on ut2 for NIS/IP mapping.
- `/etc/exports` on ut2 for NFS export rules.
