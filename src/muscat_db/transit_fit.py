"""Helpers for the Transit Fit page: manage config generation (fit.yaml, sys.yaml),
run the transit-fit pipeline, poll logs, and return outputs/plots.
"""
from __future__ import annotations

import csv
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import IO
import yaml

from muscat_db.instruments import INSTRUMENTS
from muscat_db.photometry import output_base, valid_date, _conda_env_python, _tail

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.resolve()

@dataclass
class TransitFitJob:
    key: str
    inst: str
    date: str
    target: str
    cmd: list[str]
    proc: subprocess.Popen
    logf: IO
    log_path: pathlib.Path
    started_at: float = field(default_factory=time.time)
    state: str = "running"  # running | done | error | cancelled
    returncode: int | None = None
    cancelled: bool = False


_FIT_JOBS: dict[str, TransitFitJob] = {}
_FIT_LOCK = threading.Lock()


def fit_job_key(inst: str, date: str, target: str) -> str:
    return f"{inst}/{date}/{target.replace(' ', '')}"


def _timer_prefix() -> list[str]:
    """Resolve how to invoke the timer-fit tool, using the timer conda env."""
    env = "timer"
    conda_py = _conda_env_python(env)
    if conda_py:
        timer_fit_path = pathlib.Path(conda_py).parent / "timer-fit"
        if timer_fit_path.is_file():
            return [str(timer_fit_path)]
        return [conda_py, "-m", "timer.fit"]

    if shutil.which("conda"):
        return ["conda", "run", "-n", env, "--no-capture-output", "timer-fit"]

    if shutil.which("timer-fit"):
        return ["timer-fit"]
    return ["timer-fit"]


def get_csv_lightcurves(inst: str, date: str, target: str) -> list[pathlib.Path]:
    """Find the CSV lightcurves outputted by the Photometry page for a target."""
    rdir = output_base() / inst / date
    if not rdir.is_dir():
        return []

    target_clean = target.replace(" ", "").replace("-", "").lower()
    csvs = []
    for f in rdir.glob("*.csv"):
        if f.name.startswith("_") or "summary" in f.name:
            continue
        fname = f.name.lower()
        if inst.lower() in fname and date in fname:
            t_part = fname.split(f"_{inst.lower()}_")[0]
            t_clean = t_part.replace("-", "")
            if t_clean == target_clean:
                csvs.append(f)
    return sorted(csvs)


def get_target_parameters(target_name: str) -> dict:
    """Retrieve default stellar and planetary parameters from the catalog."""
    params = {
        "teff": 5778.0, "teff_unc": 100.0,
        "logg": 4.4, "logg_unc": 0.1,
        "feh": 0.0, "feh_unc": 0.1,
        "planets": "b",
        "period": 1.0, "period_unc": 0.001,
        "t0": 2450000.0, "t0_unc": 0.01,
        "dur": 0.1, "dur_unc": 0.01,
        "ror": 0.05, "ror_unc": 0.005,
        "b": 0.0, "b_unc": 0.1,
    }

    csv_path = _REPO_ROOT / "data" / "muscatdb_targets_old.csv"
    if not csv_path.exists():
        return params

    norm_target = target_name.replace("-", "").replace(" ", "").replace("_", "").upper()
    try:
        with open(csv_path, errors="replace") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                name_val = (row.get("name") or "").strip()
                norm_name = name_val.replace("-", "").replace(" ", "").replace("_", "").upper()
                if norm_target in norm_name or norm_name in norm_target:
                    # Parse stellar details
                    if row.get("Teff_GAIA_sg2"):
                        try: params["teff"] = float(row["Teff_GAIA_sg2"])
                        except ValueError: pass
                    # Parse planet period
                    if row.get("period"):
                        try: params["period"] = float(row["period"])
                        except ValueError: pass
                    elif row.get("period_sg1"):
                        try: params["period"] = float(row["period_sg1"])
                        except ValueError: pass
                    if row.get("period_error"):
                        try: params["period_unc"] = float(row["period_error"])
                        except ValueError: pass
                    elif row.get("period_error_sg1"):
                        try: params["period_unc"] = float(row["period_error_sg1"])
                        except ValueError: pass
                    # Parse planet t0
                    if row.get("t0"):
                        try: params["t0"] = float(row["t0"])
                        except ValueError: pass
                    elif row.get("t0_sg1"):
                        try: params["t0"] = float(row["t0_sg1"])
                        except ValueError: pass
                    if row.get("t0_error"):
                        try: params["t0_unc"] = float(row["t0_error"])
                        except ValueError: pass
                    elif row.get("t0_error_sg1"):
                        try: params["t0_unc"] = float(row["t0_error_sg1"])
                        except ValueError: pass
                    # Parse planet duration
                    if row.get("duration"):
                        try: params["dur"] = float(row["duration"])
                        except ValueError: pass
                    elif row.get("duration_sg1"):
                        try: params["dur"] = float(row["duration_sg1"])
                        except ValueError: pass
                    if row.get("duration_error_sg1"):
                        try: params["dur_unc"] = float(row["duration_error_sg1"])
                        except ValueError: pass
                    # Parse planet ror
                    if row.get("Rp_sg1"):
                        try: params["ror"] = float(row["Rp_sg1"])
                        except ValueError: pass
                    # Parse planet b
                    if row.get("Rs_a"):
                        try: params["b"] = float(row["Rs_a"])
                        except ValueError: pass
                    break
    except Exception:
        pass

    return params


def start_fit(
    inst: str,
    date: str,
    target: str,
    options: dict,
) -> dict:
    """Prepare inputs and launch a transit fit using the timer-fit script."""
    if inst not in INSTRUMENTS:
        return {"ok": False, "error": f"unknown instrument {inst!r}"}
    if not valid_date(date):
        return {"ok": False, "error": "date must be 6-digit yymmdd"}
    if not (target or "").strip():
        return {"ok": False, "error": "target is required"}

    csvs = get_csv_lightcurves(inst, date, target)
    if not csvs:
        return {"ok": False, "error": "No photometry CSV lightcurves found for this target."}

    # Working directory
    rdir = output_base() / inst / date / f"transit_fit_{target.replace(' ', '')}"
    rdir.mkdir(parents=True, exist_ok=True)

    # Clean old run files
    for p in rdir.glob("*"):
        if p.is_file() and p.name != "timer-fit.log":
            try: p.unlink()
            except OSError: pass
        elif p.is_dir() and p.name == "out":
            try: shutil.rmtree(p)
            except OSError: pass

    # Copy CSV files to working directory
    for c in csvs:
        shutil.copy2(c, rdir / c.name)

    # Build fit.yaml
    fit_data: dict = {"data": {}}
    for c in csvs:
        fname = c.name
        parts = fname.split(f"_{inst}_")
        if len(parts) > 1:
            rest = parts[1]
            band = rest.split(f"_{date}")[0]
        else:
            band = "gp"

        mapped_band = band.lower()
        if 'g' in mapped_band: mapped_band = 'g'
        elif 'r' in mapped_band or 'na' in mapped_band: mapped_band = 'r'
        elif 'i' in mapped_band: mapped_band = 'i'
        elif 'z' in mapped_band: mapped_band = 'z'
        else: mapped_band = 'g'

        fit_data["data"][band] = {
            "file": fname,
            "band": mapped_band,
            "trend": 1,
            "binsize": 0.00139,
            "format": "afphot"
        }

    planets_str = (options.get("planets") or "b").strip()
    fit_data["planets"] = planets_str
    
    tc_pred = (options.get("tc_pred") or "").strip()
    if tc_pred:
        try: fit_data["tc_pred"] = float(tc_pred)
        except ValueError: pass

    try: fit_data["tc_pred_unc"] = float(options.get("tc_pred_unc") or 0.04)
    except ValueError: fit_data["tc_pred_unc"] = 0.04

    fit_data["chromatic"] = options.get("chromatic") != "false"
    fit_data["fixed"] = options.get("fixed") or ["period", "u_star"]

    with open(rdir / "fit.yaml", "w") as f:
        yaml.safe_dump(fit_data, f, default_flow_style=False)

    # Build sys.yaml
    sys_data: dict = {
        "star": {
            "teff": [float(options.get("teff") or 5778.0), float(options.get("teff_unc") or 100.0)],
            "logg": [float(options.get("logg") or 4.4), float(options.get("logg_unc") or 0.1)],
            "feh": [float(options.get("feh") or 0.0), float(options.get("feh_unc") or 0.1)]
        },
        "planets": {}
    }

    planets_list = [p.strip() for p in planets_str.split(",") if p.strip()]
    for p in planets_list:
        sys_data["planets"][p] = {
            "period": [float(options.get("period") or 1.0), float(options.get("period_unc") or 0.001)],
            "t0": [float(options.get("t0") or 2450000.0), float(options.get("t0_unc") or 0.01)],
            "dur": [float(options.get("dur") or 0.1), float(options.get("dur_unc") or 0.01)],
            "ror": [float(options.get("ror") or 0.05), float(options.get("ror_unc") or 0.005)],
            "b": [float(options.get("b") or 0.0), float(options.get("b_unc") or 0.1)]
        }

    with open(rdir / "sys.yaml", "w") as f:
        yaml.safe_dump(sys_data, f, default_flow_style=False)

    # Launch process
    key = fit_job_key(inst, date, target)
    cmd = [*_timer_prefix(), "-v", str(rdir)]
    log_path = rdir / "timer-fit.log"
    logf = open(log_path, "w")
    logf.write(f"$ {shlex.join(cmd)}\n\n")
    logf.flush()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(rdir),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        logf.write(f"\nfailed to launch fitting: {exc}\n")
        logf.close()
        return {"ok": False, "error": f"failed to launch fitting: {exc}"}

    with _FIT_LOCK:
        _FIT_JOBS[key] = TransitFitJob(
            key=key, inst=inst, date=date, target=target,
            cmd=cmd, proc=proc, logf=logf, log_path=log_path,
        )
        # Record new job in the database
        from muscat_db.database import save_job
        save_job(
            type_="transit_fit",
            inst=inst,
            date=date,
            target=target,
            state="running",
            returncode=None,
            elapsed=0,
            started_at=_FIT_JOBS[key].started_at
        )

    return {"ok": True, "key": key}


def job_status(inst: str, date: str, target: str) -> dict:
    """Retrieve logs and status of an active transit fitting job."""
    key = fit_job_key(inst, date, target)
    with _FIT_LOCK:
        job = _FIT_JOBS.get(key)
        if job is None:
            # Check if output exists on disk
            rdir = output_base() / inst / date / f"transit_fit_{target.replace(' ', '')}"
            log_path = rdir / "timer-fit.log"
            if log_path.is_file():
                # Read completed job log
                return {"state": "done", "log": _tail(log_path), "returncode": 0, "elapsed": 0}
            return {"state": "none", "log": "", "returncode": None, "elapsed": 0}
        
        rc = job.proc.poll()
        if rc is None:
            state = "cancelling" if job.cancelled else "running"
        else:
            if job.cancelled:
                state = "cancelled"
            else:
                state = "done" if rc == 0 else "error"
            if job.state in ("running",):
                job.state = state
                job.returncode = rc
                try: job.logf.close()
                except OSError: pass
        log_path = job.log_path
        elapsed = time.time() - job.started_at
        
    return {
        "state": state,
        "returncode": rc,
        "log": _tail(log_path),
        "elapsed": round(elapsed),
    }


def _kill_after(proc: subprocess.Popen, grace: float = 6.0) -> None:
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try: proc.kill()
        except OSError: pass


def cancel_fit(inst: str, date: str, target: str) -> dict:
    """Terminate the running fitting process."""
    key = fit_job_key(inst, date, target)
    with _FIT_LOCK:
        job = _FIT_JOBS.get(key)
        if job is None:
            return {"ok": False, "error": "no job to cancel"}
        if job.proc.poll() is not None:
            return {"ok": True, "already_finished": True}
        job.cancelled = True
        proc = job.proc
        # Immediately record cancellation in the database
        from muscat_db.database import save_job
        save_job(
            type_="transit_fit",
            inst=inst,
            date=date,
            target=target,
            state="cancelled",
            returncode=-1,
            elapsed=round(time.time() - job.started_at),
            started_at=job.started_at,
            error_desc="Cancelled by user"
        )

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try: proc.terminate()
        except OSError: pass
        
    threading.Thread(target=_kill_after, args=(proc,), daemon=True).start()
    return {"ok": True, "key": key}


def get_all_jobs() -> list[dict]:
    """Retrieve all background fitting jobs."""
    with _FIT_LOCK:
        res = []
        for key, job in _FIT_JOBS.items():
            rc = job.proc.poll()
            if rc is None:
                state = "cancelling" if job.cancelled else "running"
            else:
                if job.cancelled:
                    state = "cancelled"
                else:
                    state = "done" if rc == 0 else "error"
                if job.state in ("running",):
                    job.state = state
                    job.returncode = rc
                    try: job.logf.close()
                    except OSError: pass
            
            elapsed = time.time() - job.started_at
            res.append({
                "key": job.key,
                "inst": job.inst,
                "date": job.date,
                "target": job.target,
                "type": "transit_fit",
                "state": state,
                "returncode": rc,
                "elapsed": round(elapsed),
                "started_at": job.started_at,
            })
        return sorted(res, key=lambda j: j["started_at"], reverse=True)


def get_fit_outputs(inst: str, date: str, target: str) -> dict:
    """Check and retrieve output files, plots, and summary values from completed run."""
    rdir = output_base() / inst / date / f"transit_fit_{target.replace(' ', '')}"
    out_dir = rdir / "out"

    outputs = {
        "has_any": False,
        "plots": [],
        "summary": None,
        "has_log": False
    }

    if (rdir / "timer-fit.log").is_file():
        outputs["has_log"] = True

    if not out_dir.is_dir():
        return outputs

    # Check for fit.png and data.png
    for p_name in ("fit.png", "data.png"):
        if (out_dir / p_name).is_file():
            outputs["plots"].append(p_name)
            outputs["has_any"] = True

    # Parse summary.csv
    summary_path = out_dir / "summary.csv"
    if summary_path.is_file():
        try:
            summary_rows = []
            with open(summary_path) as f:
                reader = csv.reader(f)
                headers = next(reader)  # E.g. ["", "mean", "sd", "eti89_lb", "eti89_ub", ...]
                headers[0] = "parameter"
                for row in reader:
                    if row:
                        summary_rows.append(dict(zip(headers, row)))
            if summary_rows:
                outputs["summary"] = {
                    "headers": headers,
                    "rows": summary_rows
                }
                outputs["has_any"] = True
        except Exception:
            pass

    return outputs


def sync_jobs() -> None:
    from muscat_db.database import save_job, get_persisted_jobs
    with _FIT_LOCK:
        db_jobs = get_persisted_jobs()
        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "transit_fit"}
        
        for key, job in _FIT_JOBS.items():
            db_key = f"transit_fit:{job.inst}/{job.date}/{job.target.replace(' ', '')}"
            rc = job.proc.poll()
            if rc is None:
                state = "cancelling" if job.cancelled else "running"
            else:
                if job.cancelled:
                    state = "cancelled"
                else:
                    state = "done" if rc == 0 else "error"
                if job.state in ("running",):
                    job.state = state
                    job.returncode = rc
                    try:
                        job.logf.close()
                    except OSError:
                        pass
            
            elapsed = time.time() - job.started_at
            error_desc = ""
            if state == "error":
                from muscat_db.photometry import _get_error_desc
                error_desc = _get_error_desc(job.log_path)
            elif state == "cancelled":
                error_desc = "Cancelled by user"
            
            save_job(
                type_="transit_fit",
                inst=job.inst,
                date=job.date,
                target=job.target,
                state=state,
                returncode=rc,
                elapsed=round(elapsed),
                started_at=job.started_at,
                error_desc=error_desc
            )
            running_keys.discard(db_key)
            
        for db_key in running_keys:
            _, rest = db_key.split(":", 1)
            inst, date, target = rest.split("/", 2)
            started_at = time.time()
            elapsed = 0
            for j in db_jobs:
                if j["key"] == db_key:
                    started_at = j["started_at"]
                    elapsed = j["elapsed"]
                    break
            save_job(
                type_="transit_fit",
                inst=inst,
                date=date,
                target=target,
                state="error",
                returncode=-1,
                elapsed=elapsed,
                started_at=started_at,
                error_desc="Process lost (server restart)"
            )

