"""Shared reverse-proxy authentication trust rule.

nginx performs HTTP Basic Auth and forwards the authenticated username in an
``X-Forwarded-User`` header. Trusting that header is ONLY safe for connections
that actually arrived from nginx's own loopback socket: uvicorn's default bind
is ``0.0.0.0``, so without this check any network client could set the header
itself and impersonate a user. We therefore verify the immediate TCP peer is
loopback before honoring it, rather than relying on the operator having
remembered ``--nginx`` at start time.

This does not defend against another local account on the same host connecting
straight to uvicorn's loopback port; that would require a shared secret between
nginx and uvicorn, which is not implemented yet.

The rule lives here (not inline in the HTTP middleware) because both the
middleware and the companion-app gateway (:mod:`muscat_db.proxy`) must apply it
identically -- and the gateway's WebSocket route bypasses HTTP middleware
entirely, so it has to recompute the trusted user from the handshake itself.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

# Hosts whose X-Forwarded-User we trust: nginx's own loopback socket.
TRUSTED_PROXY_HOSTS = frozenset({"127.0.0.1", "::1"})


def trusted_forwarded_user(
    forwarded_user: str | None, client_host: str | None
) -> str | None:
    """Return the authenticated username, or ``None`` if it cannot be trusted.

    ``forwarded_user`` is the raw ``X-Forwarded-User`` header value (may be
    ``None`` or empty). It is honored only when ``client_host`` is a loopback
    proxy address; a non-loopback peer that sets the header itself is ignored.
    """
    user = forwarded_user or None
    if user is not None and client_host not in TRUSTED_PROXY_HOSTS:
        return None
    return user


def request_user(request: Request) -> str | None:
    """Return the authenticated username the proxy middleware attached to
    ``request.state.user``, or ``None`` when the request is unauthenticated."""
    return getattr(request.state, "user", None) or None


def settings_auth_error() -> JSONResponse:
    """401 response for per-user settings endpoints when nginx auth is absent."""
    return JSONResponse(
        {
            "ok": False,
            "error": "login required",
            "detail": "Per-user settings require nginx authentication.",
        },
        status_code=401,
    )


def is_same_origin(request: Request) -> bool:
    """True if the request's Origin (or Referer) header matches this host.

    HTTP Basic Auth credentials are resent by the browser automatically on
    every request to the realm, so state-changing endpoints need their own
    CSRF defense. A CORS preflight is not sufficient here: FastAPI's
    ``Body(...)`` parses the request body as JSON regardless of the
    Content-Type the client declared, so a cross-origin "simple request"
    (e.g. Content-Type: text/plain, which browsers don't preflight) would
    still reach the handler with an attacker-controlled body.
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    return urlsplit(origin).netloc == request.headers.get("host", "")


def csrf_error() -> JSONResponse:
    """403 response for state-changing endpoints that fail the same-origin check."""
    return JSONResponse({"ok": False, "error": "cross-origin request rejected"}, status_code=403)
