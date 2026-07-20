"""Single source of truth for environment configuration.

Every environment variable the muscat-db + prose2 pipeline consults is listed in
``ENV_VARS`` below, so there is one place to look when wiring up a new machine.
The actual ``.env`` loading happens in ``muscat_db/__init__.py`` (early, before
submodules read ``os.environ``); this module documents the variables and reports
their status. See ``.env.example`` at the repo root for an annotated template.

Note: the env *getters* with their defaults live next to the code that uses them
(``photometry.py``, ``database.py``, ``transit_fit.py``, ``ttv_fit.py``). This
registry mirrors those names/defaults for documentation and the startup status
report; it does not replace them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EnvVar:
    name: str
    default: str | None
    purpose: str
    secret: bool = False


# Canonical registry of every variable the pipeline reads. ``default=None`` means
# the code has no fallback (the feature is unavailable / errors when it is needed).
ENV_VARS: tuple[EnvVar, ...] = (
    EnvVar("MUSCAT_DB_PATH", "muscat.db", "SQLite database path"),
    EnvVar(
        "MUSCAT_DB_SECRET",
        None,
        "Server secret used to encrypt per-user settings such as LCO API tokens. "
        "Keep stable across restarts; changing it makes stored tokens unreadable.",
        secret=True,
    ),
    EnvVar(
        "MUSCAT_DATA_DIR",
        "/data",
        "Common raw FITS root containing MuSCAT, MuSCAT2, MuSCAT3, MuSCAT4, and Sinistro",
    ),
    EnvVar(
        "MUSCAT_OBSLOG_DIR",
        str(Path.home() / "muscat" / "obslog"),
        "Obslog CSV base shared by muscat-db and prose2",
    ),
    EnvVar(
        "MUSCAT_PROSE_DIR",
        str(Path.home() / "ql" / "prose"),
        "Pipeline output base directory",
    ),
    EnvVar("MUSCAT_PROSE_PROJECT", "<repo>/../ext_tools/prose2", "prose2 repository path"),
    EnvVar("MUSCAT_PROSE_PYTHON", None, "Explicit prose interpreter (highest priority)"),
    EnvVar("MUSCAT_PROSE_CONDA_ENV", "prose", "Conda env supplying prose dependencies"),
    EnvVar(
        "MUSCAT_TIMER_DIR",
        str(Path.home() / "ql" / "timer"),
        "timer package output directory",
    ),
    EnvVar(
        "MUSCAT_TTV_DIR",
        str(Path.home() / "ql" / "harmonic"),
        "harmonic package output directory (TTV fits, keyed on target)",
    ),
    EnvVar(
        "MUSCAT_QUICKLOOK_URL",
        "http://127.0.0.1:5000",
        "Loopback backend for the TESS QuickLook companion application",
    ),
    EnvVar(
        "MUSCAT_BOYLE_CATALOG",
        "<repo>/data/Boyle2026/final_catalog.feather",
        "Optional Boyle2026 stellar-rotation catalog used by the TOI browser",
    ),
    EnvVar(
        "MUSCAT_TMPDIR",
        str(Path.home() / "temp"),
        "Temp dir handed to spawned jobs (must be on a non-full filesystem)",
    ),
    EnvVar("MUSCAT_PHOT_STALL_LIMIT_S", "1500", "Photometry job stall timeout (seconds)"),
    EnvVar("MUSCAT_PHOT_MAX_RUNTIME_S", "10800", "Photometry job max runtime (seconds)"),
    EnvVar("MUSCAT_PHOT_FINALIZE_GRACE_S", "8", "Log-quiescence grace window (seconds)"),
    EnvVar("MUSCAT_JOB_RECONCILE_INTERVAL_S", "2", "Server-side job reconciliation cadence (seconds)"),
    EnvVar("MUSCAT_EXPOSURE_CALIBRATION_WORKERS", "2", "Global exposure-calibration workers"),
    EnvVar("MUSCAT_EXPOSURE_CALIBRATION_STALE_S", "21600", "Abandoned calibration claim timeout (seconds)"),
    EnvVar("MUSCAT_CATALOG_GLOBAL_WORKERS", "8", "Process-wide outbound catalog concurrency"),
    EnvVar("MUSCAT_CATALOG_BATCH_MAX_ACTIVE", "4", "Concurrent catalog batch requests"),
    EnvVar("MUSCAT_CATALOG_BATCH_MAX_ITEMS", "200", "Stars allowed per catalog batch"),
    EnvVar("MUSCAT_CATALOG_BATCH_MAX_BYTES", "262144", "Serialized bytes allowed per catalog batch"),
    EnvVar("MUSCAT_ZIP_BUILD_WORKERS", "1", "Concurrent output ZIP builders"),
    EnvVar("MUSCAT_ZIP_MAX_FILES", "10000", "Files allowed per output ZIP"),
    EnvVar("MUSCAT_ZIP_MAX_INPUT_BYTES", str(2 << 30), "Uncompressed input bytes allowed per output ZIP"),
    EnvVar("MUSCAT_ZIP_FREE_RESERVE_BYTES", str(5 << 30), "Free bytes preserved on the ZIP temp filesystem"),
    EnvVar("MUSCAT_ZIP_CACHE_TTL_S", "900", "Output ZIP manifest-cache lifetime (seconds)"),
    EnvVar(
        "ASTROMETRY_NET_API_KEY",
        None,
        "nova.astrometry.net WCS solving for muscat/muscat2 calibration "
        "(--wcs_method astrometry.net; not needed with --wcs_method twirl, "
        "or for BANZAI muscat3/muscat4/sinistro)",
        secret=True,
    ),
    EnvVar(
        "LCO_API_TOKEN",
        None,
        "LCO Observation Portal API token (live scheduling / IPP dry-run on the /lco page). "
        "Server-side only; never sent to the browser.",
        secret=True,
    ),
    EnvVar(
        "MUSCAT_LCO_DIR",
        None,
        "Root directory for LCO archive downloads (<root>/<instrument>/<date>/). "
        "Falls back to MUSCAT_DATA_DIR's per-instrument layout when unset.",
    ),
    EnvVar("MUSCAT_LCO_ARCHIVE_DOWNLOAD_WORKERS", "1", "Concurrent LCO background download jobs"),
    EnvVar("MUSCAT_LCO_ARCHIVE_DOWNLOAD_FRAME_WORKERS", "8", "Frame workers within one LCO download"),
    EnvVar("MUSCAT_LCO_ARCHIVE_FUNPACK_WORKERS", "2", "Concurrent LCO funpack subprocesses"),
    EnvVar("MUSCAT_LCO_ARCHIVE_MAX_FRAMES", "500", "Frames allowed per LCO archive request"),
    EnvVar("MUSCAT_LCO_ARCHIVE_MAX_PAYLOAD_BYTES", "2097152", "Serialized bytes allowed per LCO archive request"),
    EnvVar("MUSCAT_LCO_ARCHIVE_FOREGROUND_MAX_FRAMES", "10", "Frames allowed in a foreground LCO download"),
    EnvVar("MUSCAT_LCO_ARCHIVE_MAX_ACTIVE_PER_USER", "2", "Active LCO archive jobs allowed per user"),
    EnvVar("MUSCAT_LCO_ARCHIVE_DOWNLOAD_MAX_JOBS", "200", "Tracked LCO archive queue/history entries"),
    EnvVar(
        "MUSCAT_LCO_ALLOW_SUBMIT",
        "0",
        "Server-side safety gate for live LCO observation submission. While '0' "
        "(default), /api/lco/submit refuses even with a valid dry-run + confirm; "
        "set to '1' only when intentionally going live.",
    ),
    EnvVar("MUSCAT_LCO_MONITOR_ENABLED", "1", "Run the restart-safe submitted-request monitor"),
    EnvVar("MUSCAT_LCO_MONITOR_POLL_S", "300", "Initial LCO request/archive polling interval (seconds)"),
    EnvVar("MUSCAT_LCO_MONITOR_FAST_AFTER_WINDOW_S", "7200", "Keep the initial cadence this long after a request window (seconds)"),
    EnvVar("MUSCAT_LCO_MONITOR_MAX_POLL_S", "3600", "Maximum unchanged-result polling interval (seconds)"),
    EnvVar("MUSCAT_LCO_MONITOR_ERROR_MAX_POLL_S", "3600", "Maximum API-error retry interval (seconds)"),
    EnvVar("MUSCAT_LCO_MONITOR_LOOP_S", "15", "Local due-request monitor loop interval (seconds)"),
    EnvVar("MUSCAT_LCO_MONITOR_BATCH_SIZE", "2", "Maximum due requests advanced per local monitor loop"),
    EnvVar("MUSCAT_LCO_MONITOR_DOWNLOAD_CHECK_S", "10", "Active archive-download status interval (seconds)"),
    EnvVar("MUSCAT_LCO_MONITOR_NO_DATA_GRACE_S", "86400", "Terminal-request archive-lag grace period (seconds)"),
    EnvVar("MUSCAT_LCO_MONITOR_LEASE_S", "90", "Cross-worker observation-monitor lease duration (seconds)"),
    EnvVar("MUSCAT_LCO_MONITOR_SCAN_WORKERS", "1", "FITS header workers used by automatic LCO scans"),
    EnvVar(
        "ADS_API_TOKEN",
        None,
        "NASA ADS API Token (used to query published papers about the target)",
        secret=True,
    ),
    EnvVar(
        "ESO_USERNAME",
        None,
        "ESO archive username — global server-side fallback for ESO TAP queries. "
        "Per-user credentials (saved in Settings) take precedence. "
        "Anonymous queries are used when neither is set.",
        secret=False,
    ),
    EnvVar(
        "ESO_PASSWORD",
        None,
        "ESO archive password — global server-side fallback for ESO TAP queries. "
        "Keep paired with ESO_USERNAME.",
        secret=True,
    ),
    EnvVar(
        "MUSCAT_CHAT_AGENT_NAME",
        "bot",
        "The @name that invokes the codebase assistant in team chat (e.g. @bot).",
    ),
    EnvVar(
        "MUSCAT_OLLAMA_URL",
        "http://muscat-ut4.c.u-tokyo.ac.jp:11434",
        "Base URL of the ollama server backing the chat assistant (its /api/chat "
        "endpoint). Requires ollama bound to 0.0.0.0 and the port reachable from "
        "this host.",
    ),
    EnvVar(
        "MUSCAT_OLLAMA_MODEL",
        "gemma4:latest",
        "Ollama model tag the chat assistant runs.",
    ),
    EnvVar("MUSCAT_OLLAMA_TIMEOUT_S", "120", "Per-request generation timeout for the chat assistant (seconds)"),
    EnvVar("MUSCAT_OLLAMA_MAX_CONCURRENT", "2", "Concurrent chat-assistant requests before callers get a 'busy' note"),
)


def status_of(var: EnvVar) -> str:
    """Return 'set', 'default', or 'unset' for a single variable."""
    raw = os.environ.get(var.name)
    if raw not in (None, ""):
        return "set"
    return "default" if var.default is not None else "unset"


def config_status() -> list[tuple[str, str]]:
    """Return ``(name, status)`` for each known variable, registry order."""
    return [(v.name, status_of(v)) for v in ENV_VARS]


def missing_required_secret() -> EnvVar | None:
    """The astrometry key is only *conditionally* required (muscat/muscat2 + astrometry.net),
    so this never hard-fails the app. prose2 enforces it at calibration time and
    points the user at ``--wcs_method twirl``. Returned here only for a warning."""
    for v in ENV_VARS:
        if v.name == "ASTROMETRY_NET_API_KEY" and status_of(v) == "unset":
            return v
    return None
