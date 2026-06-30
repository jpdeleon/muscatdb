"""Guard against event-loop-blocking routes (architecture audit M2).

Data-bound routes do synchronous SQLite + filesystem work. If they are declared
`async def` (with no `await`), FastAPI runs them directly on the event loop and
that blocking work stalls every other in-flight request. Declared as plain
`def`, FastAPI runs them in its threadpool instead. These routes must stay sync.
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
