"""Helpers for the TTV Fit page: manage config generation (CSV, INI),
run the harmonic TTV pipeline, poll logs, and return outputs/plots.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import IO

from muscat_db import jobs, database
from muscat_db.job_store import get_job_store
from muscat_db import __meta__, __muscatdb_version__, __version__
from muscat_db.instruments import INSTRUMENTS
from muscat_db.photometry import (
    _conda_env_python,
    _RUNS_DIR_NAME,
    valid_date,
    _tail,
    _get_error_desc,
)
from muscat_db.cache import register_cache

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.resolve()
logger = logging.getLogger(__name__)

_HARMONIC_VERSION: str | None = None
_HARMONIC_VERSION_LOCK = threading.Lock()


def _harmonic_version() -> str:
    global _HARMONIC_VERSION
    if _HARMONIC_VERSION is not None:
        return _HARMONIC_VERSION
    with _HARMONIC_VERSION_LOCK:
        if _HARMONIC_VERSION is not None:
            return _HARMONIC_VERSION
        harmonic_py = _conda_env_python("harmonic")
        if harmonic_py is None:
            _HARMONIC_VERSION = "unknown"
        else:
            try:
                result = subprocess.run(
                    [harmonic_py, "-c", "from importlib.metadata import version; print(version('harmonic'))"],
                    capture_output=True, text=True, timeout=10,
                )
                _HARMONIC_VERSION = result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"
            except Exception:
                _HARMONIC_VERSION = "unknown"
    return _HARMONIC_VERSION


def _write_log_banner(logf: IO, cmd: list[str], options: dict | None = None) -> None:
    separator = "=" * 60
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logf.write(f"{separator}\n")
    logf.write(f"muscat-db v{__version__}  |  harmonic v{_harmonic_version()}  |  {now_utc}\n")
    logf.write("command: ttv-fit\n")
    logf.write(f"{separator}\n\n")
    logf.write(f"$ {shlex.join(cmd)}\n\n")
    if options is not None:
        logf.write("--- options ---\n")
        for k, v in sorted(options.items()):
            logf.write(f"  {k}: {v!r}\n")
        logf.write("\n")


def ttv_output_dir(inst: str, date: str, target: str, run_name: str = "") -> pathlib.Path:
    """Results directory for a TTV run: ``<base>/<target>/_runs/<run_name>``.

    Mirrors the photometry ``_runs/`` convention (:func:`photometry.run_output_dir`);
    ``inst``/``date`` are accepted for call-site symmetry but harmonic results are
    keyed on target alone.
    """
    base = pathlib.Path(os.environ.get("MUSCAT_TTV_DIR", "~/ql/harmonic")).expanduser().resolve(strict=False)
    parts = [base, _target_dir_name(target), _RUNS_DIR_NAME, slugify_run_name(run_name)]
    path = pathlib.Path(*[str(p) for p in parts]).resolve(strict=False)
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("invalid target") from exc
    return path


def get_ttv_command(inst: str, date: str, target: str, options: dict) -> str:
    run_name = (options.get("run_name") or "").strip()
    try:
        rdir = ttv_output_dir(inst, date, target, run_name)
    except ValueError:
        return ""
    cmd = [
        *_harmonic_prefix(),
        "-i", str(rdir / "data.csv"),
        "-c", str(rdir / "config.ini"),
        "-o", str(rdir),
    ]
    letters = options.get("planet_letters", "")
    if letters:
        cmd.extend(["-l", letters])
    walkers = options.get("walkers")
    if walkers:
        cmd.extend(["-w", str(walkers)])
    steps = options.get("steps")
    if steps:
        cmd.extend(["--steps", str(steps)])
    burn = options.get("burn")
    if burn:
        cmd.extend(["-b", str(burn)])
    thin = options.get("thin")
    if thin:
        cmd.extend(["--thin", str(thin)])
    nproc = options.get("nproc")
    if nproc:
        cmd.extend(["--nproc", str(nproc)])
    seed = options.get("seed")
    if seed:
        cmd.extend(["--seed", str(seed)])
    if options.get("non_transiting_outer"):
        cmd.append("-n")
    if options.get("phase_offsets"):
        cmd.append("--phase-offsets")
    mstar = options.get("mstar")
    if mstar:
        cmd.extend(["-m", str(mstar)])
    if options.get("clobber"):
        cmd.append("--clobber")
    return shlex.join(cmd)


_target_dir_name = jobs.target_dir_name
slugify_run_name = jobs.slugify_run_name


def log_path(inst: str, date: str, target: str) -> pathlib.Path | None:
    try:
        rdir = ttv_output_dir(inst, date, target)
    except ValueError:
        return None
    p = rdir / "harmonic.log"
    return p if p.is_file() else None


TTVFitJob = jobs.PipelineJob

_TTV_JOBS: dict[str, TTVFitJob] = {}
_TTV_LOCK = threading.Lock()
_MAX_FULL_JOBS = 1

_FINALIZE_GRACE_S = int(os.environ.get("MUSCAT_TTV_FINALIZE_GRACE_S", 8))
_FINALIZE_GRACE_TERMINAL_S = int(os.environ.get("MUSCAT_TTV_FINALIZE_GRACE_TERMINAL_S", 2))
_TERMINAL_LOG_MARKERS = ("TTV fitting completed successfully",)


def _finalize_config() -> jobs.FinalizeConfig:
    return jobs.FinalizeConfig(
        grace_s=_FINALIZE_GRACE_S,
        grace_terminal_s=_FINALIZE_GRACE_TERMINAL_S,
        terminal_markers=_TERMINAL_LOG_MARKERS,
        partial_failure_marker=None,
    )


def _count_running_full() -> int:
    return jobs.count_running_full(_TTV_JOBS)


def ttv_job_key(inst: str, date: str, target: str, run_name: str = "") -> str:
    return f"{inst}/{date}/{_target_dir_name(target)}/{slugify_run_name(run_name)}"


def _harmonic_prefix() -> list[str]:
    env = "harmonic"
    conda_py = _conda_env_python(env)
    if conda_py:
        harmonic_cli = pathlib.Path(conda_py).parent / "harmonic"
        if harmonic_cli.is_file():
            return [str(harmonic_cli)]
        return [conda_py, "-m", "harmonic.harmonic"]
    if shutil.which("harmonic"):
        return ["harmonic"]
    return ["harmonic"]


def _infer_planet_letters(planets: list[str]) -> str:
    """Sort planet letter designations and return as a contiguous string."""
    sorted_pl = sorted(set(planets))
    return "".join(sorted_pl)


def _make_harmonic_csv(transit_data: list[dict], planet_letters: str) -> str:
    """Convert planet-lettered transit data to harmonic's integer-indexed CSV.

    *transit_data* is a list of dicts with keys ``planet`` (letter), ``epoch``,
    ``tc``, ``tc_unc``. Returns the CSV content as a string.
    """
    letter_to_idx = {pl: i for i, pl in enumerate(planet_letters)}
    rows = ["planet,epoch,tc,tc_unc"]
    for row in transit_data:
        pl = row["planet"]
        idx = letter_to_idx.get(pl)
        if idx is None:
            logger.warning("planet %r not in letter set %r, skipping", pl, planet_letters)
            continue
        rows.append(f"{idx},{row['epoch']},{row['tc']},{row['tc_unc']}")
    return "\n".join(rows) + "\n"


def _make_harmonic_config(
    planet_letters: str,
    init_params: dict | None = None,
    outer_params: dict | None = None,
    t14_params: dict | None = None,
) -> str:
    """Build a harmonic INI config string.

    *init_params*: dict like ``{"a_bc": 0.02, "per_bc": 1000, "t_bc": 2455333}``
    """
    lines = ["[INIT]"]
    init = init_params or {}
    for key in sorted(init.keys()):
        lines.append(f"{key} = {init[key]}")
    lines.append("")
    if outer_params:
        lines.append("[OUTER]")
        for key in sorted(outer_params.keys()):
            lines.append(f"{key} = {outer_params[key]}")
        lines.append("")
    if t14_params:
        lines.append("[T14]")
        for key in sorted(t14_params.keys()):
            lines.append(f"{key} = {t14_params[key]}")
        lines.append("")
    return "\n".join(lines)


def write_ttv_inputs(
    rdir: pathlib.Path,
    csv_content: str,
    ini_content: str,
    options: dict,
) -> None:
    data_path = rdir / "data.csv"
    data_path.write_text(csv_content)
    config_path = rdir / "config.ini"
    config_path.write_text(ini_content)
    meta: dict = {
        "__muscatdb_version__": __muscatdb_version__,
        "_meta__": __meta__,
        "harmonic_version": _harmonic_version(),
        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "options": options,
    }
    import yaml
    with open(rdir / "meta.yaml", "w") as f:
        yaml.safe_dump(meta, f, default_flow_style=False, sort_keys=False)


def validate_ttv_options(options: dict | None) -> str | None:
    o = options or {}
    csv_lines_raw = (o.get("csv_content") or "").strip()
    if not csv_lines_raw:
        return "Transit time data is required"
    lines = csv_lines_raw.split("\n")
    header = lines[0] if lines else ""
    if header != "planet,epoch,tc,tc_unc":
        return "CSV must have header: planet,epoch,tc,tc_unc"

    try:
        import csv as _csv_module
        reader = _csv_module.DictReader(lines)
        for row in reader:
            pl = row.get("planet", "").strip()
            ep = row.get("epoch", "").strip()
            tc = row.get("tc", "").strip()
            unc = row.get("tc_unc", "").strip()
            if not pl or not ep or not tc or not unc:
                return "All rows must have planet, epoch, tc, tc_unc values"
            try:
                float(tc)
                float(unc)
            except ValueError:
                return "tc and tc_unc must be numeric"
    except Exception:
        return "Could not parse CSV data"

    planet_letters = o.get("planet_letters", "").strip()
    if not planet_letters:
        return "Planet letters are required"
    if not all(c.isalpha() and c.islower() for c in planet_letters):
        return "Planet letters must be lowercase letters"

    ini_lines_raw = (o.get("ini_content") or "").strip()
    if not ini_lines_raw:
        return "INI configuration is required"
    import configparser
    config = configparser.ConfigParser()
    try:
        config.read_string("[dummy]\n" + ini_lines_raw.replace("[INIT]", "[INIT]\n"))
        if "INIT" not in config.sections():
            return "INI must have [INIT] section"
    except Exception:
        return "Could not parse INI configuration"

    if len(planet_letters) >= 2:
        first_pair = planet_letters[:2]
        expected_amp = f"a_{first_pair}"
        has_any_amp = any(k.startswith("a_") for k in config["INIT"])
        if not has_any_amp:
            return f"INI [INIT] must contain at least one amplitude guess (e.g. {expected_amp})"

    return None


def start_ttv_fit(
    inst: str,
    date: str,
    target: str,
    options: dict,
    user_name: str | None = None,
) -> dict:
    if inst not in INSTRUMENTS:
        return {"ok": False, "error": f"unknown instrument {inst!r}"}
    if not valid_date(date):
        return {"ok": False, "error": "date must be 6-digit yymmdd"}
    if not (target or "").strip():
        return {"ok": False, "error": "target is required"}
    try:
        _target_dir_name(target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    err = validate_ttv_options(options)
    if err:
        return {"ok": False, "error": err}

    run_name = (options.get("run_name") or "").strip()
    run_seg = slugify_run_name(run_name)

    key = ttv_job_key(inst, date, target, run_name)

    with _TTV_LOCK:
        existing = _TTV_JOBS.get(key)
        if existing is not None and existing.proc.poll() is None:
            return {"ok": True, "key": key, "already_running": True}

        at_capacity = _count_running_full() >= _MAX_FULL_JOBS
        if at_capacity:
            try:
                get_job_store().enqueue(
                    type_="ttv_fit",
                    inst=inst, date=date, target=target, run_id=run_seg,
                    started_at=time.time(),
                    run_type="full",
                    params=json.dumps({"options": options}, separators=(",", ":")),
                    run_name=run_name,
                    user_name=user_name,
                )
            except Exception:
                return {"ok": False, "error": "database not writable"}
            return {"ok": True, "key": key, "queued": True}

    rdir = ttv_output_dir(inst, date, target, run_name)
    rdir.mkdir(parents=True, exist_ok=True)

    csv_content = options.get("csv_content", "")
    ini_content = options.get("ini_content", "")
    write_ttv_inputs(rdir, csv_content, ini_content, options)

    cmd = [
        *_harmonic_prefix(),
        "-i", str(rdir / "data.csv"),
        "-c", str(rdir / "config.ini"),
        "-o", str(rdir),
    ]
    letters = options.get("planet_letters", "")
    if letters:
        cmd.extend(["-l", letters])
    walkers = options.get("walkers")
    if walkers:
        cmd.extend(["-w", str(walkers)])
    steps = options.get("steps")
    if steps:
        cmd.extend(["--steps", str(steps)])
    burn = options.get("burn")
    if burn:
        cmd.extend(["-b", str(burn)])
    thin = options.get("thin")
    if thin:
        cmd.extend(["--thin", str(thin)])
    nproc = options.get("nproc")
    if nproc:
        cmd.extend(["--nproc", str(nproc)])
    seed = options.get("seed")
    if seed:
        cmd.extend(["--seed", str(seed)])
    if options.get("non_transiting_outer"):
        cmd.append("-n")
    if options.get("phase_offsets"):
        cmd.append("--phase-offsets")
    mstar = options.get("mstar")
    if mstar:
        cmd.extend(["-m", str(mstar)])
    if options.get("clobber"):
        cmd.append("--clobber")

    log_path = rdir / "harmonic.log"
    logf = open(log_path, "w")
    _write_log_banner(logf, cmd, options)
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
        try:
            with open(rdir / "harmonic.pid", "w") as pidf:
                pidf.write(str(proc.pid))
        except Exception:
            logger.debug("failed to write harmonic.pid in %s", rdir, exc_info=True)
    except (FileNotFoundError, OSError) as exc:
        logf.write(f"\nfailed to launch harmonic: {exc}\n")
        logf.close()
        return {"ok": False, "error": f"failed to launch harmonic: {exc}"}

    with _TTV_LOCK:
        _TTV_JOBS[key] = TTVFitJob(
            key=key, inst=inst, date=date, target=target,
            cmd=cmd, proc=proc, logf=logf, log_path=log_path,
            run_type="full", run_id=run_seg, run_name=run_name,
        )
        get_job_store().save(
            type_="ttv_fit",
            inst=inst,
            date=date,
            target=target,
            run_id=run_seg,
            state="running",
            returncode=None,
            elapsed=0,
            started_at=_TTV_JOBS[key].started_at,
            run_type="full",
            params=json.dumps({"options": options}, separators=(",", ":")),
            run_name=run_name,
            user_name=user_name,
        )

    return {"ok": True, "key": key}


def _pending_status(inst: str, date: str, target: str, run_name: str = "") -> dict | None:
    try:
        db_key = f"ttv_fit:{ttv_job_key(inst, date, target, run_name)}"
    except ValueError:
        return None
    try:
        for entry in get_job_store().all():
            if (
                entry["key"] == db_key
                and entry["type"] == "ttv_fit"
                and entry["state"] == "pending"
            ):
                started = entry.get("started_at") or time.time()
                return {
                    "state": "pending",
                    "returncode": None,
                    "log": "",
                    "elapsed": round(time.time() - started),
                }
    except Exception:
        logger.debug("failed to read pending ttv-fit status for %s/%s/%s", inst, date, target, exc_info=True)
    return None


def _running_status(inst: str, date: str, target: str, run_name: str = "") -> dict | None:
    try:
        db_key = f"ttv_fit:{ttv_job_key(inst, date, target, run_name)}"
    except ValueError:
        return None
    try:
        for entry in get_job_store().all():
            if (
                entry["key"] == db_key
                and entry["type"] == "ttv_fit"
                and entry["state"] == "running"
            ):
                try:
                    rdir = ttv_output_dir(inst, date, target, run_name)
                    lp = rdir / "harmonic.log"
                except ValueError:
                    lp = None
                started = entry.get("started_at") or time.time()
                return {
                    "state": "running",
                    "returncode": None,
                    "log": _tail(lp) if (lp and lp.is_file()) else "",
                    "elapsed": round(time.time() - started),
                }
    except Exception:
        logger.debug("failed to read running ttv-fit status for %s/%s/%s", inst, date, target, exc_info=True)
    return None


def _persisted_status(inst: str, date: str, target: str, run_name: str = "") -> dict | None:
    try:
        db_key = f"ttv_fit:{ttv_job_key(inst, date, target, run_name)}"
    except ValueError:
        return None
    try:
        for entry in get_job_store().all():
            if entry["key"] != db_key or entry["type"] != "ttv_fit":
                continue
            state = entry["state"]
            if state not in ("done", "error", "cancelled"):
                return None
            try:
                rdir = ttv_output_dir(inst, date, target, run_name)
                lp = rdir / "harmonic.log"
            except ValueError:
                lp = None
            return {
                "state": state,
                "returncode": entry.get("returncode"),
                "log": _tail(lp) if (lp and lp.is_file()) else "",
                "elapsed": round(entry.get("elapsed") or 0),
            }
    except Exception:
        logger.debug("failed to read persisted ttv-fit status for %s/%s/%s", inst, date, target, exc_info=True)
    return None


def job_status(inst: str, date: str, target: str, run_name: str = "") -> dict:
    try:
        key = ttv_job_key(inst, date, target, run_name)
    except ValueError as exc:
        return {"state": "none", "log": "", "returncode": None, "elapsed": 0, "error": str(exc)}
    with _TTV_LOCK:
        job = _TTV_JOBS.get(key)
        if job is None:
            pending = _pending_status(inst, date, target, run_name)
            if pending is not None:
                return pending
            running = _running_status(inst, date, target, run_name)
            if running is not None:
                return running
            persisted = _persisted_status(inst, date, target, run_name)
            if persisted is not None:
                return persisted
            try:
                rdir = ttv_output_dir(inst, date, target, run_name)
                log_path = rdir / "harmonic.log"
                if log_path.is_file():
                    return {"state": "done", "log": _tail(log_path), "returncode": 0, "elapsed": 0}
            except ValueError:
                pass
            return {"state": "none", "log": "", "returncode": None, "elapsed": 0}

        state, rc, is_terminal = jobs.resolve_job_state(job, _finalize_config())
        if is_terminal and job.state == "running":
            job.state = state
            job.returncode = rc
            job.elapsed = round(time.time() - job.started_at)
            try:
                job.logf.close()
            except OSError:
                pass
        log_path = job.log_path
        elapsed = job.elapsed if job.state not in ("running", "cancelling") and job.elapsed is not None else round(time.time() - job.started_at)

    return {
        "state": state,
        "returncode": rc,
        "log": _tail(log_path),
        "elapsed": round(elapsed),
        "run_type": job.run_type if job else "full",
    }


_kill_after = jobs.kill_after


def cancel_ttv_fit(inst: str, date: str, target: str, run_name: str = "") -> dict:
    try:
        key = ttv_job_key(inst, date, target, run_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    store = get_job_store()
    run_seg = slugify_run_name(run_name)
    with _TTV_LOCK:
        job = _TTV_JOBS.get(key)
        if job is None:
            db_key = f"ttv_fit:{key}"
            found = [j for j in store.all() if j["key"] == db_key]
            if found and found[0]["state"] == "pending":
                store.save(
                    type_="ttv_fit", inst=inst, date=date, target=target,
                    state="cancelled", returncode=-1, elapsed=0,
                    started_at=found[0]["started_at"],
                    error_desc="Cancelled by user",
                    run_id=run_seg, run_name=run_name,
                )
                return {"ok": True, "key": key}
            return {"ok": False, "error": "no job to cancel"}
        if job.proc.poll() is not None:
            return {"ok": True, "already_finished": True}
        job.cancelled = True
        proc = job.proc
        store.save(
            type_="ttv_fit",
            inst=inst, date=date, target=target,
            state="cancelled", returncode=-1,
            elapsed=round(time.time() - job.started_at),
            started_at=job.started_at,
            error_desc="Cancelled by user",
            run_id=run_seg, run_name=run_name,
        )

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass

    threading.Thread(target=_kill_after, args=(proc,), daemon=True).start()
    return {"ok": True, "key": key}


def delete_ttv_fit(inst: str, date: str, target: str, run_name: str = "") -> dict:
    try:
        rdir = ttv_output_dir(inst, date, target, run_name)
    except ValueError:
        return {"ok": False, "error": "invalid target"}
    removed = 0
    if rdir.is_dir():
        try:
            shutil.rmtree(rdir)
            removed += 1
        except OSError:
            pass
    job_key = ttv_job_key(inst, date, target, run_name)
    db_key = f"ttv_fit:{job_key}"
    get_job_store().delete(db_key)
    with _TTV_LOCK:
        _TTV_JOBS.pop(job_key, None)
    _ttv_outputs_cache.clear()
    return {"ok": True, "count": removed}


_ttv_outputs_cache = register_cache(ttl=300.0)


def has_ttv_outputs(inst: str, date: str, target: str, run_name: str = "") -> bool:
    try:
        rdir = ttv_output_dir(inst, date, target, run_name)
    except ValueError:
        return False
    return rdir.is_dir() and (rdir / "samples.csv.gz").is_file()


def list_ttv_runs(inst: str, date: str, target: str) -> list[dict]:
    """Run-name slugs under ``<target>/_runs`` that hold at least one result file.

    Newest-first by directory mtime, so the freshest run is the one the
    ephemeris page selects by default.
    """
    try:
        runs_dir = ttv_output_dir(inst, date, target, "").parent
    except ValueError:
        return []
    if not runs_dir.is_dir():
        return []

    runs: list[dict] = []
    for d in sorted(runs_dir.iterdir()):
        # Directory names are already slugs, so re-slugging them is a no-op.
        if not d.is_dir() or not get_ttv_outputs(inst, date, target, d.name)["has_any"]:
            continue
        try:
            mtime = d.stat().st_mtime
        except OSError:
            mtime = 0.0
        runs.append({"run_name": d.name, "mtime": mtime})

    runs.sort(key=lambda r: (-r["mtime"], r["run_name"]))
    return runs


def get_ttv_outputs(inst: str, date: str, target: str, run_name: str = "") -> dict:
    try:
        rdir = ttv_output_dir(inst, date, target, run_name)
    except ValueError:
        return _get_ttv_outputs_mtime(inst, date, target, run_name, -1.0)
    mtime = 0.0
    try:
        mtime = rdir.stat().st_mtime
    except OSError:
        pass
    return _get_ttv_outputs_mtime(inst, date, target, run_name, mtime)


@_ttv_outputs_cache
def _get_ttv_outputs_mtime(
    inst: str, date: str, target: str, run_name: str, _cache_mtime: float
) -> dict:
    outputs: dict = {
        "has_any": False,
        "plots": [],
        "has_log": False,
        "has_data_csv": False,
        "has_config_ini": False,
        "has_samples": False,
        "extra_files": [],
    }
    try:
        rdir = ttv_output_dir(inst, date, target, run_name)
    except ValueError:
        return outputs

    if not rdir.is_dir():
        return outputs

    if (rdir / "harmonic.log").is_file():
        outputs["has_log"] = True
    if (rdir / "data.csv").is_file():
        outputs["has_data_csv"] = True
    if (rdir / "config.ini").is_file():
        outputs["has_config_ini"] = True
    if (rdir / "samples.csv.gz").is_file():
        outputs["has_samples"] = True

    for p in sorted(rdir.glob("*.png")):
        if p.is_file():
            try:
                st = p.stat()
                mtime = st.st_mtime
                version = str(st.st_mtime_ns)
                created_at = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
            except Exception:
                version = "0"
                created_at = "Unknown"
            outputs["plots"].append({
                "file": p.name,
                "created_at": created_at,
                "version": version,
            })
            outputs["has_any"] = True

    _linked = {"samples.csv.gz"}
    for p in sorted(rdir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".png" or p.name in _linked:
            continue
        if p.name in ("harmonic.log", "data.csv", "config.ini", "meta.yaml", "args.txt", "fit_config.json"):
            outputs["extra_files"].append(p.name)
            outputs["has_any"] = True

    return outputs


def sync_jobs() -> None:
    store = get_job_store()
    with _TTV_LOCK:
        db_jobs = store.all()
        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "ttv_fit"}
        db_by_key = {j["key"]: j for j in db_jobs}

        for key, job in _TTV_JOBS.items():
            db_key = f"ttv_fit:{ttv_job_key(job.inst, job.date, job.target, job.run_name)}"
            state, rc, is_terminal = jobs.resolve_job_state(job, _finalize_config())
            if is_terminal and job.state == "running":
                job.state = state
                job.returncode = rc
                job.elapsed = round(time.time() - job.started_at)
                try:
                    job.logf.close()
                except OSError:
                    pass

            persist_state = "running" if state == "finalizing" else state
            persist_rc = None if state == "finalizing" else rc
            existing = db_by_key.get(db_key)
            if job.state not in ("running", "cancelling") and job.elapsed is not None:
                elapsed = job.elapsed
            elif existing is not None and existing.get("state") not in ("running", "cancelling"):
                elapsed = existing.get("elapsed") or 0
            else:
                elapsed = round(time.time() - job.started_at)
            unchanged = (
                existing is not None
                and existing.get("state") == persist_state
                and existing.get("returncode") == persist_rc
            )
            running_keys.discard(db_key)
            if unchanged:
                continue

            error_desc = ""
            if persist_state == "error":
                error_desc = _get_error_desc(job.log_path)
            elif persist_state == "cancelled":
                error_desc = "Cancelled by user"

            store.save(
                type_="ttv_fit",
                inst=job.inst,
                date=job.date,
                target=job.target,
                state=persist_state,
                returncode=persist_rc,
                elapsed=round(elapsed),
                started_at=job.started_at,
                error_desc=error_desc,
                run_id=job.run_id,
                run_name=job.run_name,
            )

            if persist_state in ("done", "error", "cancelled"):
                database.refresh_target_status(job.target)

        for db_key in running_keys:
            row = next((j for j in db_jobs if j["key"] == db_key), None)
            if row is None:
                continue
            inst = row["inst"]
            date = row["date"]
            target = row["target"]
            run_name = row.get("run_name") or ""
            completed_ok = False
            rdir = None
            try:
                rdir = ttv_output_dir(inst, date, target, run_name)
                if rdir.is_dir() and (rdir / "samples.csv.gz").is_file():
                    log_path = rdir / "harmonic.log"
                    if log_path.is_file():
                        with open(log_path, errors="replace") as lf:
                            log_content = lf.read()
                            if "TTV fitting completed successfully" in log_content:
                                completed_ok = True
            except Exception:
                logger.debug("failed to inspect orphan ttv fit completion for %s", rdir, exc_info=True)

            if completed_ok:
                store.save(
                    type_="ttv_fit",
                    inst=inst, date=date, target=target,
                    state="done", returncode=0,
                    elapsed=row["elapsed"],
                    started_at=row["started_at"],
                    error_desc="",
                    run_id=row.get("run_id"),
                    run_name=run_name,
                )
                database.refresh_target_status(target)
            else:
                store.save(
                    type_="ttv_fit",
                    inst=inst, date=date, target=target,
                    state="error", returncode=-1,
                    elapsed=row["elapsed"],
                    started_at=row["started_at"],
                    error_desc="Process lost (server restart)",
                    run_id=row.get("run_id"),
                    run_name=run_name,
                )
                database.refresh_target_status(target)

        if _count_running_full() < _MAX_FULL_JOBS:
            pending = store.pending("ttv_fit")
            for entry in pending:
                if _count_running_full() >= _MAX_FULL_JOBS:
                    break
                try:
                    p = json.loads(entry.get("params") or "{}")
                except (json.JSONDecodeError, TypeError):
                    p = {}
                opts = p.get("options", {})
                run_name = entry.get("run_name") or opts.get("run_name") or ""
                run_seg = slugify_run_name(run_name)
                try:
                    mem_key = ttv_job_key(entry["inst"], entry["date"], entry["target"], run_name)
                except ValueError:
                    continue
                if mem_key in _TTV_JOBS:
                    store.save(type_="ttv_fit", inst=entry["inst"], date=entry["date"], target=entry["target"], state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Duplicate entry", run_id=run_seg, run_name=run_name)
                    continue
                inst, date, target = entry["inst"], entry["date"], entry["target"]
                try:
                    key = ttv_job_key(inst, date, target, run_name)
                    rdir = ttv_output_dir(inst, date, target, run_name)
                except ValueError:
                    store.save(type_="ttv_fit", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Invalid target", run_id=run_seg, run_name=run_name)
                    continue
                rdir.mkdir(parents=True, exist_ok=True)
                csv_content = opts.get("csv_content", "")
                ini_content = opts.get("ini_content", "")
                write_ttv_inputs(rdir, csv_content, ini_content, opts)
                cmd = [
                    *_harmonic_prefix(),
                    "-i", str(rdir / "data.csv"),
                    "-c", str(rdir / "config.ini"),
                    "-o", str(rdir),
                ]
                letters = opts.get("planet_letters", "")
                if letters:
                    cmd.extend(["-l", letters])
                walkers = opts.get("walkers")
                if walkers:
                    cmd.extend(["-w", str(walkers)])
                steps = opts.get("steps")
                if steps:
                    cmd.extend(["--steps", str(steps)])
                burn = opts.get("burn")
                if burn:
                    cmd.extend(["-b", str(burn)])
                thin = opts.get("thin")
                if thin:
                    cmd.extend(["--thin", str(thin)])
                nproc = opts.get("nproc")
                if nproc:
                    cmd.extend(["--nproc", str(nproc)])
                seed = opts.get("seed")
                if seed:
                    cmd.extend(["--seed", str(seed)])
                if opts.get("non_transiting_outer"):
                    cmd.append("-n")
                if opts.get("phase_offsets"):
                    cmd.append("--phase-offsets")
                mstar = opts.get("mstar")
                if mstar:
                    cmd.extend(["-m", str(mstar)])
                if opts.get("clobber"):
                    cmd.append("--clobber")
                log_path = rdir / "harmonic.log"
                try:
                    logf = open(log_path, "w")
                    _write_log_banner(logf, cmd, opts)
                    logf.flush()
                    proc = subprocess.Popen(cmd, cwd=str(rdir), stdout=logf, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                    try:
                        with open(rdir / "harmonic.pid", "w") as pidf:
                            pidf.write(str(proc.pid))
                    except Exception:
                        logger.debug("failed to write harmonic.pid for queued run %s", rdir, exc_info=True)
                except (FileNotFoundError, OSError) as exc:
                    try:
                        logf.close()
                    except OSError:
                        pass
                    store.save(type_="ttv_fit", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Failed to launch: {exc}", run_id=run_seg, run_name=run_name)
                    continue
                _TTV_JOBS[key] = TTVFitJob(key=key, inst=inst, date=date, target=target, cmd=cmd, proc=proc, logf=logf, log_path=log_path, run_type="full", run_id=run_seg, run_name=run_name)
                try:
                    store.save(type_="ttv_fit", inst=inst, date=date, target=target, state="running", returncode=None, elapsed=0, started_at=_TTV_JOBS[key].started_at, run_type="full", params=entry.get("params", ""), run_id=run_seg, run_name=run_name)
                except Exception:
                    logger.debug("failed to persist queued ttv-fit launch for %s", rdir, exc_info=True)
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    try:
                        logf.close()
                    except OSError:
                        pass
                    _TTV_JOBS.pop(key, None)
                    store.save(type_="ttv_fit", inst=inst, date=date, target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Database error", run_id=run_seg, run_name=run_name)
