import asyncio

import httpx
import pytest
from fastapi import Request
from starlette.websockets import WebSocket

import muscat_db.proxy as proxy
from muscat_db.auth import trusted_forwarded_user
from muscat_db.proxy import (
    APPLICATIONS,
    GatewayApplication,
    _proxy_http,
    _request_headers,
    _response_headers,
    _rewrite_location,
    _upstream_url,
    _ws_origin_allowed,
)


def test_registry_is_closed_and_quicklook_is_loopback_only():
    assert set(APPLICATIONS) == {"quicklook"}
    assert url_host(APPLICATIONS["quicklook"].backend) in {"127.0.0.1", "::1", "localhost"}
    with pytest.raises(TypeError):
        APPLICATIONS["evil"] = APPLICATIONS["quicklook"]


def url_host(url):
    return httpx.URL(url).host


def test_application_rejects_unsafe_registry_entries():
    with pytest.raises(ValueError):
        GatewayApplication("bad", "/bad", "file:///etc/passwd", frozenset({"GET"}))
    with pytest.raises(ValueError):
        GatewayApplication("bad", "/bad/", "http://127.0.0.1", frozenset({"GET"}))
    with pytest.raises(ValueError):
        GatewayApplication("bad", "/bad", "http://user:pass@localhost", frozenset({"GET"}))


def test_upstream_url_cannot_change_registered_origin():
    app = APPLICATIONS["quicklook"]
    result = httpx.URL(_upstream_url(app, "//attacker.example/x", "a=1"))
    assert result.host == url_host(app.backend)
    assert result.path == "/attacker.example/x"


def test_forwarding_headers_are_replaced_not_trusted():
    scope = {
        "type": "http", "method": "GET", "scheme": "https", "path": "/",
        "query_string": b"", "server": ("science.example", 443),
        "client": ("192.0.2.4", 1234),
        "headers": [
            (b"host", b"science.example"),
            (b"x-forwarded-for", b"10.0.0.1"),
            (b"x-forwarded-prefix", b"/evil"),
            (b"connection", b"keep-alive"),
            (b"cookie", b"session=yes"),
        ],
        "state": {"user": "observer"},
    }
    headers = _request_headers(Request(scope), APPLICATIONS["quicklook"])
    assert headers["X-Forwarded-For"] == "192.0.2.4"
    assert headers["X-Forwarded-Prefix"] == "/tess-quicklook"
    assert headers["X-Forwarded-User"] == "observer"
    assert "connection" not in headers
    assert headers["cookie"] == "session=yes"


@pytest.mark.parametrize("location, expected", [
    ("/jobs", "/tess-quicklook/jobs"),
    ("jobs?id=1", "/tess-quicklook/jobs?id=1"),
    ("http://127.0.0.1:5000/jobs", "/tess-quicklook/jobs"),
    ("https://example.org/docs", "https://example.org/docs"),
])
def test_redirect_rewriting(location, expected):
    assert _rewrite_location(location, APPLICATIONS["quicklook"]) == expected


def _http_scope(headers, user=None):
    scope = {
        "type": "http", "method": "GET", "scheme": "https", "path": "/",
        "query_string": b"", "server": ("science.example", 443),
        "client": ("192.0.2.4", 1234),
        "headers": headers,
    }
    if user is not None:
        scope["state"] = {"user": user}
    return scope


def test_inbound_x_forwarded_user_is_stripped_when_unauthenticated():
    # A client that forges the identity header with no authenticated user must
    # not have it reach the trusted backend.
    scope = _http_scope([(b"host", b"science.example"), (b"x-forwarded-user", b"admin")])
    headers = _request_headers(Request(scope), APPLICATIONS["quicklook"])
    assert "X-Forwarded-User" not in headers
    assert "x-forwarded-user" not in {k.lower() for k in headers}


def test_authenticated_user_replaces_inbound_spoof_without_duplication():
    scope = _http_scope(
        [(b"host", b"science.example"), (b"x-forwarded-user", b"attacker")],
        user="observer",
    )
    headers = _request_headers(Request(scope), APPLICATIONS["quicklook"])
    forwarded = [v for k, v in headers.items() if k.lower() == "x-forwarded-user"]
    assert forwarded == ["observer"]


def _ws(headers):
    scope = {"type": "websocket", "path": "/tess-quicklook/ws", "headers": headers}
    return WebSocket(scope, receive=None, send=None)


@pytest.mark.parametrize("origin_headers, allowed", [
    ([(b"host", b"science.example"), (b"origin", b"https://science.example")], True),
    ([(b"host", b"science.example"), (b"origin", b"https://evil.example")], False),
    ([(b"host", b"science.example")], False),  # missing Origin is untrusted
])
def test_ws_origin_allowed(origin_headers, allowed):
    assert _ws_origin_allowed(_ws(origin_headers)) is allowed


@pytest.mark.parametrize("forwarded, client_host, expected", [
    ("observer", "127.0.0.1", "observer"),   # trusted loopback proxy
    ("observer", "::1", "observer"),          # trusted loopback proxy (v6)
    ("admin", "192.0.2.4", None),             # non-loopback peer: impersonation
    ("admin", None, None),                    # unknown peer: not trusted
    (None, "127.0.0.1", None),                # no header, nothing to trust
    ("", "127.0.0.1", None),                  # empty header
])
def test_trusted_forwarded_user(forwarded, client_host, expected):
    assert trusted_forwarded_user(forwarded, client_host) == expected


def _authed_http_scope(user):
    scope = {
        "type": "http", "method": "GET", "scheme": "https", "path": "/",
        "query_string": b"", "server": ("science.example", 443),
        "client": ("192.0.2.4", 1234),
        "headers": [(b"host", b"science.example")],
    }
    if user is not None:
        scope["state"] = {"user": user}
    return scope


def test_gateway_rejects_unauthenticated_http_request():
    # No authenticated user -> the gateway must not proxy to the companion app.
    resp = asyncio.run(_proxy_http(Request(_authed_http_scope(None)), APPLICATIONS["quicklook"], ""))
    assert resp.status_code == 401


def test_gateway_lets_authenticated_user_reach_backend():
    # With a user present, the auth gate passes; the request reaches the client
    # and (against a closed loopback backend) surfaces as a 502, not a 401.
    app = GatewayApplication(
        name="closed", prefix="/closed", backend="http://127.0.0.1:5999",
        allowed_methods=frozenset({"GET"}), timeout_s=2.0,
    )
    scope = _authed_http_scope("observer")

    async def run():
        request = Request(scope, receive=_empty_body_receive)
        return await _proxy_http(request, app, "")

    resp = asyncio.run(run())
    assert resp.status_code == 502


async def _empty_body_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def test_response_headers_preserve_duplicate_set_cookie_and_drop_hop_by_hop():
    upstream = httpx.Headers([
        ("set-cookie", "a=1; Path=/"),
        ("set-cookie", "b=2; Expires=Wed, 09 Jun 2027 10:18:14 GMT"),
        ("content-type", "text/html"),
        ("content-length", "10"),
        ("connection", "keep-alive"),
        ("transfer-encoding", "chunked"),
    ])
    result = _response_headers(upstream)
    cookies = [v for k, v in result if k.lower() == b"set-cookie"]
    assert cookies == [b"a=1; Path=/", b"b=2; Expires=Wed, 09 Jun 2027 10:18:14 GMT"]
    names = {k.lower() for k, _ in result}
    assert b"content-length" not in names
    assert b"connection" not in names  # hop-by-hop stripped
    assert b"transfer-encoding" not in names
    assert (b"content-type", b"text/html") in result


def test_shared_client_startup_and_shutdown_are_idempotent():
    async def scenario():
        await proxy.shutdown()
        assert proxy._client is None
        await proxy.startup()
        first = proxy._client
        assert isinstance(first, httpx.AsyncClient)
        await proxy.startup()  # idempotent: does not replace the live client
        assert proxy._client is first
        await proxy.shutdown()
        assert proxy._client is None
        await proxy.shutdown()  # idempotent when already closed
        assert first.is_closed

    asyncio.run(scenario())
