"""Shared HTTP clients for outbound archive/catalog network calls.

Replaces the ad hoc ``urllib.request.urlopen`` calls (inconsistent 1s-60s
timeouts, a fresh TCP connection per request) scattered across catalog.py and
web.py with two pooled httpx clients (architecture audit finding C2):

- The async client backs routes whose entire job is a single external call
  (ExoFOP, NASA/TOI TAP, ADS) — using it lets FastAPI await the I/O instead of
  occupying a threadpool slot for the whole request.
- The sync client backs catalog lookups that are invoked from routes doing
  substantial synchronous local DB/job-store work in the same handler (the
  /target page's HARPS RVBank fallback, api_lco_windows, api_ephemeris_target
  _info). Those routes must stay plain ``def`` (tests/test_route_concurrency.py
  pins this), so there is nothing to await there; the sync client still gets
  them off raw urllib and onto one pooled, consistently-timed-out client.

Both clients are opened/closed by the FastAPI lifespan (muscat_db.web),
mirroring muscat_db.proxy's client. A lazy-build safety net covers contexts
that bypass the lifespan (e.g. direct-endpoint tests that never enter
TestClient as a context manager).
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = float(os.environ.get("MUSCAT_ARCHIVE_TIMEOUT_S", "15.0"))
USER_AGENT = "muscat-db/0.1.0"

_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=8)

_async_client: httpx.AsyncClient | None = None
_sync_client: httpx.Client | None = None


def _build_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT_S,
        limits=_LIMITS,
    )


def _build_sync_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT_S,
        limits=_LIMITS,
    )


async def startup() -> None:
    """Open the shared async client. Idempotent; called from the lifespan."""
    global _async_client
    if _async_client is None:
        _async_client = _build_async_client()


async def shutdown() -> None:
    """Close both shared clients. Idempotent; called from the lifespan."""
    global _async_client, _sync_client
    if _async_client is not None:
        try:
            await asyncio.wait_for(_async_client.aclose(), timeout=2.0)
        except (TimeoutError, Exception) as e:
            logger.warning("archive async client shutdown timeout or error: %s", e)
        _async_client = None
    if _sync_client is not None:
        try:
            _sync_client.close()
        except Exception as e:
            logger.warning("archive sync client shutdown error: %s", e)
        _sync_client = None


def get_async_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None:
        # Safety net for contexts that bypass the lifespan (e.g. direct-endpoint
        # tests); normal startup pre-creates the client.
        _async_client = _build_async_client()
    return _async_client


def get_sync_client() -> httpx.Client:
    global _sync_client
    if _sync_client is None:
        _sync_client = _build_sync_client()
    return _sync_client
