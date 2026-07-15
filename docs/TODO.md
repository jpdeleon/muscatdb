WIP
* implement docs/MUSCATDB-LITE.md — greenfield redesign: capability-gated modular
  install (extras: obs, toi, nexsci, fov, expcalc, cluster), thin per-feature web
  routers, and a durable pull-based work queue for crash-safe multi-host execution
  (supersedes the old celery+redis migration plan; see MUSCATDB-LITE §12)

TODO
* merge tests
* profile then optimize page navigation
* per-user job namespacing (salvaged from the multi-user auth plan, Phase 2 — still
  unimplemented): namespace job keys and output dirs by user
  ({user}/{inst}/{date}/...), scope _MAX_FULL_JOBS per user, add a My Jobs / All Jobs
  toggle on the Jobs page
* rename guard (salvaged from the muscatdb-rename review): if the import/CLI is renamed
  to `muscatdb`, KEEP the Fernet KDF domain literal `muscat-db-user-settings:` and
  dual-read env vars during the window, or stored per-user LCO/ADS tokens can no longer
  be decrypted
* add a static but navigable github-pages version as visual muscat-db documentation
* improve test coverage
* setting user permissions (admin vs regular; is_admin column already exists)
* database health check command — root-cause the audit-2026-06-30.md (Part 1)
  findings (duplicate muscat3/231111 ingest, blank OBJECT rows, noncanonical obsdate)
* photometry: use median for best reference band?
* add methods for photometry for defocused datasets
