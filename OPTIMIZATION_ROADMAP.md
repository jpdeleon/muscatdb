# Optimization Roadmap

## Completed

✅ **#1 — Cut redundant DB writes and cache invalidation from job polling** (PR #10 commit `dbcf94f`)
- Stopped `sync_jobs()` from rewriting unchanged running-job rows on every 2s poll
- Gated the one-time jobs schema migration to fire once per process instead of on every read/write
- Result: ~1.4× faster `get_persisted_jobs()`; directory caches now survive their 300s TTL

✅ **#4 — Photometry page double directory scan** (photometry.py, web.py)
- `list_photometry_runs()` now returns `(runs, run_outputs)` — a dict mapping each `run_id` to its pre-computed `list_outputs()` result
- `photometry_page()` in web.py reuses the pre-computed outputs for the selected run when no sinistro site/mode filter is active, skipping the redundant second directory walk
- Impact: one fewer `list_outputs()` call (and directory scan) on every photometry page load that is not sinistro-with-active-filter

✅ **#5 — Polling backoff** (templates/photometry.html, transit_fit.html)
- Replaced `setInterval(poll, 2000)` (fires forever at 2s) with `setTimeout`-based self-scheduling (`schedulePoll(delayMs)`) in both templates
- Intervals by state: `running/cancelling/finalizing` → 2 s; `pending` → 7 s; network error → 5 s; terminal states stop polling entirely
- Bonus: Fixed critical bug in transit_fit.html where `running` and `cancelling` states never rescheduled polling
- Impact: 3–4× fewer requests during queued/pending periods; no behaviour change during active log streaming

✅ **Bonus: Faster reload for test runs** (photometry.py, transit_fit.py, templates)
- Exposed `run_type` in job_status responses (photometry.py and transit_fit.py)
- Templates now check `s.run_type` and use 400ms reload delay for test runs vs 1200ms for full runs
- Impact: test runs reload ~3× faster, improving feedback loop

## Remaining (Low-Risk, High-ROI)

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
