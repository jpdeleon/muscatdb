"""Las Cumbres Observatory (LCO) integration helpers.

Isolated module for the ``/lco`` page so the LCO-specific logic (Observation
Portal calls, archive search/download, transit-window generation, and
requestgroup payload construction) stays decoupled from the web layer and can be
split into its own service later. The web layer (``web.py``) only validates the
HTTP boundary and delegates here.

Design notes:
* HTTP uses stdlib ``urllib`` with a timeout + exponential backoff, matching the
  existing outbound-call style in ``web.py`` / ``exposure.py`` (no new runtime dep).
* The API token is read from ``LCO_API_TOKEN`` and never returned to the browser.
* Observation *submission* is built but guarded: ``submit_requestgroup`` performs
  the POST, but callers must enforce the dry-run + confirm gate, and the
  ``MUSCAT_LCO_ALLOW_SUBMIT`` env flag must be enabled. This keeps real, billed
  telescope requests from firing accidentally during development.
"""

from __future__ import annotations

import datetime
import copy
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from muscat_db import photometry as phot
from muscat_db.instruments import INSTRUMENTS

# --------------------------------------------------------------------------- #
# Endpoints & constants
# --------------------------------------------------------------------------- #

OBS_PORTAL_BASE = "https://observe.lco.global/api"
ARCHIVE_BASE = "https://archive-api.lco.global"

_USER_AGENT = "muscat-db-lco/1.0"
_HTTP_TIMEOUT_S = 20.0
_DOWNLOAD_TIMEOUT_S = 180.0
_RETRIES = 3
_BACKOFF_S = 1.0
_MAX_WINDOWS = 1000  # guard against absurd date ranges
_GET_CACHE_TTL_S = 60.0
_GET_CACHE_MAX = 128

# Instrument-type strings on the LCO network.
_MUSCAT_INSTRUMENT_TYPE = "2M0-SCICAM-MUSCAT"
_SINISTRO_INSTRUMENT_TYPE = "1M0-SCICAM-SINISTRO"
_MUSCAT_BANDS = ("g", "r", "i", "z")

_PROPOSAL_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,200}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

_GET_CACHE_LOCK = threading.Lock()
_GET_CACHE: dict[tuple, tuple[float, dict]] = {}


class LcoError(RuntimeError):
    """Boundary error carrying an HTTP-ish status and optional upstream detail."""

    def __init__(self, message: str, status: int = 400, detail: str | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"ok": False, "error": str(self)}
        if self.detail:
            out["detail"] = self.detail
        return out


def infer_archive_instrument(frame: dict) -> str:
    """Infer the muscat-db instrument for an LCO archive frame."""
    filename = str(frame.get("filename") or frame.get("basename") or "").strip().lower()
    site = str(frame.get("SITEID") or "").strip().lower()
    tel = str(frame.get("TELID") or "").strip().lower()

    if filename.startswith("ogg2m001-"):
        return "muscat3"
    if filename.startswith("coj2m002-"):
        return "muscat4"
    if tel.startswith("1m0") or (site and "1m0" in filename):
        return "sinistro"
    if tel.startswith("2m0") and site == "ogg":
        return "muscat3"
    if tel.startswith("2m0") and site == "coj":
        return "muscat4"
    raise LcoError("could not infer destination instrument from archive metadata", 400)


# --------------------------------------------------------------------------- #
# Token / config
# --------------------------------------------------------------------------- #


def has_token() -> bool:
    return bool(os.environ.get("LCO_API_TOKEN"))


def load_token() -> str:
    token = os.environ.get("LCO_API_TOKEN")
    if not token:
        raise LcoError(
            "LCO_API_TOKEN is not configured on the server; LCO portal features "
            "are unavailable.",
            status=503,
        )
    return token


def submit_allowed() -> bool:
    """Server-side master switch for live submission (default off)."""
    return os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT", "0").strip().lower() in ("1", "true", "yes")


def config_state() -> dict:
    """Non-secret status for the page (booleans only — never the token value)."""
    return {
        "token_configured": has_token(),
        "download_root_configured": bool(os.environ.get("MUSCAT_LCO_DIR")),
        "submit_allowed": submit_allowed(),
    }


# --------------------------------------------------------------------------- #
# Small validation helpers
# --------------------------------------------------------------------------- #


def _safe_float(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pos_float(value, field: str) -> float:
    v = _safe_float(value)
    if v is None or v <= 0 or not math.isfinite(v):
        raise LcoError(f"{field} must be a positive number", 400)
    return v


def _pos_int(value, field: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise LcoError(f"{field} must be a positive integer", 400)
    if v <= 0:
        raise LcoError(f"{field} must be a positive integer", 400)
    return v


def _req_str(value, field: str, lo: int, hi: int) -> str:
    s = ("" if value is None else str(value)).strip()
    if not (lo <= len(s) <= hi):
        raise LcoError(f"{field} must be {lo}-{hi} characters", 400)
    if any(ord(c) < 32 for c in s):
        raise LcoError(f"{field} contains control characters", 400)
    return s


def _req_token(value, field: str) -> str:
    s = ("" if value is None else str(value)).strip()
    if not s:
        raise LcoError(f"{field} is required", 400)
    if not _TOKEN_RE.match(s):
        raise LcoError(f"{field} has an invalid value", 400)
    return s


def validate_proposal(value) -> str:
    s = ("" if value is None else str(value)).strip()
    if not _PROPOSAL_RE.match(s):
        raise LcoError("proposal id is missing or malformed", 400)
    return s


def validate_radec(ra, dec) -> tuple[float, float]:
    r = _safe_float(ra)
    d = _safe_float(dec)
    if r is None or not (0.0 <= r < 360.0):
        raise LcoError("ra must be in [0, 360) degrees", 400)
    if d is None or not (-90.0 <= d <= 90.0):
        raise LcoError("dec must be in [-90, 90] degrees", 400)
    return r, d


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    auth: bool = True,
    token: str | None = None,
    timeout: float = _HTTP_TIMEOUT_S,
) -> dict:
    """Authenticated JSON request to an LCO endpoint with retry/backoff.

    4xx responses are not retried (they are caller errors); 5xx and network
    failures are retried up to ``_RETRIES`` with exponential backoff.
    """
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
    else:
        clean = None

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    token_value = None
    if auth:
        token_value = token or load_token()
        headers["Authorization"] = f"Token {token_value}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    cache_key = None
    if method.upper() == "GET" and body is None:
        token_digest = hashlib.sha256((token_value or "").encode("utf-8")).hexdigest()[:16]
        cache_key = (method.upper(), url, bool(auth), token_digest)
        now = time.time()
        with _GET_CACHE_LOCK:
            hit = _GET_CACHE.get(cache_key)
            if hit and (now - hit[0]) <= _GET_CACHE_TTL_S:
                return copy.deepcopy(hit[1])
            if hit:
                _GET_CACHE.pop(cache_key, None)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    last: LcoError | None = None
    for attempt in range(_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                out = json.loads(raw) if raw.strip() else {}
                if cache_key is not None:
                    with _GET_CACHE_LOCK:
                        _GET_CACHE[cache_key] = (time.time(), copy.deepcopy(out))
                        if len(_GET_CACHE) > _GET_CACHE_MAX:
                            oldest = min(_GET_CACHE.items(), key=lambda kv: kv[1][0])[0]
                            _GET_CACHE.pop(oldest, None)
                return out
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if 400 <= e.code < 500:
                raise LcoError(f"LCO API rejected the request (HTTP {e.code})", e.code, detail)
            last = LcoError(f"LCO API server error (HTTP {e.code})", 502, detail)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = LcoError(f"LCO API request failed: {e}", 502)
        except json.JSONDecodeError as e:
            last = LcoError(f"LCO API returned invalid JSON: {e}", 502)
        if attempt < _RETRIES - 1:
            time.sleep(_BACKOFF_S * (2 ** attempt))
    raise last or LcoError("LCO API request failed", 502)


def get_proposals(token: str | None = None) -> dict:
    return _request("GET", f"{OBS_PORTAL_BASE}/proposals/", token=token)


def get_requestgroups(proposal: str, token: str | None = None) -> dict:
    pid = validate_proposal(proposal)
    return _request("GET", f"{OBS_PORTAL_BASE}/requestgroups/", params={"proposal": pid}, token=token)


def max_allowable_ipp(payload: dict, token: str | None = None) -> dict:
    return _request(
        "POST", f"{OBS_PORTAL_BASE}/requestgroups/max_allowable_ipp/", body=payload, token=token
    )


def submit_requestgroup(payload: dict, token: str | None = None) -> dict:
    """POST a requestgroup for real. Guarded: requires the server-side submit
    switch. Callers must additionally enforce the dry-run + confirm gate."""
    if not submit_allowed():
        raise LcoError(
            "Live LCO submission is disabled on this server "
            "(set MUSCAT_LCO_ALLOW_SUBMIT=1 to enable).",
            status=403,
        )
    return _request("POST", f"{OBS_PORTAL_BASE}/requestgroups/", body=payload, token=token)


# --------------------------------------------------------------------------- #
# Archive search & download
# --------------------------------------------------------------------------- #

# Allow-list of archive query params we forward (everything else is dropped).
_ARCHIVE_PARAMS = {
    "proposal_id", "OBJECT", "SITEID", "TELID", "INSTRUME", "FILTER",
    "RLEVEL", "OBSTYPE", "start", "end", "covers", "limit", "offset",
    "basename", "configuration_type", "reduction_level", "public",
}


def archive_search(filters: dict, token: str | None = None) -> dict:
    params = {k: v for k, v in (filters or {}).items() if k in _ARCHIVE_PARAMS}
    limit = params.get("limit")
    if limit is not None:
        params["limit"] = min(_pos_int(limit, "limit"), 1000)
    # Archive auth is only needed for proprietary data; send it when available.
    use_auth = has_token() if token is None else True
    return _request("GET", f"{ARCHIVE_BASE}/frames/", params=params, auth=use_auth, token=token)


def frame_date_dir(frame: dict) -> str:
    """Compact ``YYMMDD`` directory from a frame's observation timestamp.

    Prefers ``DAY_OBS``; falls back to the date portion of ``DATE_OBS``.
    """
    day_obs = (frame.get("DAY_OBS") or "").strip()
    raw = day_obs or (frame.get("DATE_OBS") or "").strip()
    if not raw:
        raise LcoError("frame is missing an observation date", 400)
    digits = raw[:10].replace("-", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        return digits[2:8]  # YYYYMMDD -> YYMMDD
    if len(digits) == 6 and digits.isdigit():
        return digits
    raise LcoError(f"could not parse frame observation date: {raw!r}", 400)


def download_dir(inst: str, date: str) -> pathlib.Path:
    """Resolve ``<MUSCAT_LCO_DIR>/<inst>/<date>`` or the per-instrument data dir."""
    if inst not in INSTRUMENTS:
        raise LcoError("unknown instrument", 400)
    if not phot.valid_date(date):
        raise LcoError("invalid date (expected YYMMDD)", 400)
    root = os.environ.get("MUSCAT_LCO_DIR")
    if root:
        return pathlib.Path(root) / inst / date
    return phot.raw_data_dir(inst, date)


def frame_dest(inst: str, date: str, filename: str) -> pathlib.Path:
    name = ("" if filename is None else str(filename)).strip()
    if not _FILENAME_RE.match(name) or "/" in name or ".." in name:
        raise LcoError("invalid filename", 400)
    return (download_dir(inst, date) / name).resolve(strict=False)


def download_frame(url: str, dest: pathlib.Path, overwrite: bool = False) -> dict:
    """Stream a frame URL to ``dest``. Never overwrites unless asked.

    The archive returns a (often presigned, unauthenticated) URL in each frame's
    ``url`` field, so this is a plain GET, not an authenticated portal call.
    """
    dest = pathlib.Path(dest)
    if dest.exists() and not overwrite:
        return {"status": "exists", "path": str(dest)}
    if not (url or "").lower().startswith(("http://", "https://")):
        raise LcoError("invalid download url", 400)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        os.replace(tmp, dest)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise LcoError(f"download failed: {e}", 502)
    return {"status": "downloaded", "path": str(dest), "bytes": dest.stat().st_size}


def download_frames(frames: list[dict], overwrite: bool = False) -> list[dict]:
    """Download a batch, one per-file result each (errors captured, not raised)."""
    results = []
    for frame in frames or []:
        filename = frame.get("filename") or frame.get("basename")
        url = frame.get("url")
        entry = {"filename": filename}
        try:
            inst = infer_archive_instrument(frame)
            date = frame_date_dir(frame)
            dest = frame_dest(inst, date, filename)
            res = download_frame(url, dest, overwrite=overwrite)
            entry["instrument"] = inst
            entry.update(res)
        except LcoError as e:
            entry.update({"status": "error", "error": str(e)})
        results.append(entry)
    return results


# --------------------------------------------------------------------------- #
# Transit window generation (pure, stdlib-only)
# --------------------------------------------------------------------------- #

_UNIX_EPOCH_JD = 2440587.5  # JD at 1970-01-01T00:00:00Z


def _jd_to_dt(jd: float) -> datetime.datetime:
    # Round to the nearest second so float error doesn't show 01:44:59 for 01:45:00.
    seconds = round((jd - _UNIX_EPOCH_JD) * 86400.0)
    return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=seconds)


def _to_jd(value) -> float:
    """Accept a JD float, an ISO datetime string, or a date (``YYYY-MM-DD``)."""
    if isinstance(value, (int, float)):
        return float(value)
    s = ("" if value is None else str(value)).strip()
    if not s:
        raise LcoError("missing date/time value", 400)
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise LcoError(f"could not parse date/time: {s!r}", 400)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    delta = dt.astimezone(datetime.timezone.utc) - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return delta.total_seconds() / 86400.0 + _UNIX_EPOCH_JD


def _iso_utc(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def generate_windows(
    t0: float,
    period: float,
    duration_hours: float,
    range_start,
    range_end,
    pad_before_min: float = 0.0,
    pad_after_min: float = 0.0,
) -> list[dict]:
    """Transit observation windows over a UTC date range.

    ``t0`` and ``period`` are the ephemeris (days; ``t0`` typically BJD). Each
    window spans ``[mid - duration/2 - pad_before, mid + duration/2 + pad_after]``
    for every integer cycle whose mid-transit falls inside the range.

    Note on timescale: ``t0`` is usually BJD_TDB while LCO windows are UTC. The
    barycentric/TDB offset is at most ~8 minutes; callers should set padding wide
    enough (baseline is normally tens of minutes) to absorb it. Windows are
    coarse scheduling bounds, not precise mid-transit predictions.
    """
    period = _pos_float(period, "period")
    duration_hours = _pos_float(duration_hours, "duration")
    t0 = float(t0)
    start_jd = _to_jd(range_start)
    end_jd = _to_jd(range_end)
    if end_jd < start_jd:
        raise LcoError("range end is before range start", 400)
    if pad_before_min < 0 or pad_after_min < 0:
        raise LcoError("padding must be non-negative", 400)

    half = (duration_hours / 24.0) / 2.0
    pad_b = pad_before_min / 1440.0
    pad_a = pad_after_min / 1440.0

    n_lo = math.ceil((start_jd - t0) / period)
    n_hi = math.floor((end_jd - t0) / period)
    if n_hi < n_lo:
        return []
    if (n_hi - n_lo + 1) > _MAX_WINDOWS:
        raise LcoError(
            f"date range yields too many windows (>{_MAX_WINDOWS}); narrow the range",
            400,
        )

    windows = []
    for n in range(n_lo, n_hi + 1):
        mid = t0 + n * period
        windows.append(
            {
                "epoch": n,
                "mid_jd": mid,
                "start": _iso_utc(_jd_to_dt(mid - half - pad_b)),
                "mid": _iso_utc(_jd_to_dt(mid)),
                "end": _iso_utc(_jd_to_dt(mid + half + pad_a)),
            }
        )
    return windows


# --------------------------------------------------------------------------- #
# RequestGroup payload construction
# --------------------------------------------------------------------------- #


def _build_target(params: dict) -> dict:
    name = _req_str(params.get("target_name"), "target_name", 1, 100)
    ra, dec = validate_radec(params.get("ra"), params.get("dec"))
    return {
        "name": name,
        "type": "ICRS",
        "ra": ra,
        "dec": dec,
        "proper_motion_ra": _safe_float(params.get("pm_ra")) or 0.0,
        "proper_motion_dec": _safe_float(params.get("pm_dec")) or 0.0,
        "epoch": 2000,
    }


def _build_constraints(params: dict) -> dict:
    max_airmass = _safe_float(params.get("max_airmass"))
    if max_airmass is None:
        max_airmass = 1.6
    if not (1.0 <= max_airmass <= 3.0):
        raise LcoError("max_airmass must be in [1.0, 3.0]", 400)
    min_lunar = _safe_float(params.get("min_lunar_distance"))
    if min_lunar is None:
        min_lunar = 30.0
    if not (0.0 <= min_lunar <= 180.0):
        raise LcoError("min_lunar_distance must be in [0, 180]", 400)
    return {"max_airmass": max_airmass, "min_lunar_distance": min_lunar}


def _build_windows(windows) -> list[dict]:
    if not isinstance(windows, list) or not windows:
        raise LcoError("at least one observation window is required", 400)
    out = []
    for w in windows:
        if not isinstance(w, dict):
            raise LcoError("window must be an object with start and end", 400)
        start = w.get("start")
        end = w.get("end")
        if not start or not end:
            raise LcoError("each window requires start and end", 400)
        # Validate parseability; keep original strings.
        s_jd = _to_jd(start)
        e_jd = _to_jd(end)
        if e_jd <= s_jd:
            raise LcoError("window end must be after window start", 400)
        out.append({"start": str(start), "end": str(end)})
    return out


def _muscat_instrument_configs(params: dict) -> list[dict]:
    exptimes = params.get("exposure_times") or {}
    et: dict[str, float] = {}
    for band in _MUSCAT_BANDS:
        if exptimes.get(band) not in (None, ""):
            et[band] = _pos_float(exptimes.get(band), f"exposure_time_{band}")
    if not et:
        raise LcoError("at least one g/r/i/z exposure time is required", 400)

    mode = (params.get("exposure_mode") or "SYNCHRONOUS").strip().upper()
    if mode not in ("SYNCHRONOUS", "ASYNCHRONOUS"):
        raise LcoError("exposure_mode must be SYNCHRONOUS or ASYNCHRONOUS", 400)

    count = _pos_int(params.get("exposure_count", 1), "exposure_count")
    readout = _req_token(params.get("readout_mode") or "MUSCAT_FAST", "readout_mode")

    narrowband = params.get("narrowband") or {}
    optical: dict[str, str] = {}
    for band in _MUSCAT_BANDS:
        pos = (narrowband.get(band) or "out").strip().lower()
        if pos not in ("in", "out"):
            raise LcoError(f"narrowband position for {band} must be 'in' or 'out'", 400)
        optical[f"narrowband_{band}_position"] = pos

    extra = {"exposure_mode": mode}
    for band, value in et.items():
        extra[f"exposure_time_{band}"] = value

    return [
        {
            "exposure_count": count,
            "exposure_time": max(et.values()),
            "mode": readout,
            "optical_elements": optical,
            "extra_params": extra,
        }
    ]


def _sinistro_instrument_configs(params: dict) -> list[dict]:
    filt = _req_token(params.get("filter"), "filter")
    exptime = _pos_float(params.get("exposure_time"), "exposure_time")
    count = _pos_int(params.get("exposure_count", 1), "exposure_count")
    readout = _req_token(params.get("readout_mode") or "central_2k_2x2", "readout_mode")
    return [
        {
            "exposure_count": count,
            "exposure_time": exptime,
            "mode": readout,
            "optical_elements": {"filter": filt},
        }
    ]


def build_requestgroup(kind: str, params: dict) -> dict:
    """Construct a generic-imaging requestgroup for ``muscat`` or ``sinistro``."""
    kind = (kind or "").strip().lower()
    name = _req_str(params.get("name"), "name", 1, 50)
    proposal = validate_proposal(params.get("proposal"))
    target = _build_target(params)
    constraints = _build_constraints(params)
    windows = _build_windows(params.get("windows"))

    ipp = _safe_float(params.get("ipp_value"))
    if ipp is None:
        ipp = 1.0
    if not (0.5 <= ipp <= 2.0):
        raise LcoError("ipp_value must be in [0.5, 2.0]", 400)

    kind = (kind or "").strip().lower()
    if kind in ("muscat", "muscat3", "muscat4"):
        instrument_type = _MUSCAT_INSTRUMENT_TYPE
        telescope_class = "2m0"
        instrument_configs = _muscat_instrument_configs(params)
        if not params.get("site"):
            if kind == "muscat3":
                params["site"] = "ogg"
            elif kind == "muscat4":
                params["site"] = "coj"
    elif kind == "sinistro":
        instrument_type = _SINISTRO_INSTRUMENT_TYPE
        telescope_class = "1m0"
        instrument_configs = _sinistro_instrument_configs(params)
    else:
        raise LcoError("imaging kind must be 'muscat', 'muscat3', 'muscat4', or 'sinistro'", 400)

    configuration = {
        "type": "EXPOSE",
        "instrument_type": instrument_type,
        "target": target,
        "constraints": constraints,
        "acquisition_config": {"mode": "OFF"},
        "guiding_config": {"mode": "ON", "optional": True},
        "instrument_configs": instrument_configs,
    }

    location = {"telescope_class": telescope_class}
    if params.get("site"):
        location["site"] = _req_token(params.get("site"), "site")

    request = {"configurations": [configuration], "windows": windows, "location": location}
    return {
        "name": name,
        "proposal": proposal,
        "ipp_value": ipp,
        "operator": "SINGLE",
        "observation_type": "NORMAL",
        "requests": [request],
    }


def payload_hash(payload: dict) -> str:
    """Stable hash of a validated payload, used to gate submit on a prior dry-run."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
