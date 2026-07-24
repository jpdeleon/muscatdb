# src/muscat_db/lco.py
"""
Helper module for interacting with the LCO API.
"""
from __future__ import annotations

import datetime
import concurrent.futures
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from muscat_db.catalog import _angular_sep_arcsec, _normalize_target_name
from muscat_db.coord import (
    CoordRepr,
    unpack as _unpack_coord,
    clean_ra as _clean_ra,
    clean_dec as _clean_dec,
)
from muscat_db.database import (
    UserSettingsError,
    db_path as _db_path,
    get_conn,
    get_user_lco_token,
    user_lco_token_configured,
)
from muscat_db.instruments import INSTRUMENTS

logger = logging.getLogger(__name__)

# A frame filename / path segment: letters, digits and the punctuation LCO uses
# in archive names. Excludes "/" and "\" so a crafted payload can't traverse.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._+:\-]+$")
_DOWNLOAD_INSTRUMENT_DIRS = {
    "sinistro": "Sinistro",
    "muscat": "MuSCAT",
    "muscat2": "MuSCAT2",
    "muscat3": "MuSCAT3",
    "muscat4": "MuSCAT4",
}
_DOWNLOAD_HOSTS = frozenset({
    "archive-api.lco.global",
    "archive-lco-global.s3.amazonaws.com",
})

# Secondary-mirror defocus offset limits (mm), from LCO's live instrument
# capabilities schema (observe.lco.global/api/instruments/): the InstrumentConfig
# "defocus" extra_param is capped at +/-8mm for 2M0-SCICAM-MUSCAT and +/-5mm for
# 1M0-SCICAM-SINISTRO.
_DEFOCUS_LIMIT_MM = {
    "muscat": 8.0,
    "muscat3": 8.0,
    "muscat4": 8.0,
    "sinistro": 5.0,
}


def _validated_defocus(params: dict, limit_mm: float) -> float:
    """Parse and range-check the secondary-mirror defocus offset (mm)."""
    raw = params.get("defocus")
    if raw in (None, ""):
        return 0.0
    try:
        defocus = float(raw)
    except (TypeError, ValueError):
        raise LcoError("Defocus must be a number in mm", status=400)
    if abs(defocus) > limit_mm:
        raise LcoError(
            f"Defocus must be within ±{limit_mm:g}mm (got {defocus:g}mm)",
            status=400,
        )
    return defocus


class LcoError(Exception):
    """Structured LCO API error."""

    def __init__(self, message: str, status: int = 500, detail: str | None = None):
        self.message = message
        self.status = status
        self.detail = detail
        super().__init__(f"[{status}] {message}" + (f" - {detail}" if detail else ""))

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.message, "detail": self.detail, "status": self.status}


def _get_lco_api_token(user_name: str | None = None, *, require_own_token: bool = False) -> str:
    """Return the LCO API token for *user_name*.

    A logged-in (nginx-authenticated) user acts under their *own* LCO account:
    telescope-time bookings, proposal listings, and IPP validation all run under
    whichever token authenticates the request. So when ``require_own_token`` is
    set — the identity-bearing portal calls (proposals, requestgroups, IPP,
    submit) — an authenticated user with no saved token is refused rather than
    silently borrowing the server's shared ``LCO_API_TOKEN`` (which belongs to
    the operator, not them). The global token remains the fallback only for
    unauthenticated/CLI callers (``user_name`` is ``None``) and for read-only
    archive access, which never carries the caller's identity.
    """
    if user_name:
        try:
            user_token = get_user_lco_token(user_name)
        except UserSettingsError as exc:
            raise LcoError(
                "Stored LCO token cannot be used",
                status=503,
                detail=str(exc),
            ) from exc
        if user_token:
            return user_token
        if require_own_token:
            raise LcoError(
                "Your LCO API token is not configured",
                status=403,
                detail=(
                    "This action runs under your own LCO account. Save your "
                    "personal LCO token in Settings; the server's shared token "
                    "is not used on behalf of logged-in users."
                ),
            )
    token = os.environ.get("LCO_API_TOKEN")
    if not token:
        raise LcoError(
            "LCO API token is not configured",
            status=503,
            detail=(
                "Save an LCO token in Settings for your logged-in user, "
                "or set the legacy LCO_API_TOKEN server secret."
            ),
        )
    return token


def config_state(user_name: str | None = None) -> dict:
    """Return the configuration state for LCO variables. No secrets exposed.

    Portal actions (proposals, IPP, submit) run under the caller's own LCO
    identity, so for a logged-in (authenticated) user the server's global token
    does not count toward ``token_configured``/``submit_allowed`` — only their
    saved per-user token does. Unauthenticated/CLI callers keep the global token
    as a valid source.
    """
    authenticated = bool((user_name or "").strip())
    user_token_configured = user_lco_token_configured(user_name)
    global_token_configured = bool(os.environ.get("LCO_API_TOKEN"))
    if authenticated:
        token_configured = user_token_configured
        token_source = "user" if user_token_configured else None
    else:
        token_configured = user_token_configured or global_token_configured
        token_source = "user" if user_token_configured else ("global" if global_token_configured else None)
    download_root_configured = bool(
        os.environ.get("MUSCAT_LCO_DIR") or os.environ.get("MUSCAT_DATA_DIR")
    )
    submit_flag_enabled = os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") == "1"
    root = download_root()
    return {
        "token_configured": token_configured,
        "user_token_configured": user_token_configured,
        "global_token_configured": global_token_configured,
        "token_source": token_source,
        "download_root_configured": download_root_configured,
        "download_root": str(root) if root else None,
        "submit_allowed": token_configured and download_root_configured and submit_flag_enabled,
    }


def _lco_api_request(
    url: str,
    method: str = "GET",
    data: dict | None = None,
    user_name: str | None = None,
    token: str | None = None,
    require_own_token: bool = False,
) -> dict:
    """Make an authenticated request to the LCO API.

    Both the observation portal (observe.lco.global) and the Science Archive
    (archive-api.lco.global) authenticate with the same DRF token using the
    ``Token`` scheme. Using ``Bearer`` makes the archive return HTTP 401
    ``{"detail": "No Such User"}``.

    ``require_own_token`` forbids the global-token fallback for an authenticated
    user (see :func:`_get_lco_api_token`); it is ignored when ``token`` is passed
    explicitly.
    """
    token = token or _get_lco_api_token(user_name, require_own_token=require_own_token)
    headers = {"Authorization": "Token " + token, "Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if 200 <= response.status < 300:
                return json.loads(response.read().decode())
            raise LcoError(
                f"LCO API returned HTTP {response.status}",
                status=response.status,
                detail=response.read().decode(),
            )
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()
        except Exception:
            detail = str(e)
        raise LcoError(f"LCO API request failed with HTTP {e.code}", status=e.code, detail=detail)
    except Exception as e:
        raise LcoError("LCO API request failed", detail=str(e))


def get_proposals(
    user_name: str | None = None,
    token: str | None = None,
    *,
    require_own_token: bool = True,
) -> dict:
    """Fetch the current user's active proposals."""
    return _lco_api_request(
        "https://observe.lco.global/api/proposals/?state=ACTIVE",
        user_name=user_name,
        token=token,
        require_own_token=require_own_token,
    )


def get_requestgroups(
    proposal: str,
    user_name: str | None = None,
    token: str | None = None,
    *,
    require_own_token: bool = True,
) -> dict:
    """Fetch request groups for a given proposal."""
    if not proposal:
        raise LcoError("Proposal ID is required", status=400)
    url = f"https://observe.lco.global/api/requestgroups/?proposal={urllib.parse.quote(proposal)}"
    return _lco_api_request(url, user_name=user_name, token=token, require_own_token=require_own_token)


def get_requestgroup(
    requestgroup_id: int,
    user_name: str | None = None,
    token: str | None = None,
    *,
    require_own_token: bool = True,
) -> dict:
    """Fetch one submitted request group, including current child states.

    The background monitor (:mod:`muscat_db.lco_monitor`) polls this for records
    it did not necessarily submit under a per-user token, so it opts out of the
    own-token requirement to keep legacy/global-submitted requests observable.
    """
    try:
        identifier = int(requestgroup_id)
    except (TypeError, ValueError) as exc:
        raise LcoError("Request-group ID must be numeric", status=400) from exc
    url = f"https://observe.lco.global/api/requestgroups/{identifier}/"
    return _lco_api_request(url, user_name=user_name, token=token, require_own_token=require_own_token)


def get_request(
    request_id: int,
    user_name: str | None = None,
    token: str | None = None,
    *,
    require_own_token: bool = True,
) -> dict:
    """Fetch one child Request, used to resolve a request id to its parent group.

    A user copying an ID from an LCO "Request Detail" page has the *request* id,
    not the requestgroup id. This endpoint carries the parent group id
    (``request_group``) so a clone can fall back from requestgroups to requests.
    """
    try:
        identifier = int(request_id)
    except (TypeError, ValueError) as exc:
        raise LcoError("Request ID must be numeric", status=400) from exc
    url = f"https://observe.lco.global/api/requests/{identifier}/"
    return _lco_api_request(url, user_name=user_name, token=token, require_own_token=require_own_token)


def _query_params(filters: dict) -> str:
    """Encode API filters without dropping meaningful zero/false values."""
    return urllib.parse.urlencode(
        {key: value for key, value in filters.items() if value is not None and value != ""}
    )


def archive_search(
    filters: dict,
    user_name: str | None = None,
    token: str | None = None,
) -> dict:
    """Search the LCO archive."""
    base_url = "https://archive-api.lco.global/frames/"
    params = _query_params(filters)
    url = f"{base_url}?{params}"
    return _lco_api_request(url, user_name=user_name, token=token)


# Safety cap so a single request-id fetch can't spin forever paginating a
# pathologically large observation request.
_ARCHIVE_MAX_FRAMES = 10_000


def archive_search_all(
    filters: dict,
    user_name: str | None = None,
    max_frames: int = _ARCHIVE_MAX_FRAMES,
    token: str | None = None,
) -> dict:
    """Search the LCO archive, following pagination until exhausted or capped.

    The archive paginates ``frames/`` results (``next`` holds the fully-formed
    next-page URL, already carrying the same query params). A single observation
    request can span thousands of frames, so ``archive_search`` (one page) is not
    enough to pull a whole dataset by ``request_id``. Stops at ``max_frames`` and
    reports ``truncated`` so the caller can warn the user.
    """
    base_url = "https://archive-api.lco.global/frames/"
    params = _query_params(filters)
    url: str | None = f"{base_url}?{params}"
    results: list[dict] = []
    total: int | None = None
    while url and len(results) < max_frames:
        page = _lco_api_request(url, user_name=user_name, token=token)
        if total is None:
            total = page.get("count")
        results.extend(page.get("results") or [])
        url = page.get("next")
    truncated = bool(url) and len(results) >= max_frames
    return {"count": total, "results": results[:max_frames], "truncated": truncated}


def infer_archive_instrument(frame: dict) -> str:
    """Infer the muscat-db instrument name from LCO archive frame metadata."""
    site = str(frame.get("SITEID") or frame.get("site_id") or "").lower()
    tel = str(frame.get("TELID") or frame.get("telescope_id") or "").lower()
    instrume = str(frame.get("INSTRUME") or frame.get("instrument_id") or "").lower()
    filename = str(frame.get("filename") or frame.get("basename") or "").lower()

    if not site and filename:
        if filename.startswith("ogg"):
            site = "ogg"
        elif filename.startswith("coj"):
            site = "coj"
        elif filename.startswith(("lsc", "cpt", "tfn", "elp")):
            site = filename[:3]

    if not tel and filename:
        if "2m0" in filename:
            tel = "2m0"
        elif "1m0" in filename:
            tel = "1m0"

    if not instrume and filename:
        if "-ep" in filename or "muscat" in filename:
            instrume = "muscat"
        elif "-fa" in filename or "-kb" in filename or "sinistro" in filename:
            instrume = "sinistro"

    if site == "ogg" and tel.startswith("2m0") and ("muscat" in instrume or "ep" in instrume):
        return "muscat3"
    if site == "coj" and tel.startswith("2m0") and ("muscat" in instrume or "ep" in instrume):
        return "muscat4"
    if tel.startswith("1m0"):
        return "sinistro"

    raise LcoError(
        "Could not infer destination instrument",
        detail=f"site={site}, tel={tel}, instrume={instrume}, filename={filename}",
    )


# IANA tz names for each LCO site, used to bucket frames into the same
# "observing night" a human would use (local evening through local morning).
_LCO_SITE_TZ = {
    "ogg": "Pacific/Honolulu",
    "coj": "Australia/Brisbane",
    "lsc": "America/Santiago",
    "cpt": "Africa/Johannesburg",
    "elp": "America/Chicago",
    "tfn": "Atlantic/Canary",
    "tlv": "Asia/Jerusalem",
}
_LCO_DATASET_MATCH_ARCSEC = 60.0


def _parse_lco_obs_dt(frame: dict) -> datetime.datetime | None:
    raw = (
        frame.get("DATE_OBS")
        or frame.get("observation_date")
        or frame.get("DAY_OBS")
        or ""
    )
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return datetime.datetime.fromisoformat(raw).replace(tzinfo=datetime.timezone.utc)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def _lco_observing_date(frame: dict) -> str:
    dt = _parse_lco_obs_dt(frame)
    if dt is None:
        day_obs = str(frame.get("DAY_OBS") or "").strip()
        if day_obs:
            return day_obs
        return ""
    site = str(frame.get("SITEID") or "").strip().lower()
    tz_name = _LCO_SITE_TZ.get(site, "UTC")
    local_dt = dt.astimezone(ZoneInfo(tz_name))
    # Observing nights run through local midnight, so local post-midnight frames
    # belong to the prior evening's dataset.
    if local_dt.hour < 12:
        local_dt = local_dt - datetime.timedelta(days=1)
    return local_dt.date().isoformat()


def _sexagesimal_to_deg(value: str, *, is_ra: bool) -> float | None:
    parts = value.split(":")
    if len(parts) != 3:
        return None
    sign = 1.0
    head = parts[0]
    if not is_ra and head.startswith("-"):
        sign = -1.0
    head = head.lstrip("+-")
    try:
        a = float(head)
        b = float(parts[1])
        c = float(parts[2])
    except ValueError:
        return None
    base = abs(a) + b / 60.0 + c / 3600.0
    if is_ra:
        return base * 15.0
    return sign * base


def _coord_to_deg(value, *, is_ra: bool) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    clean = _clean_ra(s) if is_ra else _clean_dec(s)
    if clean is None:
        return None
    return _sexagesimal_to_deg(clean, is_ra=is_ra)


def _frame_coords_deg(frame: dict) -> tuple[float | None, float | None]:
    ra = (
        frame.get("RA")
        or frame.get("ra")
        or frame.get("ra_x")
        or frame.get("target_ra")
    )
    dec = (
        frame.get("DEC")
        or frame.get("Dec")
        or frame.get("declination")
        or frame.get("dec_x")
        or frame.get("target_dec")
    )
    return _coord_to_deg(ra, is_ra=True), _coord_to_deg(dec, is_ra=False)


def _local_lco_datasets(inst: str, obsdate: str, site: str) -> list[dict]:
    db = _db_path()
    with get_conn(db) as conn:
        conn.create_aggregate("coord_repr", 2, CoordRepr)
        rows = conn.execute(
            """
            SELECT object, COUNT(*) AS nframes, coord_repr(ra, declination) AS coord
            FROM frames
            WHERE instrument = ?
              AND obsdate = ?
              AND filename LIKE ?
            GROUP BY object
            """,
            (inst, obsdate, f"{site}%"),
        ).fetchall()
    out = []
    for obj, nframes, packed in rows:
        ra_raw, dec_raw = _unpack_coord(packed)
        ra_deg = _coord_to_deg(ra_raw, is_ra=True)
        dec_deg = _coord_to_deg(dec_raw, is_ra=False)
        out.append(
            {
                "object": obj or "",
                "nframes": int(nframes or 0),
                "ra_deg": ra_deg,
                "dec_deg": dec_deg,
            }
        )
    return out


def _annotate_lco_archive_results(inst: str, results: list[dict]) -> tuple[list[dict], int]:
    if not results:
        return [], 0

    rows: list[dict] = [dict(r) for r in results]
    rows.sort(
        key=lambda r: (
            _parse_lco_obs_dt(r) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
            str(r.get("filename") or r.get("basename") or ""),
        )
    )

    filename_to_group: dict[str, str] = {}
    dataset_meta: dict[str, dict] = {}
    group_idx_by_key: dict[tuple[str, str, str], int] = {}

    for row in rows:
        observing_date = _lco_observing_date(row)
        identity = (
            observing_date,
            str(row.get("OBJECT") or ""),
            str(row.get("SITEID") or ""),
        )
        if identity not in group_idx_by_key:
            group_idx_by_key[identity] = len(group_idx_by_key) + 1
        group_id = f"{observing_date or 'unknown'}:{group_idx_by_key[identity]}"
        if group_id not in dataset_meta:
            inferred_inst = inst if inst in INSTRUMENTS else ""
            if not inferred_inst:
                try:
                    inferred_inst = infer_archive_instrument(row)
                except LcoError:
                    inferred_inst = ""
            dataset_meta[group_id] = {
                "dataset_id": group_id,
                "dataset_date": observing_date,
                "instrument": inferred_inst,
                "object": str(row.get("OBJECT") or ""),
                "site": str(row.get("SITEID") or ""),
                "telescope": str(row.get("TELID") or ""),
                "instrument_header": str(row.get("INSTRUME") or ""),
                "frame_count": 0,
                "existing_count": 0,
                "filenames": [],
                "archive_ra_deg": None,
                "archive_dec_deg": None,
            }
        meta = dataset_meta[group_id]
        meta["frame_count"] += 1
        fname = str(row.get("filename") or row.get("basename") or "")
        if fname:
            meta["filenames"].append(fname)
            filename_to_group[fname] = group_id
        if meta["archive_ra_deg"] is None or meta["archive_dec_deg"] is None:
            ra_deg, dec_deg = _frame_coords_deg(row)
            if ra_deg is not None and dec_deg is not None:
                meta["archive_ra_deg"] = ra_deg
                meta["archive_dec_deg"] = dec_deg

    local_cache: dict[tuple[str, str, str], list[dict]] = {}
    for meta in dataset_meta.values():
        inst_name = str(meta.get("instrument") or "")
        obsdate = (meta.get("dataset_date") or "").replace("-", "")[2:8]
        site = str(meta.get("site") or "").lower()
        if not inst_name or not obsdate or not site:
            continue
        key = (inst_name, obsdate, site)
        if key not in local_cache:
            local_cache[key] = _local_lco_datasets(inst_name, obsdate, site)

        archive_ra = meta.get("archive_ra_deg")
        archive_dec = meta.get("archive_dec_deg")
        if archive_ra is None or archive_dec is None:
            archive_name = _normalize_target_name(str(meta.get("object") or ""))
            if not archive_name:
                continue
            for cand in local_cache[key]:
                if _normalize_target_name(str(cand.get("object") or "")) == archive_name:
                    meta["existing_count"] = int(cand.get("nframes") or 0)
                    meta["matched_object"] = str(cand.get("object") or "")
                    break
            continue

        best_match = None
        best_sep = None
        for cand in local_cache[key]:
            ra2 = cand.get("ra_deg")
            dec2 = cand.get("dec_deg")
            if ra2 is None or dec2 is None:
                continue
            sep = _angular_sep_arcsec(archive_ra, archive_dec, ra2, dec2)
            if best_sep is None or sep < best_sep:
                best_sep = sep
                best_match = cand
        if best_match is not None and best_sep is not None and best_sep <= _LCO_DATASET_MATCH_ARCSEC:
            meta["existing_count"] = int(best_match.get("nframes") or 0)
            meta["matched_object"] = str(best_match.get("object") or "")
            meta["match_sep_arcsec"] = round(best_sep, 2)

    out: list[dict] = []
    for row in rows:
        fname = str(row.get("filename") or row.get("basename") or "")
        gid = filename_to_group.get(fname, "")
        meta = dataset_meta.get(gid, {})
        row["dataset_id"] = gid
        row["dataset_date"] = meta.get("dataset_date", "")
        row["archive_instrument"] = meta.get("instrument", "")
        row["dataset_exists"] = bool(meta.get("existing_count"))
        row["dataset_existing_count"] = int(meta.get("existing_count", 0))
        row["dataset_frame_count"] = int(meta.get("frame_count", 0))
        row["dataset_matched_object"] = meta.get("matched_object", "")
        row["dataset_match_sep_arcsec"] = meta.get("match_sep_arcsec")

        # Check if frame is saved locally
        inferred_inst = meta.get("instrument") or ""
        obsdate = (meta.get("dataset_date") or "").replace("-", "")[2:8]
        row["saved_locally"] = False
        if inferred_inst and obsdate and fname:
            try:
                dest = frame_dest(inferred_inst, obsdate, fname)
                if dest.exists() and dest.stat().st_size > 0:
                    row["saved_locally"] = True
            except Exception:
                logger.debug("failed local saved-frame check for %s/%s/%s", inferred_inst, obsdate, fname, exc_info=True)
        out.append(row)
    return out, len(dataset_meta)


def _safe_segment(value: str, kind: str) -> str:
    """Return *value* if it is a single safe path segment, else raise.

    Blocks the traversal vector where a crafted frame payload (filename,
    DATE_OBS-derived obsdate, ...) escapes the download root via ``/`` or ``..``.
    """
    v = (value or "").strip()
    if (
        not v
        or v in (".", "..")
        or "/" in v
        or "\\" in v
        or ".." in v
        or not _SAFE_SEGMENT_RE.match(v)
    ):
        raise LcoError(f"unsafe {kind}: {value!r}", status=400)
    return v


def download_root() -> Path | None:
    """Return the configured download root, or ``None`` if unset.

    Single source of truth for where archive frames land: ``MUSCAT_LCO_DIR``
    takes precedence, then ``MUSCAT_DATA_DIR``. Kept side-effect free (no raise)
    so callers that only want to *display* the location (config, UI hints) share
    the same resolution as the code that actually writes files.
    """
    lco_dir = os.environ.get("MUSCAT_LCO_DIR")
    if lco_dir:
        return Path(lco_dir)
    data_dir = os.environ.get("MUSCAT_DATA_DIR")
    if data_dir:
        return Path(data_dir).expanduser()
    return None


def download_instrument_dir(instrument: str) -> str:
    """Return the case-sensitive archive-download directory for an instrument."""
    key = (instrument or "").strip().lower()
    return _DOWNLOAD_INSTRUMENT_DIRS.get(key, instrument)


def frame_dest(instrument: str, obsdate: str, filename: str) -> Path:
    """Return the destination path for a downloaded frame."""
    root = download_root()
    if root is None:
        raise LcoError("MUSCAT_LCO_DIR or MUSCAT_DATA_DIR must be set", status=503)
    # Validate every segment so a crafted frame payload can't traverse out of the
    # download root (arbitrary file write via urlretrieve). Confirm the resolved
    # path stays under the root as a final backstop.
    instrument = _safe_segment(download_instrument_dir(instrument), "instrument")
    obsdate = _safe_segment(obsdate, "obsdate")
    filename = _safe_segment(filename, "filename")
    root = root.resolve()
    dest = (root / instrument / obsdate / filename).resolve()
    try:
        dest.relative_to(root)
    except ValueError as exc:
        raise LcoError(f"unsafe frame path: {filename!r}", status=400) from exc
    return dest


def frame_destination(frame: dict) -> tuple[str, str, Path]:
    """Return the inferred instrument, YYMMDD directory, and path for a frame."""
    filename = frame.get("filename") or frame.get("basename")
    if not filename:
        raise LcoError("Frame metadata has no filename")
    instrument = infer_archive_instrument(frame)
    date_obs = str(
        frame.get("DATE_OBS")
        or frame.get("observation_date")
        or frame.get("DAY_OBS")
        or ""
    ).split("T")[0].replace("-", "")
    if len(date_obs) < 6:
        raise LcoError("Could not determine obsdate")
    obsdate = date_obs[2:]
    return instrument, obsdate, frame_dest(instrument, obsdate, str(filename))


def _validate_download_url(url: str) -> str:
    """Allow only public HTTPS endpoints used by the LCO frame archive.

    Hostname allowlisting prevents arbitrary destinations, while resolving and
    checking every address prevents an allowed hostname from reaching a local or
    private service.  Redirects pass through the same function via
    :class:`_ValidatedArchiveRedirectHandler`.
    """
    parsed = urllib.parse.urlparse(url or "")
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise LcoError(
            "refusing to download from untrusted URL", status=400, detail=url
        ) from exc
    if (
        parsed.scheme != "https"
        or host not in _DOWNLOAD_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LcoError("refusing to download from untrusted URL", status=400, detail=url)

    try:
        addresses = {
            ipaddress.ip_address(sockaddr[0])
            for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                host, port or 443, type=socket.SOCK_STREAM
            )
        }
    except (OSError, ValueError) as exc:
        raise LcoError(
            "could not resolve archive download host", status=502, detail=host
        ) from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise LcoError(
            "refusing archive download host with non-public address",
            status=400,
            detail=host,
        )
    return url


class _ValidatedArchiveRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reapply the archive URL policy before urllib follows each redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_download_url(url: str, timeout: float):
    """Open an archive URL with redirect validation enabled for every hop."""
    _validate_download_url(url)
    opener = urllib.request.build_opener(_ValidatedArchiveRedirectHandler())
    return opener.open(url, timeout=timeout)


# Per-frame download timeout (seconds), applied to each socket read. A stalled
# archive/S3 connection must fail fast rather than block the request thread — and
# under `serve --reload`, the whole server — indefinitely. Overridable via env
# for slow links or unusually large frames.
_DOWNLOAD_TIMEOUT_S = float(os.environ.get("MUSCAT_LCO_DOWNLOAD_TIMEOUT_S", "120"))
_DOWNLOAD_CHUNK = 1 << 20  # 1 MiB
_FUNPACK_TIMEOUT_S = float(os.environ.get("MUSCAT_LCO_FUNPACK_TIMEOUT_S", "300"))


def _download_to_file(url: str, dest: Path, timeout: float = _DOWNLOAD_TIMEOUT_S) -> None:
    """Stream *url* to *dest* atomically, with a per-read socket timeout.

    Writes to a sibling ``.part`` file and atomically renames on success so an
    interrupted or stalled download never leaves a truncated ``.fits.fz`` in
    place. ``timeout`` applies to each socket read, so a hung connection raises
    ``TimeoutError`` instead of blocking forever (the bug that wedged the server
    when a bare ``urlretrieve`` stalled mid-dataset).
    """
    tmp = dest.with_name(dest.name + ".part")
    try:
        with _open_download_url(url, timeout=timeout) as response:
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(response, fh, _DOWNLOAD_CHUNK)
        tmp.replace(dest)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        # Drop the partial file so a retry starts clean; re-raise for the caller
        # to record as this frame's error without aborting the rest of the batch.
        tmp.unlink(missing_ok=True)
        raise
    finally:
        # Belt-and-suspenders: on success tmp was renamed away; on any exit path
        # ensure no stray .part lingers.
        tmp.unlink(missing_ok=True)


def _download_frame(frame: dict, overwrite: bool = False) -> dict:
    filename = frame.get("filename") or frame.get("basename")
    if not filename:
        return {"filename": "unknown", "status": "error", "error": "missing filename"}

    status = {"filename": filename, "status": "pending"}
    try:
        _instrument, _obsdate, dest = frame_destination(frame)
        status["dest"] = str(dest)

        if dest.exists() and not overwrite:
            status["status"] = "exists"
            return status

        dest.parent.mkdir(parents=True, exist_ok=True)

        url = frame.get("url")
        if not url:
            status["status"] = "error"
            status["error"] = "missing download url"
            return status

        _validate_download_url(url)
        _download_to_file(url, dest)
        status["status"] = "downloaded"

    except LcoError as e:
        status["status"] = "error"
        status["error"] = e.message
    except Exception as e:
        status["status"] = "error"
        status["error"] = str(e)
    return status


def download_frames(frames: list[dict], overwrite: bool = False) -> list[dict]:
    """Download frames from the LCO archive."""
    return [_download_frame(frame, overwrite=overwrite) for frame in frames]


def _funpack_dest(path: Path) -> Path | None:
    if path.name.endswith(".fits.fz"):
        return path.with_name(path.name[:-3])
    if path.name.endswith(".fz"):
        return path.with_name(path.name[:-3])
    return None


def _funpack_file(path: Path, timeout: float = _FUNPACK_TIMEOUT_S) -> dict:
    out = _funpack_dest(path)
    status = {
        "filename": path.name,
        "src": str(path),
        "dest": str(out) if out else "",
        "status": "pending",
    }
    if out is None:
        status["status"] = "skipped"
        status["error"] = "not an fpacked FITS filename"
        return status
    if out.exists():
        status["status"] = "exists"
        return status
    funpack = shutil.which("funpack")
    if not funpack:
        status["status"] = "error"
        status["error"] = "funpack is not installed"
        return status
    try:
        proc = subprocess.run(
            [funpack, "-O", str(out), str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except OSError as exc:
        status["status"] = "error"
        status["error"] = str(exc)
        return status
    except subprocess.TimeoutExpired:
        status["status"] = "error"
        status["error"] = f"funpack timed out after {timeout:g}s"
        return status
    if proc.returncode != 0:
        status["status"] = "error"
        status["error"] = (proc.stderr or proc.stdout or f"funpack exited {proc.returncode}").strip()
        return status
    status["status"] = "unpacked"
    return status


def _funpack_paths(results: list[dict]) -> list[Path]:
    paths = []
    seen: set[str] = set()
    for result in results:
        if result.get("status") not in {"downloaded", "exists"}:
            continue
        dest = result.get("dest")
        if not dest:
            continue
        path = Path(dest)
        if str(path) in seen:
            continue
        seen.add(str(path))
        if path.name.endswith(".fz"):
            paths.append(path)
    return paths


def _funpack_download_results(results: list[dict]) -> list[dict]:
    return [_funpack_file(path) for path in _funpack_paths(results)]


_ARCHIVE_DOWNLOAD_WORKERS = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_DOWNLOAD_WORKERS", "1")))
_ARCHIVE_DOWNLOAD_FRAME_WORKERS = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_DOWNLOAD_FRAME_WORKERS", "8")))
_ARCHIVE_FUNPACK_WORKERS = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_FUNPACK_WORKERS", "2")))
_ARCHIVE_DOWNLOAD_JOB_TTL_S = max(60, int(os.environ.get("MUSCAT_LCO_ARCHIVE_DOWNLOAD_JOB_TTL_S", "86400")))
_ARCHIVE_DOWNLOAD_MAX_JOBS = max(10, int(os.environ.get("MUSCAT_LCO_ARCHIVE_DOWNLOAD_MAX_JOBS", "200")))
_ARCHIVE_DOWNLOAD_MAX_FRAMES = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_MAX_FRAMES", "500")))
_ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES = max(1024, int(os.environ.get("MUSCAT_LCO_ARCHIVE_MAX_PAYLOAD_BYTES", "2097152")))
_ARCHIVE_DOWNLOAD_MAX_PER_USER = max(1, int(os.environ.get("MUSCAT_LCO_ARCHIVE_MAX_ACTIVE_PER_USER", "2")))
_ARCHIVE_DOWNLOAD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_ARCHIVE_DOWNLOAD_WORKERS,
    thread_name_prefix="lco-archive-download",
)
_ARCHIVE_DOWNLOAD_LOCK = threading.Lock()
_ARCHIVE_DOWNLOAD_JOBS: dict[str, dict] = {}


def _archive_download_snapshot(job: dict) -> dict:
    frames = list(job["frames"])
    results = [dict(r) for r in job["results"]]
    funpack_results = [dict(r) for r in job.get("funpack_results", [])]
    processing_results = [dict(r) for r in job.get("processing_results", [])]
    instruments: list[str] = []
    obsdates: list[str] = []
    objects: list[str] = []
    dest_dirs: list[str] = []

    def add_unique(values: list[str], value: str | None) -> None:
        if value and value not in values:
            values.append(value)

    for frame in frames:
        add_unique(
            objects,
            str(frame.get("OBJECT") or frame.get("object") or frame.get("target_name") or "").strip(),
        )
        try:
            inst, obsdate, dest = frame_destination(frame)
            add_unique(instruments, inst)
            add_unique(obsdates, obsdate)
            add_unique(dest_dirs, str(dest.parent))
        except Exception:
            pass

    for result in results:
        dest = result.get("dest")
        if dest:
            add_unique(dest_dirs, str(Path(dest).parent))

    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "frames_total": job["frames_total"],
        "frames_done": len(results),
        "results": results,
        "phase": job.get("phase", "pending"),
        "funpack_total": job.get("funpack_total", 0),
        "funpack_done": len(funpack_results),
        "funpack_results": funpack_results,
        "processing_results": processing_results,
        "photometry_url": job.get("photometry_url") or "",
        "instruments": instruments,
        "obsdates": obsdates,
        "objects": objects,
        "dest_dirs": dest_dirs,
        "started_at": job["started_at"],
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "user_name": job.get("user_name", ""),
    }


def _archive_dataset_pairs(frames: list[dict]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for frame in frames:
        inst, obsdate, _dest = frame_destination(frame)
        pair = (inst, obsdate)
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def _archive_photometry_url(
    datasets: list[tuple[str, str]], frames: list[dict]
) -> str:
    if len(datasets) != 1:
        return ""
    inst, obsdate = datasets[0]
    objects: list[str] = []
    for frame in frames:
        target = str(
            frame.get("OBJECT") or frame.get("object") or frame.get("target_name") or ""
        ).strip()
        if target and target not in objects:
            objects.append(target)
    params = {"inst": inst, "date": obsdate}
    if len(objects) == 1:
        params["target"] = objects[0]
    return "/photometry?" + urllib.parse.urlencode(params)


def _process_archive_datasets(job_id: str, frames: list[dict]) -> None:
    """Serially scan and ingest every dataset in an interactive download."""
    from muscat_db.database import ingest_date
    from muscat_db.scanner import scan_date

    root = download_root()
    if root is None:
        raise RuntimeError("MUSCAT_LCO_DIR or MUSCAT_DATA_DIR must be configured")
    datasets = _archive_dataset_pairs(frames)
    if not datasets:
        raise RuntimeError("Could not determine an instrument/date to ingest")

    for inst, obsdate in datasets:
        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is None:
                return
            current["phase"] = "scanning"
        scan_result = scan_date(inst, obsdate, max_workers=1, data_root=str(root))
        if not scan_result or not scan_result.get("total"):
            raise RuntimeError(f"scan found no reduced FITS files for {inst} {obsdate}")

        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is None:
                return
            current["phase"] = "ingesting"
        count = ingest_date(_db_path(), inst, obsdate)
        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is None:
                return
            current["processing_results"].append({
                "instrument": inst,
                "obsdate": obsdate,
                "scanned_count": int(scan_result.get("total") or 0),
                "ingested_count": int(count or 0),
            })

    with _ARCHIVE_DOWNLOAD_LOCK:
        current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
        if current is not None:
            current["photometry_url"] = _archive_photometry_url(datasets, frames)


def _prune_archive_download_jobs(now: float | None = None, reserve_slots: int = 0) -> None:
    now = now if now is not None else time.time()
    finished = [
        (jid, job.get("finished_at") or 0)
        for jid, job in _ARCHIVE_DOWNLOAD_JOBS.items()
        if job["state"] in {"done", "error"}
    ]
    for jid, finished_at in finished:
        if finished_at and now - finished_at > _ARCHIVE_DOWNLOAD_JOB_TTL_S:
            _ARCHIVE_DOWNLOAD_JOBS.pop(jid, None)

    target_size = max(0, _ARCHIVE_DOWNLOAD_MAX_JOBS - reserve_slots)
    overflow = len(_ARCHIVE_DOWNLOAD_JOBS) - target_size
    if overflow > 0:
        finished = [
            (jid, job.get("finished_at") or 0)
            for jid, job in _ARCHIVE_DOWNLOAD_JOBS.items()
            if job["state"] in {"done", "error"}
        ]
        for jid, _finished_at in sorted(finished, key=lambda item: item[1])[:overflow]:
            _ARCHIVE_DOWNLOAD_JOBS.pop(jid, None)


def _run_archive_download_job(job_id: str) -> None:
    with _ARCHIVE_DOWNLOAD_LOCK:
        job = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
        if job is None:
            return
        job["state"] = "running"
        job["phase"] = "downloading"
        frames = list(job["frames"])
        overwrite = bool(job["overwrite"])

    try:
        max_workers = min(_ARCHIVE_DOWNLOAD_FRAME_WORKERS, len(frames))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"lco-archive-frame-{job_id}",
        ) as pool:
            futures = [pool.submit(_download_frame, frame, overwrite=overwrite) for frame in frames]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                with _ARCHIVE_DOWNLOAD_LOCK:
                    current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
                    if current is None:
                        return
                    current["results"].append(result)
        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is None:
                return
            current["phase"] = "funpacking"
            results = [dict(r) for r in current["results"]]
            funpack_paths = _funpack_paths(results)
            current["funpack_total"] = len(funpack_paths)
            auto_ingest = bool(current.get("auto_ingest"))
        download_failed = any(result.get("status") == "error" for result in results)
        funpack_failed = False
        if funpack_paths:
            max_workers = min(_ARCHIVE_FUNPACK_WORKERS, len(funpack_paths))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"lco-archive-funpack-{job_id}",
            ) as pool:
                futures = {pool.submit(_funpack_file, path): path for path in funpack_paths}
                for future in concurrent.futures.as_completed(futures):
                    path = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "filename": path.name,
                            "src": str(path),
                            "dest": str(_funpack_dest(path) or ""),
                            "status": "error",
                            "error": str(exc),
                        }
                    if result.get("status") == "error":
                        funpack_failed = True
                    with _ARCHIVE_DOWNLOAD_LOCK:
                        current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
                        if current is None:
                            return
                        current["funpack_results"].append(result)
        processing_failed = download_failed or funpack_failed
        if not processing_failed and auto_ingest:
            _process_archive_datasets(job_id, frames)
        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is not None:
                current["phase"] = "done"
                current["state"] = "error" if processing_failed else "done"
                if download_failed:
                    current["error"] = "One or more FITS downloads failed"
                elif funpack_failed:
                    current["error"] = "One or more funpack commands failed"
                current["finished_at"] = time.time()
                _prune_archive_download_jobs(current["finished_at"])
    except Exception as exc:
        with _ARCHIVE_DOWNLOAD_LOCK:
            current = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
            if current is not None:
                current["state"] = "error"
                current["error"] = str(exc)
                current["finished_at"] = time.time()
                _prune_archive_download_jobs(current["finished_at"])


def _compact_archive_frames(frames: list[dict]) -> list[dict]:
    """Retain only metadata consumed by download, ingest, and status paths."""
    keys = {
        "filename", "basename", "SITEID", "site_id", "TELID", "telescope_id",
        "INSTRUME", "instrument_id", "DATE_OBS", "observation_date", "DAY_OBS",
        "OBJECT", "object", "target_name", "url",
    }
    return [{key: frame[key] for key in keys if key in frame} for frame in frames]


def start_archive_download(
    frames: list[dict], overwrite: bool = False, auto_ingest: bool = False,
    user_name: str | None = None,
) -> dict:
    """Queue an LCO archive download in a dedicated worker and return its state."""
    if not isinstance(frames, list) or not frames:
        raise LcoError("no frames selected", status=400)
    if len(frames) > _ARCHIVE_DOWNLOAD_MAX_FRAMES:
        raise LcoError(
            f"At most {_ARCHIVE_DOWNLOAD_MAX_FRAMES} frames are allowed per download",
            status=413,
        )
    if any(not isinstance(frame, dict) for frame in frames):
        raise LcoError("each frame must be an object", status=400)
    compact_frames = _compact_archive_frames(frames)
    payload_bytes = len(json.dumps(compact_frames, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    if payload_bytes > _ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES:
        raise LcoError(
            f"Frame payload exceeds {_ARCHIVE_DOWNLOAD_MAX_PAYLOAD_BYTES} bytes",
            status=413,
        )
    user_key = (user_name or "anonymous").strip() or "anonymous"
    job_id = uuid.uuid4().hex[:16]
    now = time.time()
    job = {
        "job_id": job_id,
        "state": "pending",
        "frames": compact_frames,
        "frames_total": len(compact_frames),
        "user_name": user_key,
        "overwrite": overwrite,
        "results": [],
        "funpack_results": [],
        "funpack_total": 0,
        "processing_results": [],
        "photometry_url": "",
        "auto_ingest": bool(auto_ingest),
        "phase": "pending",
        "started_at": now,
        "finished_at": None,
        "error": None,
    }
    with _ARCHIVE_DOWNLOAD_LOCK:
        _prune_archive_download_jobs(now, reserve_slots=1)
        active_for_user = sum(
            1 for existing in _ARCHIVE_DOWNLOAD_JOBS.values()
            if existing.get("state") in {"pending", "running"}
            and existing.get("user_name", "anonymous") == user_key
        )
        if active_for_user >= _ARCHIVE_DOWNLOAD_MAX_PER_USER:
            raise LcoError(
                "Too many active LCO archive downloads for this user",
                status=429,
            )
        if len(_ARCHIVE_DOWNLOAD_JOBS) >= _ARCHIVE_DOWNLOAD_MAX_JOBS:
            raise LcoError(
                "Too many LCO archive download jobs are queued",
                status=429,
                detail=(
                    f"At most {_ARCHIVE_DOWNLOAD_MAX_JOBS} archive download jobs are tracked "
                    "in this server process. Wait for queued jobs to finish before submitting more."
                ),
            )
        _ARCHIVE_DOWNLOAD_JOBS[job_id] = job
        snapshot = _archive_download_snapshot(job)
    _ARCHIVE_DOWNLOAD_EXECUTOR.submit(_run_archive_download_job, job_id)
    return snapshot


def archive_download_status(job_id: str) -> dict:
    """Return the current state for a queued archive-download job."""
    with _ARCHIVE_DOWNLOAD_LOCK:
        _prune_archive_download_jobs()
        job = _ARCHIVE_DOWNLOAD_JOBS.get(job_id)
        if job is None:
            raise LcoError("LCO archive download job not found", status=404)
        return _archive_download_snapshot(job)


def archive_download_jobs() -> list[dict]:
    """Return LCO archive-download jobs known to this server process."""
    with _ARCHIVE_DOWNLOAD_LOCK:
        _prune_archive_download_jobs()
        jobs = [_archive_download_snapshot(job) for job in _ARCHIVE_DOWNLOAD_JOBS.values()]
    jobs.sort(key=lambda job: job.get("started_at") or 0, reverse=True)
    return jobs


def generate_windows(t0: float, period: float, duration_h: float, start_dt: str, end_dt: str, pad_before_min: float, pad_after_min: float) -> list[dict]:
    """Generate transit windows within a date range.

    Epochs are normalized to the first transit within the date range for clarity
    (epoch 0 = first transit in the range, not absolute count from t0).

    Window boundaries retain the precise calculated transit times. LCO checks
    visibility against the actual astronomical window, so rounding boundaries
    can make a request claim slightly more observable time than exists.
    """
    if not all([start_dt, end_dt]):
        raise LcoError("Date range is required", status=400)

    start = datetime.datetime.fromisoformat(start_dt + "T00:00:00").replace(tzinfo=datetime.timezone.utc)
    end = datetime.datetime.fromisoformat(end_dt + "T23:59:59").replace(tzinfo=datetime.timezone.utc)

    # JD for Unix epoch is 2440587.5. BJD is close enough for this purpose.
    t0_dt = datetime.datetime.fromtimestamp((t0 - 2440587.5) * 86400, tz=datetime.timezone.utc)

    epoch_at_start = math.floor((start - t0_dt).total_seconds() / (period * 86400.0))

    windows = []
    current_epoch = epoch_at_start
    relative_epoch = 0  # Reset to 0 for the first window in range
    first_in_range = True

    while True:
        mid_bjd = t0 + current_epoch * period
        # Recalculate mid_dt from BJD each time to avoid float drift
        mid_dt = datetime.datetime.fromtimestamp((mid_bjd - 2440587.5) * 86400, tz=datetime.timezone.utc)

        if mid_dt > end:
            break

        if mid_dt >= start:
            if first_in_range:
                relative_epoch = current_epoch  # Store absolute epoch for first transit
                first_in_range = False

            start_obs = mid_dt - datetime.timedelta(hours=duration_h / 2.0, minutes=pad_before_min)
            end_obs = mid_dt + datetime.timedelta(hours=duration_h / 2.0, minutes=pad_after_min)

            windows.append({
                "epoch": int(current_epoch - relative_epoch),  # Display relative epoch (0-indexed)
                "epoch_abs": int(current_epoch),  # Store absolute epoch for reference
                "mid_bjd": mid_bjd,
                "mid": mid_dt.isoformat().replace("+00:00", "Z"),
                "start": start_obs.isoformat().replace("+00:00", "Z"),
                "end": end_obs.isoformat().replace("+00:00", "Z"),
            })

        current_epoch += 1
        if len(windows) > 1000: # safety break
             break

    return windows

def payload_hash(payload: dict) -> str:
    """Create a stable hash of the requestgroup payload."""
    # Serialize with sorted keys to ensure a consistent hash
    s = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# Front-padding (target setup / acquisition) the LCO scheduler reserves per
# configuration before science exposures begin. A REPEAT_EXPOSE config must fit
# within its window *including* this overhead, so we subtract it when deriving
# repeat_duration from the window span. 180 s matches accepted 2m0 MUSCAT
# requests; the dry-run (max_allowable_ipp) validates the final fit.
_REPEAT_EXPOSE_SETUP_OVERHEAD_S = 180

# LCO's scheduler computes visibility slightly more strictly than our astropy
# model (acquisition/slew/readout settling plus a marginally higher effective
# altitude limit). Empirically, for HIP67522 at LSC with max_airmass=2 our model
# gives ~3.47 h visible vs. LCO's ~3.07 h. So when a window edge is bounded by
# the target's own rise/set (not by the scheduling boundary), the repeat block is
# held back by this margin from that edge to keep the dry-run passing. The exact
# value varies per site/target; 900 s per bounded edge is a safe default.
_LCO_VISIBILITY_EDGE_MARGIN_S = 900


def _repeat_duration(params: dict) -> int | None:
    """Seconds a REPEAT_EXPOSE config should repeat within its observing window.

    An explicit ``repeat_duration`` wins. Otherwise derive it from the shortest
    selected window (so one value fits every window), less the setup overhead.
    When ``_clip_windows_to_observability`` capped a window's usable span (its
    LCO-visible span, already shy of the observability edge margin), that cap is
    applied before the setup overhead is deducted.
    Returns ``None`` when it cannot be determined (no windows / bad timestamps).
    """
    explicit = params.get("repeat_duration")
    if explicit:
        try:
            return int(float(explicit))
        except (TypeError, ValueError):
            pass

    caps = params.get("_observable_repeat_duration") or {}
    durations = []
    for i, w in enumerate(params.get("windows") or []):
        start, end = w.get("start"), w.get("end")
        if not start or not end:
            continue
        try:
            t0 = datetime.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            t1 = datetime.datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        except ValueError:
            continue
        usable_s = (t1 - t0).total_seconds()
        cap = caps.get(i)
        if cap is not None:
            usable_s = min(usable_s, float(cap))
        durations.append(int(usable_s) - _REPEAT_EXPOSE_SETUP_OVERHEAD_S)
    if not durations:
        return None
    return max(min(durations), 60)


def _config_type_block(params: dict, default_type: str) -> dict:
    """``{"type": ...}`` plus ``repeat_duration`` only when REPEAT_EXPOSE.

    Emitting ``repeat_duration`` on a non-REPEAT_EXPOSE config is invalid, so it
    is included exclusively for REPEAT_EXPOSE (and only when derivable).
    """
    config_type = params.get("type") or default_type
    block: dict = {"type": config_type}
    if config_type == "REPEAT_EXPOSE":
        duration = _repeat_duration(params)
        if duration is not None:
            block["repeat_duration"] = duration
    return block


def _value_or_default(value, default):
    return default if value in (None, "") else value


def _clip_windows_to_observability(kind: str, params: dict, max_airmass, min_lunar_distance, max_lunar_phase=1.0) -> None:
    """Clip each REPEAT_EXPOSE window to the portion observable at the pinned site.

    ``generate_windows`` pads windows around mid-transit with no site awareness,
    so a window can run past the target's rise/set at the site (e.g. post-egress
    padding below the airmass limit). When a specific ``site`` is pinned, each
    window's ``start``/``end`` is clipped to the longest contiguous observable run
    at that site under the request's own constraints (the airmass/moon/twilight
    actually submitted); unclipped edges keep their exact timestamps.
    ``repeat_duration`` (via :func:`_repeat_duration`) is then derived from the
    clipped span, held back by ``_LCO_VISIBILITY_EDGE_MARGIN_S`` for each edge
    bounded by the target's own rise/set.

    No pinned site leaves the windows untouched — the scheduler may pick any site
    on the telescope class, so one site's interval would not apply. A window with
    no observable time at the site raises.
    """
    if (params.get("type") or "REPEAT_EXPOSE") != "REPEAT_EXPOSE":
        return
    site = (params.get("site") or "").strip().lower()
    windows = params.get("windows") or []
    if not site or not windows or params.get("ra") in (None, "") or params.get("dec") in (None, ""):
        return

    try:
        from muscat_db import transit_obs

        ra = float(params["ra"])
        dec = float(params["dec"])
        twilight = params.get("twilight") or transit_obs.DEFAULT_TWILIGHT
        moon_sep = float(min_lunar_distance)
        phase = float(max_lunar_phase)
        airmass = float(max_airmass)

        clipped = []
        caps: dict[int, int] = {}
        for idx, window in enumerate(windows):
            interval = transit_obs.observable_interval(
                ra, dec, window, site,
                max_airmass=airmass, twilight=twilight,
                moon_sep_min=moon_sep, max_lunar_phase=phase,
            )
            if interval is None:
                raise LcoError(
                    f"Window {idx + 1} is not observable at {site.upper()} — "
                    "choose a different window, site, or loosen airmass/moon constraints",
                    status=400,
                )
            clipped.append({**window, "start": interval["start"], "end": interval["end"]})
            # One edge margin per edge the target's own rise/set bounds (not the
            # scheduling boundary): the LCO-visible span is that much shorter.
            edges = int(interval["hit_start_limit"]) + int(interval["hit_end_limit"])
            if edges:
                try:
                    start = datetime.datetime.fromisoformat(str(interval["start"]).replace("Z", "+00:00"))
                    end = datetime.datetime.fromisoformat(str(interval["end"]).replace("Z", "+00:00"))
                    span_s = (end - start).total_seconds()
                    caps[idx] = max(int(span_s) - edges * _LCO_VISIBILITY_EDGE_MARGIN_S, 60)
                except ValueError:
                    pass
        params["windows"] = clipped
        if caps:
            params["_observable_repeat_duration"] = caps
    except LcoError:
        raise
    except Exception:
        return


_INSTRUMENT_TYPE_TO_KIND = {
    "2M0-SCICAM-MUSCAT": "muscat",
    "1M0-SCICAM-SINISTRO": "sinistro",
}


def requestgroup_to_params(rg: dict) -> dict:
    """Reverse :func:`build_requestgroup`: an LCO requestgroup -> form params.

    Used to clone an existing observation into the schedule form. Windows are
    intentionally omitted: they are date-specific and a clone regenerates them
    for a new epoch. Unknown/missing fields are simply left out so the frontend
    falls back to its own defaults.
    """
    if not isinstance(rg, dict):
        raise LcoError("Requestgroup payload must be an object", status=400)
    requests = rg.get("requests") or []
    if not requests or not isinstance(requests[0], dict):
        raise LcoError("Requestgroup has no requests to clone", status=400)
    request = requests[0]
    configs = request.get("configurations") or []
    config = configs[0] if configs and isinstance(configs[0], dict) else {}
    inst_configs = config.get("instrument_configs") or []
    ic = inst_configs[0] if inst_configs and isinstance(inst_configs[0], dict) else {}
    extra = ic.get("extra_params") or {}
    optical = ic.get("optical_elements") or {}

    instrument_type = request.get("instrument_type") or config.get("instrument_type") or ""
    kind = _INSTRUMENT_TYPE_TO_KIND.get(instrument_type)
    if kind is None:
        raise LcoError(f"Unsupported instrument type for cloning: {instrument_type}", status=400)

    target = request.get("target") or {}
    # Config-level constraints are the richest; fall back to request-level.
    constraints = config.get("constraints") or request.get("constraints") or {}
    guiding = config.get("guiding_config") or {}

    params: dict = {
        "kind": kind,
        "name": rg.get("name"),
        "proposal": rg.get("proposal"),
        "ipp_value": rg.get("ipp_value"),
        "observation_type": rg.get("observation_type"),
        "target_name": target.get("name"),
        "ra": target.get("ra"),
        "dec": target.get("dec"),
        "type": config.get("type"),
        "guiding_config": guiding.get("mode"),
        "max_airmass": constraints.get("max_airmass"),
        "min_lunar_distance": constraints.get("min_lunar_distance"),
        "max_lunar_phase": constraints.get("max_lunar_phase", 1.0),
        "readout_mode": ic.get("mode"),
        "exposure_count": ic.get("exposure_count"),
        "defocus": extra.get("defocus", 0),
    }
    location = request.get("location") or {}
    if location.get("site"):
        params["site"] = location["site"]

    if kind == "muscat":
        params["exposure_times"] = {
            b: extra.get(f"exposure_time_{b}") for b in ("g", "r", "i", "z")
            if extra.get(f"exposure_time_{b}") is not None
        }
        if extra.get("exposure_mode") is not None:
            params["exposure_mode"] = extra["exposure_mode"]
        params["narrowband"] = {
            b: optical.get(f"narrowband_{b}_position", "out") for b in ("g", "r", "i", "z")
        }
    else:  # sinistro
        if optical.get("filter") is not None:
            params["filter"] = optical["filter"]
        if ic.get("exposure_time") is not None:
            params["exposure_time"] = ic["exposure_time"]

    # Drop keys that came back None so the frontend uses its own defaults.
    return {k: v for k, v in params.items() if v is not None}


def build_requestgroup(kind: str, params: dict, configurations: list[dict] | None = None) -> dict:
    """Construct the requestgroup payload for an observation.

    ``configurations`` is an ordered list of parameter overrides used by short
    test observations.  Each item is passed through the same instrument-specific
    builder and validation as a normal request.  Omitting it preserves the
    historical single-configuration payload exactly.
    """
    # Name the specific empty field(s) so the UI can point the user at what to
    # fill (a generic "missing parameters" error hides, e.g., an unset proposal).
    _REQUIRED_LABELS = {
        "name": "request name",
        "proposal": "proposal",
        "target_name": "target",
        "ra": "RA",
        "dec": "Dec",
    }
    missing = [label for key, label in _REQUIRED_LABELS.items() if not params.get(key)]
    if missing:
        raise LcoError(
            "Missing required scheduling parameters: " + ", ".join(missing),
            status=400,
        )

    supplied_configurations = configurations
    target = {
        "name": params["target_name"],
        "type": "ICRS",
        "ra": params["ra"],
        "dec": params["dec"],
    }

    # These constraints are defined at the request level, but get copied into
    # the configuration level by this function, as per the LCO examples.
    
    # Set default airmass and lunar distance based on instrument kind
    if kind in ("muscat", "muscat3", "muscat4"):
        default_max_airmass = 2.5
        default_min_lunar_distance = 18
    else:
        default_max_airmass = 1.6
        default_min_lunar_distance = 30

    max_airmass = _value_or_default(params.get("max_airmass"), default_max_airmass)
    min_lunar_distance = _value_or_default(params.get("min_lunar_distance"), default_min_lunar_distance)
    # LCO max_lunar_phase: max Moon illuminated fraction (0=new .. 1=full) to
    # schedule under. 1.0 is LCO's default (no phase restriction).
    max_lunar_phase = _value_or_default(params.get("max_lunar_phase"), 1.0)
    _clip_windows_to_observability(kind, params, max_airmass, min_lunar_distance, max_lunar_phase)

    constraints = {
        "max_airmass": max_airmass,
        "min_lunar_distance": min_lunar_distance,
        "max_lunar_phase": max_lunar_phase,
    }

    configurations = []
    instrument_type = ""
    if kind in ("muscat", "muscat3", "muscat4"):
        if not params.get("exposure_times"):
            raise LcoError("Exposure times are required for MuSCAT instruments", status=400)

        et = params["exposure_times"]
        band_times = {b: et.get(b, 0) for b in ("g", "r", "i", "z")}
        if not any(v > 0 for v in band_times.values()):
            raise LcoError("At least one MuSCAT band needs a positive exposure time", status=400)

        # MuSCAT is a simultaneous 4-band imager: LCO expects a SINGLE
        # instrument_config whose per-band exposures live in extra_params
        # (exposure_time_g/r/i/z). There is no per-band `filter` optical
        # element, and the top-level exposure_time is the longest (driving)
        # band. This mirrors LCO's accepted 2M0-SCICAM-MUSCAT request shape.
        nb = params.get("narrowband", {})
        config_type = params.get("type") or "REPEAT_EXPOSE"
        exposure_count = 1 if config_type == "REPEAT_EXPOSE" else params.get("exposure_count", 1)
        defocus = _validated_defocus(params, _DEFOCUS_LIMIT_MM[kind])
        instrument_configs = [{
            "exposure_time": max(band_times.values()),
            "exposure_count": exposure_count,
            "mode": params.get("readout_mode", "MUSCAT_FAST"),
            "optical_elements": {
                "narrowband_g_position": nb.get("g", "out"),
                "narrowband_i_position": nb.get("i", "out"),
                "narrowband_r_position": nb.get("r", "out"),
                "narrowband_z_position": nb.get("z", "out"),
            },
            "extra_params": {
                "bin_x": 1,
                "bin_y": 1,
                "offset_ra": 0,
                "offset_dec": 0,
                "defocus": defocus,
                "exposure_mode": params.get("exposure_mode", "ASYNCHRONOUS"),
                "exposure_time_g": band_times["g"],
                "exposure_time_i": band_times["i"],
                "exposure_time_r": band_times["r"],
                "exposure_time_z": band_times["z"],
            },
        }]
        instrument_type = "2M0-SCICAM-MUSCAT"
        configurations.append({
            **_config_type_block(params, "REPEAT_EXPOSE"),
            "instrument_type": instrument_type,
            "instrument_configs": instrument_configs,
            # Per the LCO instruments API, 2M0-SCICAM-MUSCAT only offers the
            # "OFF" acquisition mode; "WCS" is rejected at validation.
            "acquisition_config": {"mode": "OFF"},
            "guiding_config": {"mode": params.get("guiding_config", "ON"), "optional": True},
            "constraints": {
                "max_airmass": max_airmass,
                "min_lunar_distance": min_lunar_distance,
                "max_lunar_phase": max_lunar_phase,
                "max_seeing": params.get("max_seeing"),
                "min_transparency": params.get("min_transparency"),
                "extra_params": {}
            },
            "target": target
        })
    elif kind == "sinistro":
        mode = params.get("readout_mode", "central_2k_2x2")
        binning = 2 if "2x2" in mode else 1
        # Same REPEAT_EXPOSE constraint as MuSCAT above (line ~940): repeat_duration
        # is derived from the window span, so packing exposure_count to fill the
        # window leaves no room to repeat that block even once, and LCO rejects
        # it ("repeat_duration ... is less than the minimum required to repeat
        # at least once"). Force to 1 server-side too, not just in the frontend's
        # exposure-count field, so any caller is protected.
        sin_config_type = params.get("type") or "EXPOSE"
        sin_exposure_count = 1 if sin_config_type == "REPEAT_EXPOSE" else params.get("exposure_count", 1)
        defocus = _validated_defocus(params, _DEFOCUS_LIMIT_MM["sinistro"])
        instrument_configs = [{
            "exposure_count": sin_exposure_count,
            "exposure_time": params.get("exposure_time", 60),
            "mode": mode,
            "optical_elements": {"filter": params.get("filter", "rp")},
            "extra_params": {
                "bin_x": binning,
                "bin_y": binning,
                "offset_ra": 0,
                "offset_dec": 0,
                "defocus": defocus
            }
        }]
        instrument_type = "1M0-SCICAM-SINISTRO"
        configurations.append({
            **_config_type_block(params, "EXPOSE"),
            "instrument_type": instrument_type,
            "instrument_configs": instrument_configs,
            "acquisition_config": {"mode": "OFF"},
            "guiding_config": {"mode": params.get("guiding_config", "ON"), "optional": True},
            "constraints": constraints,
            "target": target
        })
    else:
        raise LcoError(f"Unsupported instrument kind for scheduling: {kind}", status=400)

    # telescope_class is always required; site is an optional narrowing. Gating
    # telescope_class behind site produced an invalid empty location and made
    # "any site on this class" impossible to express (LCO's accepted requests
    # carry telescope_class with no site when the network picks the site).
    location = {"telescope_class": "1m0" if kind == "sinistro" else "2m0"}
    if params.get("site"):
        location["site"] = params["site"]
    
    obs_type = "NORMAL"

    if supplied_configurations is not None:
        if not supplied_configurations:
            raise LcoError("Test observations require at least one configuration", status=400)
        ordered = []
        for index, overrides in enumerate(supplied_configurations):
            if not isinstance(overrides, dict):
                raise LcoError(f"Configuration {index + 1} must be an object", status=400)
            child_params = {**params, **overrides}
            child_params.pop("configurations", None)
            child = build_requestgroup(kind, child_params)
            child_configs = child["requests"][0]["configurations"]
            if len(child_configs) != 1:
                raise LcoError(f"Configuration {index + 1} did not produce one LCO configuration", status=400)
            ordered.append(child_configs[0])
        configurations = ordered

    return {
        "name": params["name"],
        "proposal": params["proposal"],
        "ipp_value": params.get("ipp_value", 1.0),
        "operator": "SINGLE",
        "observation_type": params.get("observation_type", obs_type),
        "requests": [{
            "target": target,
            "constraints": constraints,
            "location": location,
            "windows": params.get("windows", []),
            "instrument_type": instrument_type,
            "configurations": configurations,
        }]
    }

def max_allowable_ipp(
    request_group: dict,
    user_name: str | None = None,
    token: str | None = None,
    *,
    require_own_token: bool = True,
) -> dict:
    """Run the max-allowable-IPP dry-run."""
    url = "https://observe.lco.global/api/requestgroups/max_allowable_ipp/"
    return _lco_api_request(
        url, method="POST", data=request_group,
        user_name=user_name, token=token, require_own_token=require_own_token,
    )

def submit_requestgroup(
    request_group: dict,
    user_name: str | None = None,
    token: str | None = None,
    *,
    require_own_token: bool = True,
) -> dict:
    """Submit a live observation request."""
    if os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") != "1":
        raise LcoError(
            "Live submission is disabled on the server",
            status=403,
            detail="To enable, set MUSCAT_LCO_ALLOW_SUBMIT=1 in the server environment.",
        )
    url = "https://observe.lco.global/api/requestgroups/"
    return _lco_api_request(
        url, method="POST", data=request_group,
        user_name=user_name, token=token, require_own_token=require_own_token,
    )
