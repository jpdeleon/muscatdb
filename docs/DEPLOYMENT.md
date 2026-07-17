# Deployment & Cluster Inventory

**Host facts probed live from the NIS gateway (`muscat-ut2`) on 2026-07-13; `muscat-ut4` re-probed after returning online.** A living, point-in-time reference for running muscat-db — single-host today, multi-host under the durable work queue in [MUSCATDB-LITE.md](MUSCATDB-LITE.md) §12. All host/load/network facts must be rechecked before any multi-host rollout.

---

## Single-host deployment (nginx + tmux)

muscat-db runs behind nginx (HTTP Basic Auth) reverse-proxying to uvicorn, inside the `muscatdbgui` tmux session. The README "Multi-User Deployment" section has the full walkthrough; the essentials:

```bash
# First-time nginx setup (as root)
sudo bash deploy/setup-nginx.sh

# Manage users (writes the htpasswd file + the SQLite users row)
sudo env "PATH=$PATH" uv run muscat-db htpasswd add <user>
uv run muscat-db htpasswd delete <user>
uv run muscat-db htpasswd list

# Start / restart behind nginx (binds uvicorn 127.0.0.1:8001; nginx owns :8000)
uv run muscat-db restart --nginx --reload
```

Connect via SSH tunnel: `ssh -L 8000:localhost:8000 <user>@muscat-ut2` → http://localhost:8000.

### Authentication boundary and deployment verification

nginx authenticates browser users and forwards both `X-Forwarded-User` and a
private `X-MuSCAT-Proxy-Secret` header to uvicorn. In `--nginx` mode, the
application fails closed unless the request arrives from loopback with both
values valid. This prevents another account on the shared host from bypassing
HTTP Basic Auth by calling `127.0.0.1:8001` directly.

`deploy/setup-nginx.sh` creates the shared secret and the nginx include that
sets its header. The raw secret must be readable only by the account running
muscat-db:

```text
/etc/muscat-db/proxy-secret              0600 jerome:root
/etc/nginx/muscat-db-proxy-secret.conf   0600 root:root
```

Do not print, copy into documentation, or commit either secret value. Verify a
deployment without exposing it:

```bash
# Required ownership and mode for the raw application secret
stat -c '%A %a %U:%G %n' /etc/muscat-db/proxy-secret

# Configuration is valid; this does not reload nginx
sudo nginx -t

# Expected: 401 (nginx requires HTTP Basic Auth)
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/

# Expected: 200 (the deliberately public liveness probe)
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/healthz

# Expected: 401 (direct uvicorn access fails closed)
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/
```

Changing the secret file's owner/mode and running `nginx -t` do not stop
nginx, uvicorn, or science-pipeline processes, so they do not interrupt running
jobs. A muscat-db restart is a separate operation. Restart only in the existing
`muscatdbgui` tmux session, after checking for active photometry/transit jobs.
Python changes are picked up by `--reload`; HTML and JavaScript changes require
a restart before assuming they are deployed.

---

## Cluster inventory

Hosts derive from `/etc/hosts` on the gateway. Two subnets: `157.82.46.x` (West) and `157.82.29.x` (East).

| Host | IP | CPU Model | Phys / Log cores | RAM | OS / Python | Load @ probe | Status |
|------|----|-----------|-----------------|----|-------------|--------------|--------|
| **muscat-ut2** | 157.82.46.83 | Xeon Gold 5120 @2.2GHz (1 socket) | 14 / 28 | 44 GiB (+119 GiB swap) | Ubuntu 26.04 / 3.12.8 | low | **up** — NIS gateway, web GUI (tmux `muscatdbgui`), NFS server, NTP |
| **muscat-ut3** | 157.82.46.17 | 2× EPYC 7542 32C | 64 / 128 | 125 GiB | Ubuntu 22.04.5 / 3.10.12 | 2.4 (idle-ish) | **up** — `/raid_ut3` exported |
| **muscat-ut4** | 157.82.46.41 | Core i9-10940X @3.3GHz (1 socket) | 14 / 28 | 125 GiB (+127 GiB swap) | Ubuntu 22.04.5 / 3.10.12 | 0.12 | **up** — `/ut2` NFS, science envs, repo, DB, and NTP verified |
| **muscat-ut5** | 157.82.29.73 | 2× Xeon Gold 6338 32C @2.0GHz | 64 / 128 | 251 GiB | Ubuntu 22.04.5 / 3.10.12 | **~215 (saturated)** | **up but heavily loaded** — `/raid_ut5` exported |
| **muscat-ut6** | 157.82.29.74 | 2× Xeon Gold 6338 32C @2.0GHz | 64 / 128 | 251 GiB | Ubuntu 22.04.5 / 3.10.12 | 0.15 (idle) | **up, most available** |
| **muscat-ut7** | 157.82.46.82 | EPYC 9555 64C (1 socket, no SMT) | 64 / 64 | 246 GiB | Ubuntu 26.04 / 3.14.4 | **~225 (saturated)** | **up but heavily loaded** |

**Note on core counts:**
- ut2 (gateway) = 28 logical threads.
- ut3 is a bonus 128-logical host.
- ut4 = 14 physical / 28 logical threads with 125 GiB RAM; suitable for trial, light, or overflow work after a stability check.
- ut5 & ut6 (the "120-core" workhorses) = 128 logical threads each.
- ut7 = 64 physical cores (no hyper-threading).

---

## Software state

- Python is available everywhere; versions vary by OS (see table above).
- The cluster probe found **no PostgreSQL server or `psycopg`** on any host. These are the `[cluster]` prerequisite for the multi-host control plane (§12) and must be installed/tested before rollout. **Redis and Celery are not used** by the §12 design.

### Present on ut2 only
- **`uv` package manager** — used by the web app (`uv run`).
- On workers, either install `uv` per host or run worker entrypoints via the shared conda environments.

### Required before multi-host rollout
- Install PostgreSQL on ut2 and `psycopg` in the selected worker environment(s) (`muscatdb[cluster]`).
- Verify clock sync (NTP) across all participating hosts (lease/heartbeat expiry depends on it).

---

## Shared filesystem

The **linchpin assumption** for multi-host workers: `/ut2` and everything under it resolves identically on every host (the §12 shared-mount invariant).

### How it works
- On **ut2**: `/ut2` is a symlink to `/raid_ut2/home`. The `/raid_ut2` volume is a local ext4 filesystem on ut2.
- ut2 exports `/raid_ut2` via NFSv4 to all `muscat-ut*` hosts with `rw,sync,no_subtree_check,no_root_squash`.
- Worker hosts reach it via autofs (`/etc/auto.ut2`) which mounts `muscat-ut2:/raid_ut2` at `/mnt_ut2/raid_ut2`.
- The symlink `/ut2 → /raid_ut2/home` resolves to the same physical files on every host.

### Verified accessible on workers
- Conda environments: `$HOME/miniconda3/envs/prose`, `$HOME/miniconda3/envs/timer`, `$HOME/miniconda3/envs/harmonic`
- External tools: `$HOME/github/research/project/ext_tools/{prose2,timer,harmonic}`
- Repo: `$HOME/github/research/project/muscat-db/`
- Database: `$HOME/github/research/project/muscat-db/muscat.db` (**3,066,445,824 bytes** — same size on ut2, ut3, ut4, ut6)

### Cross-mounted raids
Three NFS servers auto-mount each other's storage:
- ut2 exports `/raid_ut2`
- ut3 exports `/raid_ut3` (mounted on ut2/ut6 at `/mnt_ut3/raid_ut3`)
- ut5 exports `/raid_ut5` (mounted on ut2/ut3/ut6 at `/mnt_ut5/raid_ut5`)

### Implication
The §12 shared-path precondition is **already satisfied for ut2/ut3/ut4/ut6**: the conda environments, external tools, repository, and (read-only) catalog database resolve through `/ut2`. Any new worker host needs the same autofs mount and NIS domain first. Logs and science outputs on this shared mount are what let the web host tail worker logs for SSE.

---

## Recommended worker role assignment (§12 durable queue)

Under §12 every worker runs the same `muscatdb worker --pipeline <name>` loop, pulling jobs from the durable queue (SKIP-LOCKED claim + lease). **No broker.** The **control plane** (PostgreSQL, `[cluster]`) lives on ut2.

### PostgreSQL control plane → **ut2**
- Always-on gateway co-located with the FastAPI web app (minimizes web↔DB latency).
- Lightweight; 44 GiB is ample.
- Bind to the LAN interface; grant workers a **least-privilege** role; firewall TCP 5432 to participating `muscat-ut*` hosts only (both subnets, 46.x ↔ 29.x).

### Photometry workers (`prose` env, high FITS I/O) → **ut6** (primary), **ut3** (secondary), **ut4** (trial/overflow)
- Large RAM + currently available (ut6 load 0.15, ut3 load 2.4); direct NFS access to raw data.
- Per-pipeline concurrency 1 for full reductions (each prose run already fans out internally via `SequenceParallel`).
- Promote ut4 only after a full-reduction smoke test and an availability burn-in.

### Transit-fit / TTV workers (`timer`/`harmonic`, CPU-heavy MCMC) → **ut6**, **ut3** (primary); **ut4**, **ut5/ut7** (capacity-gated)
- High core counts suit MCMC sampling.
- ut5 and ut7 are **currently saturated** by other users (load ~215–225); do not blind-schedule heavy work there.
- Prefer ut6/ut3; use ut4 for tests or moderate overflow; use ut5/ut7 only when live load permits. Apply `nice`/cgroup caps on shared hosts.

### Newly restored host guardrail
- **ut4** passed SSH, `/ut2` autofs, NTP, repo, DB, and science-env checks after returning online, but its uptime was only ~7 minutes at probe. Require a smoke test and stability window before production full-pipeline work.

---

## Operational risks & prerequisites

### 1. Concurrent job-state writes on a shared filesystem
**Risk:** SQLite's file-locking is unreliable over NFS; multiple workers writing the jobs table risk corruption / lost updates.
**§12 resolution:** the mutable **control plane moves to PostgreSQL** (`[cluster]`) — workers write job state transactionally to Postgres, never to SQLite over NFS. The catalog `muscat.db` stays SQLite but is derived, local to ut2, and read-only to workers. No single-writer callback is needed.

### 2. `OMP_NUM_THREADS=100` is set in the environment
**Risk:** makes `nproc` report 100 on a 28-thread machine; a worker oversubscribes ~100× if unset.
**Mitigation:** in each **worker systemd unit**, pin `OMP_NUM_THREADS` / `MKL_NUM_THREADS` / `OPENBLAS_NUM_THREADS` to the host's logical-core budget (e.g. `14` on ut2).

### 3. OS/Python heterogeneity
**Risk:** Ubuntu 22.04 (py3.10) vs 26.04 (py3.12–3.14) differ in glibc; a compiled venv is not portable across major OS versions.
**Mitigation (preferred):** run workers inside the NFS-shared conda envs (`prose`/`timer`/`harmonic`), known to work across ut2/ut3/ut6. Install `psycopg` into a dedicated shared conda env on `/ut2`. **Alternative:** containerize workers with OS-pinned images.

### 4. `uv` missing on workers
**Risk:** the web app uses `uv run`; workers don't have `uv`.
**Mitigation:** install `uv` per host, or run worker entrypoints via conda directly.

### 5. PostgreSQL reachability
**Risk:** firewall/segmentation may block TCP 5432 from workers to ut2.
**Mitigation:** verify ut2 accepts inbound 5432 from `157.82.46.x` and `157.82.29.x`; test `psql -h 157.82.46.83 -p 5432` from each worker; add firewall exceptions for `muscat-ut*` → ut2:5432.

### 6. Existing load on ut5 and ut7
**Risk:** both saturated (load ~215–225); heavy transit-fit work contends with other users.
**Mitigation:** load-aware routing (overflow to ut5/ut7 only when the ut6/ut3 primary is full); `nice`/cgroup caps.

### 7. ut4 recently restored
**Risk:** passed prerequisite checks, but post-restoration availability history is not yet established.
**Mitigation:** run one photometry test and one fit test at low concurrency; confirm logs, outputs, cancellation, and finalizing through the web UI; observe host + NFS stability before promotion.

### 8. Clock synchronization across hosts
**Risk:** worker **lease/heartbeat expiry** (§12) uses wall-clock time; skew between ut2 and workers can cause premature reclaims or confusing errors.
**Mitigation:** verify NTP on all hosts (`timedatectl status`); check drift (`chronyc tracking`); correct drift > 1 s before rollout.

---

## Next steps (tracks MUSCATDB-LITE §12 / port P2 & P9)

1. **Single-host (P2):** run one `muscatdb worker` on ut2 against the SQLite control plane; prove claim / lease / finalize / cancel across the web↔worker boundary.
2. **Multi-host (P9):** install PostgreSQL + `psycopg` (`[cluster]`) on ut2; set `MUSCAT_CONTROL_PLANE=postgres`; run the **unchanged** worker loop on ut6/ut3 as systemd units with core-pinned thread caps; register ut4 on a test queue first; capacity-gate ut5/ut7.
3. **Promote ut4** from test/overflow to regular work after burn-in.

---

## See also
- [MUSCATDB-LITE.md](MUSCATDB-LITE.md) §12 — distributed execution (durable work queue), the authoritative design.
- [CLAUDE.md](../CLAUDE.md) — project standards and environment setup.
- `/etc/hosts` (NIS/IP mapping) and `/etc/exports` (NFS rules) on ut2.
