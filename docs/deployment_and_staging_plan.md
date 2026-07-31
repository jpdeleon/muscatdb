# Deployment, Migration, Staging & Closing Issue #26 Plan

This document details the step-by-step implementation plan for establishing dual **Production (`main`)** and **Staging (`test`)** environments, migrating legacy data outputs under `$HOME/deploy/`, and completing all tasks required to close [muscat-team/muscatdb#26](https://github.com/muscat-team/muscatdb/issues/26).

---

## 1. Overview & Architecture

### Environment Mapping

| Environment | Branch | Code Checkout Directory | Port | Data & Outputs |
| :--- | :--- | :--- | :--- | :--- |
| **Production (`main`)** | `main` | `$HOME/deploy/main/app` | **`8000`** | **DB:** `$HOME/github/research/project/muscat-db/muscat.db`<br>**Outputs:** `$HOME/deploy/main/{prose,timer,harmonic}` |
| **Staging (`test`)** | `test` | `$HOME/deploy/test/app` | **`8001`** | **DB:** `$HOME/deploy/test/muscat_test.db`<br>**Outputs:** `$HOME/deploy/test/{prose,timer,harmonic}` |
| **Local Dev (`dev`)** | *(local)* | `$HOME/github/research/project/muscat-db` | **`8002`** | **DB:** local / `$HOME/github/research/project/muscat-db/muscat.db`<br>**Outputs:** `$HOME/deploy/main/` or local temp |

### Port Number Verification Summary

- **Production (`8000`)**: Keeps port `8000` unchanged so existing web clients, API calls, and Nginx proxies do not break.
- **Staging (`8001`)**: Runs background preview server on `8001` auto-deployed on push/merge to `test`.
- **Local Dev (`8002`)**: Recommended port for personal interactive coding and debugging sessions.

> [!NOTE]
> `$HOME/data` (raw telescope FITS files) is **shared read-only** across all environments.

---

## 2. Directory Layout Comparison Matrix

### Deployment Target Locations Comparison

| Location | Sudo Required? | FHS Compliant? | Pros | Cons | Decision / Recommendation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`~/deploy/`** (`$HOME/deploy/`) | ❌ **No** | 🟢 Yes (User space) | Fully owned by user `jerome`; zero root access required; zero risk to system OS files; quick setup on `ut2`. | Large output datasets stored under `$HOME` directory. | **Selected Strategy** for current `ut2` deployment. |
| **`/var/www/muscat-db/`** | ⚠️ **Yes** | 🟢 Yes (Web standard) | Industry-standard Linux path for Nginx web services; keeps heavy outputs outside `/home`. | Requires `sudo` access to create and set chown permissions once on `ut2`. | **Alternative** if full system-level isolation is preferred later. |
| **`./deploy`** (Repo root) | ❌ **No** | ❌ No | Everything under single repo path. | 🚨 **High risk**: `git reset --hard` or `git clean` in CI will delete/wipe live DBs, logs, and outputs. | ❌ **Not Recommended**. |

---

## 3. Directory Layout on `ut2`

```
$HOME/deploy/
├── main/
│   ├── app/            # Isolated clone/checkout of the main branch (Production)
│   ├── prose/          # Photometry output products
│   ├── timer/          # Transit fit output products
│   └── harmonic/       # TTV fit output products
│
└── test/
    ├── app/            # Isolated clone/checkout of the test branch (Staging)
    ├── muscat_test.db  # Isolated test database
    ├── prose/          # Staging photometry outputs
    ├── timer/          # Staging transit fit outputs
    └── harmonic/       # Staging TTV fit outputs
```

---

## 4. Host Setup & Migration Steps (on `ut2`)

### Step 4.1: Create Deploy Directories & Checkouts

```bash
# Create base deployment structure
mkdir -p $HOME/deploy/main $HOME/deploy/test

# Setup main (Production) checkout
cd $HOME/deploy/main
git clone https://github.com/muscat-team/muscatdb.git app
cd app && git checkout main && uv sync --dev

# Setup test (Staging) checkout
cd $HOME/deploy/test
git clone https://github.com/muscat-team/muscatdb.git app
cd app && git checkout test && uv sync --dev
```

### Step 4.2: Migrate Legacy Output Products (`$HOME/ql/`)

Move legacy products into `$HOME/deploy/main/` and leave symlinks at `$HOME/ql/` for backward compatibility:

```bash
# 1. Stop active production server if running
tmux send-keys -t "muscatdbgui" C-c || true

# 2. Move directories to $HOME/deploy/main/
mv $HOME/ql/prose $HOME/deploy/main/prose
mv $HOME/ql/timer $HOME/deploy/main/timer
mv $HOME/ql/harmonic $HOME/deploy/main/harmonic

# 3. Create backward-compatibility symlinks
ln -s $HOME/deploy/main/prose $HOME/ql/prose
ln -s $HOME/deploy/main/timer $HOME/ql/timer
ln -s $HOME/deploy/main/harmonic $HOME/ql/harmonic
```

### Step 4.3: Initialize Staging Database & Output Directories

```bash
# Create staging output directories
mkdir -p $HOME/deploy/test/prose $HOME/deploy/test/timer $HOME/deploy/test/harmonic

# Copy production database as staging initial state
cp $HOME/github/research/project/muscat-db/muscat.db $HOME/deploy/test/muscat_test.db
```

---

## 5. What Needs To Be Done To Close #26

Below is the status and action plan for the 6 specific points raised in [Issue #26](https://github.com/muscat-team/muscatdb/issues/26):

### Item 1: Separate Deploy Path from Dev Checkout
- **Issue:** Previously `deploy.yml` defaulted to `$HOME/github/research/project/muscat-db` and ran `git reset --hard origin/main`, wiping uncommitted dev changes.
- **Action Required:** Point `main` deployment to `$HOME/deploy/main/app` and `test` deployment to `$HOME/deploy/test/app`.

### Item 2: Full Job Concurrency Env Cap (`MUSCAT_MAX_FULL_JOBS`)
- **Issue:** `_MAX_FULL_JOBS` was hardcoded. Staging running a full job concurrently with production would overwhelm CPU/RAM.
- **Status:** **Completed in codebase.** `MUSCAT_MAX_FULL_JOBS` is implemented across `photometry.py`, `transit_fit.py`, and `ttv_fit.py`, returning explicit user error messages when `0`.
- **Action Required:** Set `MUSCAT_MAX_FULL_JOBS=0` in staging environment startup inside `deploy.yml`.

### Item 3: Drop `--reload` in Production and Staging
- **Issue:** `--reload` is a dev file watcher that invalidates active SQLite write transactions (e.g. during `ingest_date`) and leaves orphan `multiprocessing.spawn` children.
- **Action Required:** Remove `--reload` from uvicorn invocations in `deploy.yml` and production/staging startup commands.

### Item 4: Chat Fix (`web:sio_app`)
- **Status:** **Completed in #25.** `sio_app` ASGI wrapper is mounted and active.

### Item 5: Coverage Gate (`--cov-fail-under`)
- **Status:** **Completed in CI.** `ci.yml` enforces `--cov-fail-under=68`.

### Item 6: Staging Instance Configuration
- **Action Required:** Configure `deploy-staging` job in `deploy.yml` with:
  - `MUSCAT_DB_PATH=$HOME/deploy/test/muscat_test.db`
  - `MUSCAT_PROSE_DIR=$HOME/deploy/test/prose`
  - `MUSCAT_TIMER_DIR=$HOME/deploy/test/timer`
  - `MUSCAT_TTV_DIR=$HOME/deploy/test/harmonic`
  - `MUSCAT_MAX_FULL_JOBS=0`
  - `MUSCAT_LCO_MONITOR_ENABLED=0`
  - `MUSCAT_LCO_ALLOW_SUBMIT` unset
  - Port `8001` inside `tmux: muscatdb-test`

---

## 6. GitHub Actions Deployment Workflow (`.github/workflows/deploy.yml`)

```yaml
name: Deploy

on:
  push:
    branches: [main, test]

concurrency:
  group: deploy-${{ github.ref }}
  cancel-in-progress: true

jobs:
  deploy-production:
    name: Deploy Production (main)
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4

      - name: Check deploy secrets
        id: check
        env:
          DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
          DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
        run: |
          if [ -z "$DEPLOY_SSH_KEY" ] || [ -z "$DEPLOY_HOST" ] || [ -z "$DEPLOY_USER" ]; then
            echo "configured=false" >> "$GITHUB_OUTPUT"
          else
            echo "configured=true" >> "$GITHUB_OUTPUT"
          fi

      - name: Install SSH key
        if: steps.check.outputs.configured == 'true'
        run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.DEPLOY_SSH_KEY }}" > ~/.ssh/deploy_key
          chmod 600 ~/.ssh/deploy_key
          ssh-keyscan -H "${{ secrets.DEPLOY_HOST }}" >> ~/.ssh/known_hosts 2>/dev/null

      - name: Pull and restart production
        if: steps.check.outputs.configured == 'true'
        run: |
          ssh -i ~/.ssh/deploy_key "${{ secrets.DEPLOY_USER }}@${{ secrets.DEPLOY_HOST }}" << 'EOF'
            set -e
            cd $HOME/deploy/main/app
            git fetch origin main
            git reset --hard origin/main
            uv sync --dev
            tmux send-keys -t "muscatdb-main" "" C-c || true
            sleep 2
            CMD="export MUSCAT_PROSE_DIR=\$HOME/deploy/main/prose && \
                 export MUSCAT_TIMER_DIR=\$HOME/deploy/main/timer && \
                 export MUSCAT_TTV_DIR=\$HOME/deploy/main/harmonic && \
                 uv run uvicorn muscat_db.web:sio_app --host 127.0.0.1 --port 8000"
            tmux send-keys -t "muscatdb-main" "$CMD" Enter || \
              tmux new-session -d -s "muscatdb-main" "$CMD"
          EOF

  deploy-staging:
    name: Deploy Staging (test)
    if: github.ref == 'refs/heads/test'
    runs-on: ubuntu-latest
    environment: staging
    steps:
      - uses: actions/checkout@v4

      - name: Check deploy secrets
        id: check
        env:
          DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
          DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
        run: |
          if [ -z "$DEPLOY_SSH_KEY" ] || [ -z "$DEPLOY_HOST" ] || [ -z "$DEPLOY_USER" ]; then
            echo "configured=false" >> "$GITHUB_OUTPUT"
          else
            echo "configured=true" >> "$GITHUB_OUTPUT"
          fi

      - name: Install SSH key
        if: steps.check.outputs.configured == 'true'
        run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.DEPLOY_SSH_KEY }}" > ~/.ssh/deploy_key
          chmod 600 ~/.ssh/deploy_key
          ssh-keyscan -H "${{ secrets.DEPLOY_HOST }}" >> ~/.ssh/known_hosts 2>/dev/null

      - name: Pull and restart staging
        if: steps.check.outputs.configured == 'true'
        run: |
          ssh -i ~/.ssh/deploy_key "${{ secrets.DEPLOY_USER }}@${{ secrets.DEPLOY_HOST }}" << 'EOF'
            set -e
            cd $HOME/deploy/test/app
            git fetch origin test
            git reset --hard origin/test
            uv sync --dev
            tmux send-keys -t "muscatdb-test" "" C-c || true
            sleep 2
            CMD="export MUSCAT_DB_PATH=\$HOME/deploy/test/muscat_test.db && \
                 export MUSCAT_PROSE_DIR=\$HOME/deploy/test/prose && \
                 export MUSCAT_TIMER_DIR=\$HOME/deploy/test/timer && \
                 export MUSCAT_TTV_DIR=\$HOME/deploy/test/harmonic && \
                 export MUSCAT_MAX_FULL_JOBS=0 && \
                 export MUSCAT_LCO_MONITOR_ENABLED=0 && \
                 unset MUSCAT_LCO_ALLOW_SUBMIT && \
                 uv run uvicorn muscat_db.web:sio_app --host 127.0.0.1 --port 8001"
            tmux send-keys -t "muscatdb-test" "$CMD" Enter || \
              tmux new-session -d -s "muscatdb-test" "$CMD"
          EOF
```

---

## 7. Execution Verification & Closing Issue #26

1. **Host Setup:** Execute Directory Setup & Legacy Output Migration on `ut2`.
2. **PR Creation:** Open PR on `test` branch with updated `deploy.yml`.
3. **Staging Test:** Merge PR to `test` -> Verify GitHub Actions triggers `deploy-staging` and launches port `8001` inside `muscatdb-test`.
4. **Production Release:** Merge `test` into `main` -> Verify GitHub Actions triggers `deploy-production` and launches port `8000` inside `muscatdb-main`.
5. **Close #26:** Comment resolution summary on [muscat-team/muscatdb#26](https://github.com/muscat-team/muscatdb/issues/26) and close the issue.
