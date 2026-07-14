# muscatdb naming migration

Date: 2026-07-14

## Stage the import and CLI rename

**Recommendation:** Strong

**Dependency category:** local-substitutable

**Files:** `pyproject.toml`, `src/muscat_db/`, `tests/`, `scripts/`,
`notebooks/`, `.github/workflows/`

### Before: atomic cutover

```text
62 Python callers                 cron and operators
import muscat_db                  muscat-db <command>
       |                                 |
       v                                 v
ModuleNotFoundError                command not found
       \                                 /
        +------- new names only --------+
```

### After: compatibility seam

```text
+--------------------------------------------------+
| Canonical muscatdb module                        |
|                                                  |
|  - temporary muscat_db import adapter            |
|  - temporary muscat-db CLI alias                 |
|  - remove adapters after one release window      |
+--------------------------------------------------+
```

**Problem:** The import package and CLI are public interfaces; an atomic rename
breaks every caller at once.

**Solution:** Make `muscatdb` canonical while temporary adapters preserve the
old interfaces during a measured migration window.

- Locality: migration logic concentrates.
- Leverage: callers move independently.
- Tests cover both interfaces.

## Freeze persisted protocol names

**Recommendation:** Strong

**Dependency category:** ports and adapters

**Files:** `src/muscat_db/database.py`, `src/muscat_db/config.py`,
`.env.example`, `muscat.db`

### Before: global replacement

```text
KDF domain: muscat-db-user-settings:
                   |
                   v
          rename changes key
                   |
                   v
       stored tokens cannot decrypt
```

### After: explicit protocol seam

```text
+--------------------------------------------------+
| Canonical code name: muscatdb                    |
|                                                  |
|  - retain the legacy KDF literal                 |
|  - dual-read environment variables during       |
|    migration                                     |
+--------------------------------------------------+
```

**Problem:** Branding, configuration, database filenames, and cryptographic
literals currently look alike but have different compatibility contracts.

**Solution:** Rename the code identity while preserving persisted literals, or
migrate them explicitly behind one seam.

- Locality: protocol exceptions are documented.
- Leverage: existing data remains readable.
- The interface distinguishes identity from storage.

## Coordinate deployment filesystem names

**Recommendation:** Worth exploring

**Dependency category:** ports and adapters

**Files:** `deploy/`, `.github/workflows/deploy.yml`, `README.md`, cron, nginx,
tmux, repository checkout

### Before: split names

```text
repository checkout: muscat-db      Git remote: muscatdb
tmux: two spellings                 nginx site: muscat-db
                  \                 /
                   v               v
                    hard-coded paths
```

### After: one deployment name

```text
                         muscatdb
                            |
              +-------------+-------------+
              |             |             |
          checkout         tmux          nginx

          Cut over in one maintenance event.
```

**Problem:** Hard-coded checkout, tmux, nginx, and cron names make partial
deployment renames fail operationally.

**Solution:** Treat host configuration as a separate migration and verify each
adapter before retiring old filesystem names.

- Locality: host changes are grouped.
- Tests exercise the deployment interface.
- Rollback remains available.

## Top recommendation

Stage the import and CLI rename. This makes `muscatdb` canonical without
coupling the code rollout to every external caller and host-configuration
change.
