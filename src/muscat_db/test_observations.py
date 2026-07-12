"""Planning and persistence for closed-loop LCO test observations.

The planner is deliberately pure and deterministic.  Network-derived FOV,
catalogue, visibility, and capability results are inputs with provenance; this
keeps plans reproducible and makes stale/approximate evidence visible.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from muscat_db.database import db_path

ANALYSIS_VERSION = "test-observation-v1"
SUPPORTED = {"muscat3", "muscat4", "sinistro"}
STATES = {"draft", "validated", "submitted", "pending", "downloading", "analyzing", "complete", "partial", "failed"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS test_observations (
 id TEXT PRIMARY KEY, target TEXT NOT NULL, instrument TEXT NOT NULL, site TEXT NOT NULL,
 transit_json TEXT NOT NULL DEFAULT '{}', plan_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
 request_ids_json TEXT NOT NULL DEFAULT '[]', payload_hash TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'draft', analysis_version TEXT NOT NULL,
 recommendation_json TEXT NOT NULL DEFAULT '{}', failure_detail TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class TestObservationError(ValueError):
    __test__ = False
    pass


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TestObservationError(f"{label} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise TestObservationError(f"{label} must be positive and finite")
    return result


def _focus_values(nominal: float, capabilities: dict) -> tuple[list[float], str | None]:
    fetched = capabilities.get("fetched_at")
    age_s = None
    if fetched:
        try:
            age_s = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            pass
    focus = capabilities.get("defocus") or {}
    if age_s is not None and age_s > 86400:
        return [nominal], "instrument capabilities are stale; focus variation disabled"
    if not (focus.get("writable") and focus.get("field") == "defocus"):
        return [nominal], "no verified writable defocus capability; focus variation disabled"
    lo, hi = float(focus["min"]), float(focus["max"])
    step = float(focus.get("step") or 1.0)
    values = {max(lo, min(hi, nominal + delta)) for delta in (-step, 0.0, step)}
    return sorted(values), None


def generate_plan(payload: dict) -> dict:
    kind = str(payload.get("kind") or "").lower()
    if kind not in SUPPORTED:
        raise TestObservationError("test observations support muscat3, muscat4, and sinistro")
    fovs = list(payload.get("fov_candidates") or [])[:2]
    if len(fovs) != 2:
        raise TestObservationError("best and fallback FOV candidates are required")
    repeats = max(3, int(payload.get("repeats") or 3))
    budget = _number(payload.get("exposure_budget_s", 600), "exposure budget")
    nominal_focus = float(payload.get("defocus_mm") or 0.0)
    focuses, focus_warning = _focus_values(nominal_focus, payload.get("capabilities") or {})
    factors = (0.67, 1.0, 1.5)
    limits = payload.get("exposure_limits") or {}
    minimum, maximum = float(limits.get("min", 0.1)), float(limits.get("max", 600.0))
    removed: list[dict] = []

    if kind == "sinistro":
        nominal: dict[str, float] = {str(payload.get("filter") or "rp"): _number(payload.get("exposure_time"), "exposure time")}
    else:
        raw = payload.get("exposure_times") or {}
        nominal = {b: _number(raw.get(b), f"{b} exposure time") for b in ("g", "r", "i", "z")}

    exposures = []
    for factor in factors:
        values = {band: round(max(minimum, min(maximum, value * factor)), 6) for band, value in nominal.items()}
        if values in exposures:
            removed.append({"reason": "instrument-limit clipping produced duplicate", "factor": factor})
        else:
            exposures.append(values)

    saturated = payload.get("predicted_saturated") or {}
    candidates = []
    for fov_index, fov in enumerate(fovs):
        for exposure in exposures:
            factor = min(exposure[b] / nominal[b] for b in nominal)
            if saturated.get(f"{fov_index}:{factor:.2f}"):
                removed.append({"reason": "predicted saturation threshold exceeded", "fov_index": fov_index, "exposure_times": exposure})
                continue
            for focus in focuses:
                candidates.append({
                    "fov_index": fov_index, "center_ra": fov.get("center_ra"), "center_dec": fov.get("center_dec"),
                    "pa_deg": fov.get("pa_deg", 0), "exposure_times": exposure,
                    "exposure_time": max(exposure.values()), "defocus_mm": focus, "repeats": repeats,
                    "priority": (0 if abs(factor - 1) < 0.02 else 1, 0 if focus == nominal_focus else 2, fov_index),
                })
    candidates.sort(key=lambda c: c["priority"])
    kept, used = [], 0.0
    for candidate in candidates:
        cost = candidate["exposure_time"] * repeats
        if used + cost <= budget:
            candidate.pop("priority")
            kept.append(candidate)
            used += cost
        else:
            candidate.pop("priority")
            removed.append({**candidate, "reason": "exposure budget exceeded"})
    if not kept or {c["fov_index"] for c in kept} != {0, 1}:
        raise TestObservationError("10-minute budget cannot retain nominal tests for both FOVs")
    overhead = float(payload.get("estimated_overhead_s") or 0.0) * sum(c["repeats"] for c in kept)
    warnings = [w for w in (focus_warning, payload.get("window_mismatch")) if w]
    return {
        "version": ANALYSIS_VERSION, "kind": kind, "target": payload.get("target_name"), "site": payload.get("site"),
        "transit": payload.get("transit") or {}, "window": payload.get("test_window") or {},
        "fov_candidates": fovs, "configurations": kept, "removed_combinations": removed,
        "exposure_budget_s": budget, "estimated_exposure_s": round(used, 6),
        "estimated_wall_clock_s": round(used + overhead, 6), "warnings": warnings,
        "provenance": payload.get("provenance") or {}, "capabilities": payload.get("capabilities") or {},
    }


def request_configurations(plan: dict, base: dict) -> list[dict]:
    result = []
    for item in plan["configurations"]:
        override = {"type": "EXPOSE", "exposure_count": item["repeats"], "defocus": item["defocus_mm"]}
        if plan["kind"] == "sinistro":
            override.update(exposure_time=item["exposure_time"], filter=next(iter(item["exposure_times"])))
        else:
            override["exposure_times"] = item["exposure_times"]
        # Pointing is represented by the verified ICRS target coordinates used
        # by LCO, not unverified offset fields.
        override.update(ra=item["center_ra"], dec=item["center_dec"])
        result.append(override)
    return result


def _connect(path=None):
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def create_record(plan: dict, path=None) -> dict:
    identifier = str(uuid.uuid4())
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    with _connect(path) as conn:
        conn.execute("INSERT INTO test_observations (id,target,instrument,site,transit_json,plan_json,analysis_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (identifier, plan.get("target") or "", plan["kind"], plan.get("site") or "", json.dumps(plan.get("transit") or {}, sort_keys=True), json.dumps(plan, sort_keys=True), ANALYSIS_VERSION, now, now))
    return get_record(identifier, path)


def get_record(identifier: str, path=None) -> dict:
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM test_observations WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise KeyError(identifier)
    result = dict(row)
    for key in ("transit_json", "plan_json", "result_json", "request_ids_json", "recommendation_json"):
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    return result


def update_record(identifier: str, *, state: str, payload_hash: str | None = None, request_ids=None, failure_detail: str = "", path=None) -> dict:
    if state not in STATES:
        raise TestObservationError(f"invalid lifecycle state: {state}")
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    with _connect(path) as conn:
        changed = conn.execute("UPDATE test_observations SET state=?, payload_hash=COALESCE(?,payload_hash), request_ids_json=COALESCE(?,request_ids_json), failure_detail=?, updated_at=? WHERE id=?",
                               (state, payload_hash, json.dumps(request_ids) if request_ids is not None else None, failure_detail, now, identifier))
        if not changed.rowcount:
            raise KeyError(identifier)
    return get_record(identifier, path)
