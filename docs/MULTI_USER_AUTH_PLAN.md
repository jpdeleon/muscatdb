# Multi-User Authentication & Job Namespacing Plan

## Motivation

muscat-db currently runs under the `jerome` OS account. Other users access it via
SSH tunnel. There is no authentication — the server is open on `0.0.0.0:8000`.
All jobs launched through the web UI record `getpass.getuser()` ("jerome") as
`user_name`, making it impossible to distinguish one user's work from another's.
The single global `LCO_API_TOKEN` env var forces all users to share one LCO
identity.

## Current State

| Aspect | Status |
|--------|--------|
| Auth | None. No login, no sessions, no CSRF. Only `GZipMiddleware` runs. |
| Framework | FastAPI. `Request` object never imported. |
| Job user tracking | `user_name` column exists in `jobs` table, but defaults to `getpass.getuser()` — "jerome" for everyone through the web UI. |
| LCO scheduling | Single global `LCO_API_TOKEN` env var. |
| `_MAX_FULL_JOBS` | Global `= 1` — one concurrent full job across all users. |
| In-memory job dicts | `photometry._JOBS` / `transit_fit._FIT_JOBS` keyed by `{inst}/{date}/{target}/{run_id}` — no user namespace. |
| Output directories | `$MUSCAT_PROSE_DIR/{inst}/{date}/_runs_/{target}/{run_id}/` — shared. |
| DB rebuild | Daily cron rebuilds observation tables; app-owned tables (`jobs`, `target_notes`, `users`, etc.) preserved via `_APP_OWNED_TABLES`. |

## Auth Architecture

nginx is the **primary auth layer**. muscat-db's built-in session auth is an
optional fallback for direct-network access without a tunnel.

### Preferred: nginx Reverse Proxy

```
ssh -L 8000:localhost:8000 alice@server
        │
        ▼  HTTP Basic Auth (browser popup, cached per session)
     nginx :8000
        │  proxy_set_header X-Forwarded-User $remote_user;
        │  proxy_pass http://127.0.0.1:8001;
        ▼
     uvicorn :8001  (bound to 127.0.0.1 only)
        │
        ▼  request.state.user = "alice" (from header)
```

- nginx listens on port 8000, the port users SSH-tunnel to
- nginx requires HTTP Basic Auth against an `htpasswd` file
- The `$remote_user` is forwarded as `X-Forwarded-User` header
- Uvicorn binds to `127.0.0.1:8001` — only nginx on localhost can reach it
- nginx strips any incoming `X-Forwarded-User` and sets its own

**User experience**: One browser auth popup per browser restart (cached for the
session). No muscat-db login page needed.

### Optional: Built-in Session Auth (no nginx)

For direct access without SSH tunnel or as fallback:

```
[Browser] → FastAPI :8001
                ↓
     SessionMiddleware (signed cookie)
                ↓
     AuthenticationMiddleware → request.state.user
                ↓
     require_user() dependency on protected routes
```

| | nginx + Basic Auth | Built-in session auth |
|---|---|---|
| Login experience | Browser popup, cached | Page form |
| User table needed? | Yes (settings only — `password_hash` stays empty) | Yes (auth + settings) |
| External dependency | nginx + htpasswd | None |
| Password in DB | Empty (nginx handles auth) | `passlib[bcrypt]` hash |

Both flow into the same `request.state.user` and share all downstream code.

## Users Table

Added to `_APP_OWNED_TABLES` so it survives daily `build-db`.

```sql
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL DEFAULT '',
    display_name  TEXT NOT NULL DEFAULT '',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login    TEXT,
    settings      TEXT NOT NULL DEFAULT '{}'
);
```

`settings` stores encrypted per-user tokens:
```json
{"lco_token_enc": "<Fernet ciphertext>", "ads_token_enc": "<Fernet ciphertext>"}
```

**Auto-provisioning**: When `X-Forwarded-User` is present and the user doesn't
exist yet, insert a row with empty `password_hash`. The first visit creates the
account.

## Encryption

- `cryptography.fernet.Fernet` for token encryption.
- Key derived from `MUSCAT_DB_SECRET` via SHA-256.
- Stored tokens are decrypted per-request when the LCO API is called.

## Per-User Job Namespacing

Collision surface when two users launch identical jobs:

| Resource | Current key | Problem |
|----------|-------------|---------|
| In-memory `_JOBS[key]` | `{inst}/{date}/{target}/{run_id}` | Collision → second user cancels first |
| DB `jobs` table row | `{type}:{inst}/{date}/{target}/{run_id}` | `ON CONFLICT` silently overwrites |
| Output directory | `$PROSE_DIR/{inst}/{date}/_runs_/{target}/{run_id}/` | Products clobber |
| `_MAX_FULL_JOBS` | Global counter | User A blocks User B |
| Pending queue | Same key → same row | Enqueue overwrites |

**Fix — namespace everything by `user`:**

```
Job key:        {type}:{user}/{inst}/{date}/{target}/{run_id}
Output dir:     $MUSCAT_PROSE_DIR/{user}/{inst}/{date}/_runs_/{target}/{run_id}/
                $MUSCAT_TIMER_DIR/{user}/{inst}/{date}/{target}/{run_id}/
Concurrency:    per-user _MAX_FULL_JOBS (each user gets their own slot)
```

## Per-User LCO Tokens

- Stored encrypted in `users.settings["lco_token_enc"]`.
- `lco._get_lco_api_token()` accepts optional `username` param → fetches from DB.
- Falls back to global `LCO_API_TOKEN` env var when no user token is stored.
- Users manage their token via `/settings` page (masked input field).

## Implementation Plan

### Phase 1 — Auth Foundation

| Step | Files | Changes |
|------|-------|---------|
| 1.1 | `pyproject.toml` | Add `cryptography` |
| 1.2 | `config.py` | Add `MUSCAT_DB_SECRET` env var |
| 1.3 | `database.py` | Add `users` table to `SCHEMA`, add to `_APP_OWNED_TABLES` |
| 1.4 | `database.py` | Add `ensure_user()`, `get_user_settings()`, `update_user_settings()` |
| 1.5 | `database.py` | Add `_encrypt_token()` / `_decrypt_token()` Fernet helpers |
| 1.6 | `web.py` | Add middleware reading `X-Forwarded-User` → `request.state.user` |
| 1.7 | `web.py` | Update `_render()` to pass `current_user` to all templates |
| 1.8 | `templates/base.html` | Navbar shows username, Settings, Logout |
| 1.9 | *(nginx)* | Create `nginx.conf` snippet for the repo |

### Phase 2 — Per-User Job Namespacing

| Step | Files | Changes |
|------|-------|---------|
| 2.1 | `photometry.py` | `job_key()` accepts `user`; `_JOBS` key includes user |
| 2.2 | `photometry.py` | `run_output_dir()` includes user in path |
| 2.3 | `photometry.py` | `_count_running_full()` scoped to user |
| 2.4 | `photometry.py` | `start_run()` accepts `user_name` from web layer |
| 2.5 | `transit_fit.py` | `fit_job_key()` accepts `user`; `_FIT_JOBS` key includes user |
| 2.6 | `transit_fit.py` | `fit_output_dir()` includes user in path |
| 2.7 | `transit_fit.py` | `_count_running_full()` scoped to user |
| 2.8 | `transit_fit.py` | `start_fit()` accepts `user_name` from web layer |
| 2.9 | `web.py` | All photometry/transit-fit endpoints pass `request.state.user` |
| 2.10 | `web.py` | `sync_jobs()` in both pipelines reconciles user-scoped keys |
| 2.11 | `templates/jobs.html` | Add "My Jobs" / "All Jobs" filter toggle |

### Phase 3 — Per-User LCO Tokens

| Step | Files | Changes |
|------|-------|---------|
| 3.1 | `database.py` | Add `get_user_lco_token()`, `set_user_lco_token()` helpers |
| 3.2 | `lco.py` | Refactor `_get_lco_api_token()` → accepts optional `username` |
| 3.3 | `lco.py` | Thread user through all LCO API functions |
| 3.4 | `web.py` | LCO endpoints use `request.state.user` |
| 3.5 | `web.py` | Add `GET /api/settings/lco-token-status`, `POST /api/settings/lco-token` |
| 3.6 | `templates/settings.html` | LCO token input (masked, with show/hide, save button) |
| 3.7 | `templates/lco_schedule.html` | Token status dot reflects user's token, not global |

### Backward Compatibility

- Without `X-Forwarded-User` header, `request.state.user` remains `None` and
  unprotected routes still work (current behavior).
- LCO token lookup falls back to global `LCO_API_TOKEN` env var when no user
  token is stored.
- Existing DB `jobs` rows with empty `user_name` (pre-migration) remain visible
  and use legacy key resolution.
- Output directories without `{user}/` prefix are found by checking both
  namespaced and legacy paths.
- CLI commands (`scan`, `build-db`, `summary`) don't require auth.

### Estimated Effort

| Module | Files | Lines |
|--------|-------|-------|
| Schema + DB helpers | `database.py` | ~80 |
| Middleware + templates | `web.py`, `base.html` | ~80 |
| Photometry namespacing | `photometry.py` | ~80 |
| Transit-fit namespacing | `transit_fit.py` | ~80 |
| LCO token plumbing | `lco.py`, `web.py`, `settings.html` | ~80 |
| nginx config | `nginx.conf` snippet | ~30 |
| Dependencies | `pyproject.toml` | ~3 |

**Total: ~430 lines across ~10 files.**

## nginx Deployment

The server is expected to run inside the existing `muscatdb-gui` tmux session.
nginx runs as a system service independent of the tmux session.

### Files

| File | Purpose |
|------|---------|
| `deploy/nginx.conf` | nginx site config (listens on 127.0.0.1:8000, proxies to :8001) |
| `deploy/setup-nginx.sh` | First-time installer (apt install, enable site, reload) |
| `src/muscat_db/cli.py` | `htpasswd` subcommand group + `--nginx` flag on `serve`/`restart` |

### Setup (one-time, as root)

```bash
sudo bash deploy/setup-nginx.sh

# Add a user:
uv run muscat-db htpasswd add alice

# Restart muscat-db behind nginx:
uv run muscat-db restart --nginx
```

### nginx Config (`deploy/nginx.conf`)

```nginx
server {
    listen 127.0.0.1:8000;

    auth_basic           "MuSCAT-db";
    auth_basic_user_file /etc/nginx/.htpasswd-muscatdb;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header X-Forwarded-User $remote_user;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
    }
}
```

### CLI Reference

```bash
# User management (sudo not needed — writes to htpasswd via CLI helper)
muscat-db htpasswd add alice          # prompts for password
muscat-db htpasswd add bob --admin    # mark as admin
muscat-db htpasswd delete bob
muscat-db htpasswd list

# Server start with nginx proxy defaults (127.0.0.1:8001)
muscat-db serve --nginx
muscat-db restart --nginx             # stop old, start new
```

The `add` command:
1. Hashes the password with Apache MD5 (`openssl passwd -apr1`)
2. Writes to `/etc/nginx/.htpasswd-muscatdb`
3. Creates the SQLite `users` row for settings storage (INSERT OR IGNORE)

The `delete` command removes the user from both the htpasswd file and the
SQLite `users` table.

### User Connection

```bash
ssh -L 8000:localhost:8000 alice@muscat-server
```

Browser → `http://localhost:8000` → nginx asks for password (one browser
popup per restart, cached for the session). After auth, all requests carry
`X-Forwarded-User: alice` to the FastAPI backend.
