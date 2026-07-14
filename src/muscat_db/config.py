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
        "MUSCAT_TMPDIR",
        str(Path.home() / "temp"),
        "Temp dir handed to spawned jobs (must be on a non-full filesystem)",
    ),
    EnvVar("MUSCAT_PHOT_STALL_LIMIT_S", "1500", "Photometry job stall timeout (seconds)"),
    EnvVar("MUSCAT_PHOT_MAX_RUNTIME_S", "10800", "Photometry job max runtime (seconds)"),
    EnvVar("MUSCAT_PHOT_FINALIZE_GRACE_S", "8", "Log-quiescence grace window (seconds)"),
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
    EnvVar(
        "MUSCAT_LCO_ALLOW_SUBMIT",
        "0",
        "Server-side safety gate for live LCO observation submission. While '0' "
        "(default), /api/lco/submit refuses even with a valid dry-run + confirm; "
        "set to '1' only when intentionally going live.",
    ),
    EnvVar(
        "ADS_API_TOKEN",
        None,
        "NASA ADS API Token (used to query published papers about the target)",
        secret=True,
    ),
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
