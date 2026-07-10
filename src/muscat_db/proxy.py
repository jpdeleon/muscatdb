"""Closed reverse-proxy gateway for locally managed companion applications.

Destinations are selected exclusively from :data:`APPLICATIONS`; request data
can never select a host.  Companion applications must be prefix-aware (usually
through ``X-Forwarded-Prefix``/WSGI ``SCRIPT_NAME``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from muscat_db.auth import trusted_forwarded_user

logger = logging.getLogger(__name__)
router = APIRouter()

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})
_FORWARDED = frozenset({
    "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-port",
    "x-forwarded-prefix", "x-forwarded-proto", "x-forwarded-user",
})


@dataclass(frozen=True, slots=True)
class GatewayApplication:
    """An administrator-defined gateway destination."""

    name: str
    prefix: str
    backend: str
    allowed_methods: frozenset[str]
    websocket: bool = False
    timeout_s: float = 60.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.backend)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{self.name}: backend must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError(f"{self.name}: backend cannot contain credentials, query, or fragment")
        if not self.prefix.startswith("/") or self.prefix == "/" or self.prefix.endswith("/"):
            raise ValueError(f"{self.name}: prefix must be an absolute, non-root path without trailing slash")
        if not self.allowed_methods or any(m != m.upper() for m in self.allowed_methods):
            raise ValueError(f"{self.name}: allowed methods must be uppercase")


def _quicklook_backend() -> str:
    value = os.environ.get("MUSCAT_QUICKLOOK_URL", "http://127.0.0.1:5000").rstrip("/")
    parsed = urlsplit(value)
    # This integration is deliberately local-only.  A future remote app needs
    # a separate, reviewed registry entry rather than an operator-supplied URL.
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("MUSCAT_QUICKLOOK_URL must point to a loopback host")
    return value


APPLICATIONS = MappingProxyType({
    "quicklook": GatewayApplication(
        name="quicklook",
        prefix="/tess-quicklook",
        backend=_quicklook_backend(),
        allowed_methods=frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}),
        websocket=True,
    ),
})


# A single client pools connections to the loopback backend(s) for the whole
# app lifetime; it is opened and closed by the FastAPI lifespan (muscat_db.web).
# Building a client per request opens a fresh TCP connection every time and, on
# ASGI servers that skip a response's BackgroundTask when the browser
# disconnects mid-stream, leaks the upstream connection on the single event loop.
_client: httpx.AsyncClient | None = None


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=False,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    )


async def startup() -> None:
    """Open the shared upstream client. Idempotent; called from the lifespan."""
    global _client
    if _client is None:
        _client = _build_client()


async def shutdown() -> None:
    """Close the shared upstream client. Idempotent; called from the lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Safety net for contexts that bypass the lifespan (e.g. direct-endpoint
        # tests); normal startup pre-creates the client.
        _client = _build_client()
    return _client


def _request_headers(request: Request, app: GatewayApplication) -> dict[str, str]:
    excluded = _HOP_BY_HOP | _FORWARDED | {"host", "content-length"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded}
    headers.update({
        "X-Forwarded-Prefix": app.prefix,
        "X-Forwarded-Proto": request.url.scheme,
        "X-Forwarded-Host": request.headers.get("host", request.url.netloc),
    })
    if request.client:
        headers["X-Forwarded-For"] = request.client.host
    # Any client-supplied X-Forwarded-User was already dropped above (it is in
    # _FORWARDED), so this is the sole, authoritative source of the header.
    if user := getattr(request.state, "user", None):
        headers["X-Forwarded-User"] = user
    return headers


def _response_headers(headers: httpx.Headers) -> list[tuple[bytes, bytes]]:
    # Work on the raw bytes to preserve exact values and repeated headers.
    # httpx.Headers.items() comma-joins duplicates, which corrupts multiple
    # Set-Cookie headers (and any single cookie whose Expires contains a comma).
    excluded = _HOP_BY_HOP | {"content-length"}
    return [
        (name, value) for name, value in headers.raw
        if name.decode("latin-1").lower() not in excluded
    ]


def _upstream_url(app: GatewayApplication, path: str, query: str = "") -> str:
    base = app.backend.rstrip("/")
    return f"{base}/{path.lstrip('/')}" + (f"?{query}" if query else "")


def _rewrite_location(location: str, app: GatewayApplication) -> str:
    """Keep redirects into this backend inside its public gateway prefix."""
    target = urlsplit(location)
    backend = urlsplit(app.backend)
    if not target.netloc or (target.scheme, target.netloc) == (backend.scheme, backend.netloc):
        path = target.path if target.path.startswith("/") else "/" + target.path
        return urlunsplit(("", "", app.prefix + path, target.query, target.fragment))
    return location


async def _close_upstream(response: httpx.Response) -> None:
    # Close only the streamed response; the client is shared and outlives it.
    await response.aclose()


async def _proxy_http(request: Request, app: GatewayApplication, path: str):
    # Fail closed: the companion app (mutating endpoints, possibly a debug
    # console) must never be reachable without an authenticated user. The HTTP
    # middleware has already validated request.state.user against the loopback
    # trust rule, so a None here means the caller is unauthenticated.
    if not (getattr(request.state, "user", None) or None):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    if request.method not in app.allowed_methods:
        raise HTTPException(status_code=405, detail="method not allowed for gateway application")
    client = _get_client()
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                _upstream_url(app, path, request.url.query),
                headers=_request_headers(request, app),
                content=request.stream(),
                # Short connect/pool waits suit a loopback backend; the read
                # timeout stays app.timeout_s so slow responses can still stream.
                timeout=httpx.Timeout(app.timeout_s, connect=2.0, pool=2.0),
            ),
            stream=True,
        )
    except httpx.HTTPError:
        logger.exception("gateway connection failed for %s", app.name)
        return JSONResponse({"detail": f"{app.name} backend unavailable"}, status_code=502)

    raw_headers = [
        (name, _rewrite_location(value.decode("latin-1"), app).encode("latin-1"))
        if name.lower() == b"location" else (name, value)
        for name, value in _response_headers(upstream.headers)
    ]
    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(_close_upstream, upstream),
    )
    # Assign raw headers directly so repeated headers (e.g. Set-Cookie) survive;
    # a dict/Mapping would collapse them.
    response.raw_headers = raw_headers
    return response


@router.api_route("/tess-quicklook", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@router.api_route("/tess-quicklook/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def quicklook_http(request: Request, path: str = ""):
    return await _proxy_http(request, APPLICATIONS["quicklook"], path)


def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """Reject cross-site WebSocket hijacking (CSWSH).

    Browsers always send an ``Origin`` on a WS handshake and resend any ambient
    cookies, so an unauthenticated cross-site page could otherwise open this
    socket as the victim. Require Origin to match the Host the client connected
    to -- the same same-origin stance the app already takes for state-changing
    HTTP routes (``web._is_same_origin``). A missing Origin is treated as
    untrusted.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    return urlsplit(origin).netloc == websocket.headers.get("host", "")


@router.websocket("/tess-quicklook/{path:path}")
async def quicklook_websocket(websocket: WebSocket, path: str):
    """Bridge a registered WebSocket while preventing arbitrary destinations."""
    app = APPLICATIONS["quicklook"]
    if not app.websocket:
        await websocket.close(code=1008)
        return
    if not _ws_origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    # HTTP middleware does not run for WebSocket scopes, so recompute the trusted
    # user from the handshake and fail closed if the caller is unauthenticated.
    user = trusted_forwarded_user(
        websocket.headers.get("x-forwarded-user"),
        websocket.client.host if websocket.client else None,
    )
    if not user:
        await websocket.close(code=1008)
        return

    # Import lazily so ordinary HTTP-only deployments do not pay startup cost.
    from websockets.asyncio.client import connect

    backend = urlsplit(_upstream_url(app, path, websocket.url.query))
    scheme = "wss" if backend.scheme == "https" else "ws"
    target = urlunsplit((scheme, backend.netloc, backend.path, backend.query, ""))
    excluded = _HOP_BY_HOP | _FORWARDED | {"host", "content-length", "sec-websocket-key", "sec-websocket-version"}
    headers = [(k, v) for k, v in websocket.headers.items() if k.lower() not in excluded]
    headers.append(("X-Forwarded-Prefix", app.prefix))
    headers.append(("X-Forwarded-User", user))

    try:
        async with connect(target, additional_headers=headers, open_timeout=10) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def browser_to_backend():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close(code=message.get("code", 1000))
                        return
                    data = message.get("bytes") if message.get("bytes") is not None else message.get("text")
                    await upstream.send(data)

            async def backend_to_browser():
                async for data in upstream:
                    await websocket.send_bytes(data) if isinstance(data, bytes) else await websocket.send_text(data)

            tasks = [asyncio.create_task(browser_to_backend()), asyncio.create_task(backend_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("gateway websocket failed for %s", app.name)
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close(code=1011)
