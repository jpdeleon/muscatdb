# src/muscat_db/lco.py
"""
Helper module for interacting with the LCO API.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import urllib.request
import urllib.parse
from pathlib import Path


class LcoError(Exception):
    """Structured LCO API error."""

    def __init__(self, message: str, status: int = 500, detail: str | None = None):
        self.message = message
        self.status = status
        self.detail = detail
        super().__init__(f"[{status}] {message}" + (f" - {detail}" if detail else ""))

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.message, "detail": self.detail, "status": self.status}


def _get_lco_api_token() -> str:
    """Return the LCO API token from the environment, raising an error if absent."""
    token = os.environ.get("LCO_API_TOKEN")
    if not token:
        raise LcoError(
            "LCO_API_TOKEN is not configured",
            status=503,
            detail="The server is missing the LCO_API_TOKEN secret needed to make this call.",
        )
    return token


def config_state() -> dict:
    """Return the configuration state for LCO variables. No secrets exposed."""
    return {
        "token_configured": bool(os.environ.get("LCO_API_TOKEN")),
        "download_root_configured": bool(os.environ.get("MUSCAT_LCO_DIR")),
        "submit_allowed": os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") == "1",
    }


def _lco_api_request(url: str, method: str = "GET", data: dict | None = None, is_archive: bool = False) -> dict:
    """Make an authenticated request to the LCO API."""
    token = _get_lco_api_token()
    headers = {"Authorization": ("Bearer " if is_archive else "Token ") + token, "Content-Type": "application/json"}
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


def get_proposals() -> dict:
    """Fetch the current user's active proposals."""
    return _lco_api_request("https://observe.lco.global/api/proposals/?state=ACTIVE")


def get_requestgroups(proposal: str) -> dict:
    """Fetch request groups for a given proposal."""
    if not proposal:
        raise LcoError("Proposal ID is required", status=400)
    url = f"https://observe.lco.global/api/requestgroups/?proposal={urllib.parse.quote(proposal)}"
    return _lco_api_request(url)


def archive_search(filters: dict) -> dict:
    """Search the LCO archive."""
    base_url = "https://archive-api.lco.global/frames/"
    params = urllib.parse.urlencode({k: v for k, v in filters.items() if v})
    url = f"{base_url}?{params}"
    return _lco_api_request(url, is_archive=True)


def infer_archive_instrument(frame: dict) -> str:
    """Infer the muscat-db instrument name from LCO archive frame metadata."""
    site = str(frame.get("SITEID") or "").lower()
    tel = str(frame.get("TELID") or "").lower()
    instrume = str(frame.get("INSTRUME") or "").lower()

    if site == "ogg" and tel.startswith("2m0") and "muscat" in instrume:
        return "muscat3"
    if site == "coj" and tel.startswith("2m0") and "muscat" in instrume:
        return "muscat4"
    if tel.startswith("1m0"):
        return "sinistro"

    raise LcoError("Could not infer instrument", detail=f"site={site}, tel={tel}, instrume={instrume}")


def frame_dest(instrument: str, obsdate: str, filename: str) -> Path:
    """Return the destination path for a downloaded frame."""
    lco_dir = os.environ.get("MUSCAT_LCO_DIR")
    if lco_dir:
        root = Path(lco_dir)
    else:
        data_dir = os.environ.get("MUSCAT_DATA_DIR")
        if not data_dir:
            raise LcoError("MUSCAT_LCO_DIR or MUSCAT_DATA_DIR must be set", status=503)
        root = Path(data_dir)
    return root / instrument / obsdate / filename


def download_frames(frames: list[dict], overwrite: bool = False) -> list[dict]:
    """Download frames from the LCO archive."""
    results = []
    for frame in frames:
        filename = frame.get("filename") or frame.get("basename")
        if not filename:
            results.append({"filename": "unknown", "status": "error", "error": "missing filename"})
            continue
        
        status = {"filename": filename, "status": "pending"}
        results.append(status)

        try:
            instrument = infer_archive_instrument(frame)
            date_obs = (frame.get("DATE_OBS") or "").split("T")[0].replace("-", "")
            if len(date_obs) >= 6:
                obsdate = date_obs[2:]
            else:
                raise LcoError("Could not determine obsdate")

            dest = frame_dest(instrument, obsdate, filename)

            if dest.exists() and not overwrite:
                status["status"] = "exists"
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            
            url = frame.get("url")
            if not url:
                status["status"] = "error"
                status["error"] = "missing download url"
                continue

            urllib.request.urlretrieve(url, dest)
            status["status"] = "downloaded"

        except LcoError as e:
            status["status"] = "error"
            status["error"] = e.message
        except Exception as e:
            status["status"] = "error"
            status["error"] = str(e)
            
    return results


def generate_windows(t0: float, period: float, duration_h: float, start_dt: str, end_dt: str, pad_before_min: float, pad_after_min: float) -> list[dict]:
    """Generate transit windows within a date range."""
    if not all([start_dt, end_dt]):
        raise LcoError("Date range is required", status=400)
    
    start = datetime.datetime.fromisoformat(start_dt + "T00:00:00").replace(tzinfo=datetime.timezone.utc)
    end = datetime.datetime.fromisoformat(end_dt + "T23:59:59").replace(tzinfo=datetime.timezone.utc)
    
    # JD for Unix epoch is 2440587.5. BJD is close enough for this purpose.
    t0_dt = datetime.datetime.fromtimestamp((t0 - 2440587.5) * 86400, tz=datetime.timezone.utc)
    
    epoch_at_start = math.floor((start - t0_dt).total_seconds() / (period * 86400.0))
    
    windows = []
    current_epoch = epoch_at_start
    while True:
        mid_bjd = t0 + current_epoch * period
        # Recalculate mid_dt from BJD each time to avoid float drift
        mid_dt = datetime.datetime.fromtimestamp((mid_bjd - 2440587.5) * 86400, tz=datetime.timezone.utc)

        if mid_dt > end:
            break
            
        if mid_dt >= start:
            start_obs = mid_dt - datetime.timedelta(hours=duration_h / 2.0, minutes=pad_before_min)
            end_obs = mid_dt + datetime.timedelta(hours=duration_h / 2.0, minutes=pad_after_min)
            
            windows.append({
                "epoch": int(current_epoch),
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

def build_requestgroup(kind: str, params: dict) -> dict:
    """Construct the requestgroup payload for an observation."""
    if not all(params.get(k) for k in ["name", "proposal", "target_name", "ra", "dec"]):
        raise LcoError("Missing required scheduling parameters", status=400)

    target = {
        "name": params["target_name"],
        "type": "ICRS",
        "ra": params["ra"],
        "dec": params["dec"],
    }

    # These constraints are defined at the request level, but get copied into
    # the configuration level by this function, as per the LCO examples.
    constraints = {
        "max_airmass": params.get("max_airmass", 1.6),
        "min_lunar_distance": params.get("min_lunar_distance", 30),
    }

    configurations = []
    instrument_type = ""
    if kind in ("muscat", "muscat3", "muscat4"):
        if not params.get("exposure_times"):
            raise LcoError("Exposure times are required for MuSCAT instruments", status=400)
        
        # For MuSCAT, one instrument_config is created per filter.
        instrument_configs = [
            {
                "exposure_time": params["exposure_times"].get(b, 0),
                "exposure_count": params.get("exposure_count", 1),
                "mode": params.get("readout_mode", "MUSCAT_FAST"),
                "optical_elements": {
                    "filter": b,
                    "narrowband_g_position": params.get("narrowband", {}).get("g", "out"),
                    "narrowband_i_position": params.get("narrowband", {}).get("i", "out"),
                    "narrowband_r_position": params.get("narrowband", {}).get("r", "out"),
                    "narrowband_z_position": params.get("narrowband", {}).get("z", "out"),
                },
                "extra_params": {
                    "exposure_mode": params.get("exposure_mode", "ASYNCHRONOUS"),
                    "exposure_time_g": params["exposure_times"].get("g", 0),
                    "exposure_time_i": params["exposure_times"].get("i", 0),
                    "exposure_time_r": params["exposure_times"].get("r", 0),
                    "exposure_time_z": params["exposure_times"].get("z", 0),
                },
            } for b in ["g", "r", "i", "z"] if params["exposure_times"].get(b, 0) > 0
        ]
        instrument_type = "2M0-SCICAM-MUSCAT"
        configurations.append({
            "type": params.get("type", "REPEAT_EXPOSE"),
            "repeat_duration": params.get("repeat_duration"),
            "instrument_configs": instrument_configs,
            "acquisition_config": {"mode": "WCS"},
            "guiding_config": {"mode": params.get("guiding_config", "ON"), "optional": True},
            "constraints": {
                "max_airmass": params.get("max_airmass", 2.5),
                "min_lunar_distance": params.get("min_lunar_distance", 18),
                "max_seeing": params.get("max_seeing"),
                "min_transparency": params.get("min_transparency"),
                "extra_params": {}
            },
            "target": target
        })
    elif kind == "sinistro":
        mode = params.get("readout_mode", "central_2k_2x2")
        binning = 2 if "2x2" in mode else 1
        instrument_configs = [{
            "exposure_count": params.get("exposure_count", 1),
            "exposure_time": params.get("exposure_time", 60),
            "mode": mode,
            "optical_elements": {"filter": params.get("filter", "rp")},
            "extra_params": {
                "bin_x": binning,
                "bin_y": binning,
                "offset_ra": 0,
                "offset_dec": 0
            }
        }]
        instrument_type = "1M0-SCICAM-SINISTRO"
        configurations.append({
            "type": params.get("type", "EXPOSE"),
            "instrument_configs": instrument_configs,
            "acquisition_config": {"mode": "WCS"},
            "guiding_config": {"mode": params.get("guiding_config", "ON"), "optional": True},
            "constraints": constraints,
            "target": target
        })
    else:
        raise LcoError(f"Unsupported instrument kind for scheduling: {kind}", status=400)

    location = {}
    if params.get("site"):
        location["telescope_class"] = "1m0" if kind == "sinistro" else "2m0"
        location["site"] = params["site"]
    
    obs_type = "NORMAL"

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

def max_allowable_ipp(request_group: dict) -> dict:
    """Run the max-allowable-IPP dry-run."""
    url = "https://observe.lco.global/api/requestgroups/max_allowable_ipp/"
    return _lco_api_request(url, method="POST", data=request_group)

def submit_requestgroup(request_group: dict) -> dict:
    """Submit a live observation request."""
    if os.environ.get("MUSCAT_LCO_ALLOW_SUBMIT") != "1":
        raise LcoError(
            "Live submission is disabled on the server",
            status=403,
            detail="To enable, set MUSCAT_LCO_ALLOW_SUBMIT=1 in the server environment.",
        )
    url = "https://observe.lco.global/api/requestgroups/"
    return _lco_api_request(url, method="POST", data=request_group)
