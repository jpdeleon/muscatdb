"""Guard against event-loop-blocking routes (architecture audit M2).

Data-bound routes do synchronous SQLite + filesystem work. If they are declared
`async def` (with no `await`), FastAPI runs them directly on the event loop and
that blocking work stalls every other in-flight request. Declared as plain
`def`, FastAPI runs them in its threadpool instead. These routes must stay sync.

The reverse also matters (architecture audit C2): routes whose entire job is a
single external archive call (NASA/TOI TAP, ExoFOP, ADS) should be `async def`
using the shared httpx.AsyncClient (muscat_db.http_client), so FastAPI awaits
the I/O instead of occupying a threadpool slot for the whole request.
"""

import inspect

from starlette.routing import Route

from muscat_db.web import app

# Routes whose handlers do blocking SQLite/disk work and must run in the
# threadpool (i.e. be plain `def`, not `async def`).
BLOCKING_PATHS = {
    "/",
    "/target",
    "/logs",
    "/{instrument}",
    "/{instrument}/{obsdate}",
    "/api/targets/{obj}/note",
    "/api/targets/{obj}/identified",
}

# Routes whose entire job is a single external archive call and must be
# `async def` using muscat_db.web._async_get / http_client.get_async_client()
# so they free the threadpool while awaiting (see http_client.py's docstring
# for why the mixed-workload routes that also call NASA/TOI/HARPS lookups —
# /target, /api/lco/windows, /api/ephemeris/target-info — are deliberately
# NOT in this set: they do substantial synchronous local DB/job-store work in
# the same handler and use the sync httpx client instead).
ASYNC_PATHS = {
    "/api/exofop/check_confirmed",
    "/api/targets/jwst",
    "/api/targets/spectra",
    "/api/targets/publications",
    "/api/transit-fit/query-archive",
}


def _endpoints_by_path() -> dict[str, object]:
    out = {}
    for r in app.routes:
        if isinstance(r, Route):
            out.setdefault(r.path, r.endpoint)
    return out


def test_blocking_routes_are_sync_def():
    endpoints = _endpoints_by_path()
    for path in BLOCKING_PATHS:
        assert path in endpoints, f"route {path} not registered"
        ep = endpoints[path]
        assert not inspect.iscoroutinefunction(ep), (
            f"{path} is async def but does blocking I/O; it must be plain def "
            "so FastAPI runs it in the threadpool"
        )


def test_async_routes_are_async_def():
    endpoints = _endpoints_by_path()
    for path in ASYNC_PATHS:
        assert path in endpoints, f"route {path} not registered"
        ep = endpoints[path]
        assert inspect.iscoroutinefunction(ep), (
            f"{path} is plain def but its whole job is an external archive call; "
            "it should be async def using the shared httpx.AsyncClient so it "
            "doesn't occupy a threadpool slot for the duration of that call"
        )
