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
