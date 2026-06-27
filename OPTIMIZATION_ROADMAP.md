# Optimization Roadmap

## Completed

✅ **#1 — Cut redundant DB writes and cache invalidation from job polling** (PR #10 commit `dbcf94f`)
- Stopped `sync_jobs()` from rewriting unchanged running-job rows on every 2s poll
- Gated the one-time jobs schema migration to fire once per process instead of on every read/write
- Result: ~1.4× faster `get_persisted_jobs()`; directory caches now survive their 300s TTL

## Remaining (Low-Risk, High-ROI)

### #4 — Photometry page double directory scan (web.py:306, 324)

**The issue:** When the photometry page loads, `list_photometry_runs()` (line 306) scans the directory to enumerate runs and their outputs. Then, the page calls `list_outputs()` again (line 324) for the *selected* run. For the selected run, the directory is walked twice.

**The fix:**
- Have `list_photometry_runs()` return the already-computed `list_outputs()` result for each run in a `RunDescriptor` or separate dict
- Pass that pre-computed result to the template so line 324 doesn't need a second scan
- Alternative: memoize `list_outputs()` on `(run_dir_path, mtime)` so the second call is instant

**Impact:** Quick win for page load time when a target has many runs or large directories.

---

### #5 — Polling backoff (templates/photometry.html, transit_fit.html)

**The issue:** Both pages poll `/photometry/status` and `/transit-fit/status` every 2 seconds, regardless of job state. During a quick job, this means 50+ unnecessary polls. Combined with #1's reduced redundancy, the remaining cost is small, but backoff still helps.

**The fix:**
- Widen polling interval when the job is in a quiescent state:
  - `running` state with advancing log: 2s (follow progress)
  - `finalizing` state: 2s (prose workers still writing)
  - `pending` state: 5–10s (waiting for the full-job slot)
  - No active job: 10–30s (stale job check)
- Reset to 2s if a new job is launched or state changes

**Impact:** Reduces steady-state load by 5–8× when jobs are queued or during idle periods. Client-side only, no backend changes needed.

---

## Compute Layer (External — prose2 / timer)

These optimizations require changes outside muscat-db:

### S1 — Photometry pipeline parallelism & caching (prose2)

**Observed:** `tests/test_slow_runs.py` profiles full photometry runs (~30 min on production host). Common bottlenecks are frame alignment, aperture extraction, and per-band photometry loops.

**Possible improvements:**
- Pre-compute star catalogs and alignment matrices in a background cron job, reuse across runs
- Parallelize per-band processing within a single run (currently sequential)
- Cache Gaia queries by (ra, dec, radius) tuple
- Use numpy vectorization instead of loop-based pixel operations where possible

**Why it matters:** A 10–20% reduction in per-run time would be visible to users; a 50% reduction would enable real-time photometry in the UI.

**How to identify:** Profile with `tests/test_slow_runs.py -m slow -v` on the production host; use `cProfile` or `py-spy` to find hotspots.

---

### S2 — Transit fitting sampling efficiency (timer)

**Observed:** Transit fits often take 10–20 min for MCMC sampling. Warmup and autocorrelation can be optimized.

**Possible improvements:**
- Detect stalled chains and restart with better initial conditions
- Adaptively adjust proposal scales during warmup
- Use parallel tempering or other advanced sampling techniques
- Cache prior PDFs and likelihood grids across runs with similar parameters

**Why it matters:** Same as S1 — real-time feedback in the UI.

---

## Notes

- **#4 and #5 are safe, self-contained, and can be done in parallel** — no coordinating changes needed
- **S1 and S2 require coordinating with the prose2 and timer maintainers** — outside this repo's scope
- **The microbench in #1 is reproducible** — run `/tmp/claude-1003/-raid-ut2-home-jerome-github-research-project-muscat-db/c7e4b699-7d30-4413-a8df-6cf48bdffb7a/scratchpad/bench.py` to verify

---

## Quick-Start Implementation Order

1. **#4 (photometry page scan)** — 30 min, high confidence, visible on every page load
2. **#5 (polling backoff)** — 1 hour, trivial risk, reduces network chatter
3. **S1 (prose2 profiling)** — 2 hours to measure; implement depends on findings
