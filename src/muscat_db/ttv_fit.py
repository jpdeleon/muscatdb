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

import yaml

from muscat_db import jobs, database
from muscat_db.job_store import get_job_store
from muscat_db import __meta__, __muscatdb_version__, __version__
from muscat_db.photometry import (
    _conda_env_python,
    _RUNS_DIR_NAME,
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


def _format_csv_table(csv_text: str) -> str:
    """Format CSV transit-time data as a human-readable table."""
    lines = [l for l in csv_text.strip().split("\n") if l.strip()]
    if not lines:
        return csv_text
    rows = [line.split(",") for line in lines]
    col_widths = [max(len(cells[i]) for cells in rows) for i in range(len(rows[0]))]
    sep = "  "
    header = sep.join(f"{c:>{col_widths[i]}}" for i, c in enumerate(rows[0]))
    ruler = sep.join("-" * col_widths[i] for i in range(len(rows[0])))
    data = []
    for row in rows[1:]:
        data.append(sep.join(f"{c:>{col_widths[i]}}" for i, c in enumerate(row)))
    return f"{header}\n{ruler}\n" + "\n".join(data)


class _MetaDumper(yaml.SafeDumper):
    """Dump meta.yaml with multi-line strings (csv_content, ini_content, ...)
    as block literals instead of PyYAML's default folded style, which inserts
    a blank line after every embedded newline and is unreadable for CSV data."""


def _represent_multiline_str(dumper: yaml.Dumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_MetaDumper.add_representer(str, _represent_multiline_str)


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
            if k == "csv_content" and isinstance(v, str) and v.strip():
                logf.write(f"  csv_content:\n{_format_csv_table(v)}\n\n")
            elif k == "ini_content" and isinstance(v, str) and v.strip():
                logf.write(f"  ini_content:\n{v.rstrip()}\n\n")
            elif k == "input_snapshot":
                # The full ephemeris-page UI state (dataset checkboxes, manual
                # points, plot settings, ...) that produced this run's
                # csv_content -- kept in meta.yaml for the ephemeris page to
                # restore, not worth dumping into the human-readable log.
                n_manual = len((v or {}).get("manual_points") or [])
                logf.write(f"  input_snapshot: <ui state, {n_manual} manual point(s); see meta.yaml>\n")
            else:
                logf.write(f"  {k}: {v!r}\n")
        logf.write("\n")


def ttv_output_dir(target: str, run_name: str = "") -> pathlib.Path:
    """Results directory for a TTV run: ``<base>/<target>/_runs/<run_name>``."""
    base = pathlib.Path(
        os.environ.get("MUSCAT_TTV_DIR", str(pathlib.Path.home() / "ql" / "harmonic"))
    ).expanduser().resolve(strict=False)
    parts = [base, _target_dir_name(target), _RUNS_DIR_NAME, slugify_run_name(run_name)]
    path = pathlib.Path(*[str(p) for p in parts]).resolve(strict=False)
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("invalid target") from exc
    return path


def safe_output_file(target: str, run_name: str, filename: str) -> pathlib.Path | None:
    """Return a direct file child of a TTV run, rejecting path traversal."""
    if not filename or pathlib.PurePath(filename).name != filename:
        return None
    try:
        output_dir = ttv_output_dir(target, run_name).resolve(strict=False)
        candidate = (output_dir / filename).resolve(strict=False)
        candidate.relative_to(output_dir)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def get_ttv_command(target: str, options: dict) -> str:
    run_name = (options.get("run_name") or "").strip()
    try:
        rdir = ttv_output_dir(target, run_name)
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
    mu_min_me = options.get("mu_min_me")
    if mu_min_me is not None:
        cmd.extend(["--mu-min-me", str(mu_min_me)])
    mu_max_me = options.get("mu_max_me")
    if mu_max_me is not None:
        cmd.extend(["--mu-max-me", str(mu_max_me)])
    z_max = options.get("z_max")
    if z_max is not None:
        cmd.extend(["--z-max", str(z_max)])
    if options.get("clobber"):
        cmd.append("--clobber")
    return shlex.join(cmd)


_target_dir_name = jobs.target_dir_name
slugify_run_name = jobs.slugify_run_name


def log_path(target: str, run_id: str = "") -> pathlib.Path | None:
    """Path to the ``harmonic.log`` for a given TTV fit run, or ``None`` if absent.

    ``run_id`` is an already-slugified run segment (as persisted on jobs as
    ``run_id``); an empty value means the default run. Both ``target`` and
    ``run_id`` are validated as single path segments so a crafted value cannot
    escape ``MUSCAT_TTV_DIR``.
    """
    try:
        base = pathlib.Path(
            os.environ.get("MUSCAT_TTV_DIR", str(pathlib.Path.home() / "ql" / "harmonic"))
        ).expanduser().resolve(strict=False)
        target_seg = _target_dir_name(target)
        # run_id is already a slug; empty run_id means the default run.
        run_seg = jobs.run_dir_name(run_id) if run_id else "default"
    except ValueError:
        return None
    p = base / target_seg / _RUNS_DIR_NAME / run_seg / "harmonic.log"
    if p.is_file():
        return p
    # Fallback for legacy runs written directly under <base>/<target>/.
    legacy = base / target_seg / "harmonic.log"
    return legacy if legacy.is_file() else None


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


def ttv_job_key(target: str, run_name: str = "") -> str:
    return f"{_target_dir_name(target)}/{slugify_run_name(run_name)}"


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
    with open(rdir / "meta.yaml", "w") as f:
        yaml.dump(meta, f, Dumper=_MetaDumper, default_flow_style=False, sort_keys=False)


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
    target: str,
    options: dict,
    user_name: str | None = None,
) -> dict:
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

    key = ttv_job_key(target, run_name)

    with _TTV_LOCK:
        existing = _TTV_JOBS.get(key)
        if existing is not None and existing.proc.poll() is None:
            return {"ok": True, "key": key, "already_running": True}

        # Every ttv_fit job is "full" (no test-run concept here), so always
        # claim a cross-process concurrency slot before launching.
        claimed_slot = get_job_store().claim_slot("ttv_fit", key, _MAX_FULL_JOBS)
        at_capacity = not claimed_slot
        if at_capacity:
            try:
                get_job_store().enqueue(
                    type_="ttv_fit",
                    inst="_", date="_", target=target, run_id=run_seg,
                    started_at=time.time(),
                    run_type="full",
                    params=json.dumps({"options": options}, separators=(",", ":")),
                    run_name=run_name,
                    user_name=user_name,
                )
            except Exception:
                return {"ok": False, "error": "database not writable"}
            return {"ok": True, "key": key, "queued": True}

    rdir = ttv_output_dir(target, run_name)
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
    mu_min_me = options.get("mu_min_me")
    if mu_min_me is not None:
        cmd.extend(["--mu-min-me", str(mu_min_me)])
    mu_max_me = options.get("mu_max_me")
    if mu_max_me is not None:
        cmd.extend(["--mu-max-me", str(mu_max_me)])
    z_max = options.get("z_max")
    if z_max is not None:
        cmd.extend(["--z-max", str(z_max)])
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
        get_job_store().release_slot("ttv_fit", key)
        logf.write(f"\nfailed to launch harmonic: {exc}\n")
        logf.close()
        return {"ok": False, "error": f"failed to launch harmonic: {exc}"}

    with _TTV_LOCK:
        _TTV_JOBS[key] = TTVFitJob(
            key=key, inst="_", date="_", target=target,
            cmd=cmd, proc=proc, logf=logf, log_path=log_path,
            run_type="full", run_id=run_seg, run_name=run_name,
        )
        get_job_store().save(
            type_="ttv_fit",
            inst="_",
            date="_",
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


def _pending_status(target: str, run_name: str = "") -> dict | None:
    run_seg = slugify_run_name(run_name)
    try:
        for entry in get_job_store().all():
            if (
                entry.get("type") == "ttv_fit"
                and entry.get("target") == target
                and (entry.get("run_name") == run_name or entry.get("run_id") == run_seg)
                and entry.get("state") == "pending"
            ):
                started = entry.get("started_at") or time.time()
                return {
                    "state": "pending",
                    "returncode": None,
                    "log": "",
                    "elapsed": round(time.time() - started),
                }
    except Exception:
        logger.debug("failed to read pending ttv-fit status for %s", target, exc_info=True)
    return None


def _running_status(target: str, run_name: str = "") -> dict | None:
    run_seg = slugify_run_name(run_name)
    try:
        for entry in get_job_store().all():
            if (
                entry.get("type") == "ttv_fit"
                and entry.get("target") == target
                and (entry.get("run_name") == run_name or entry.get("run_id") == run_seg)
                and entry.get("state") == "running"
            ):
                try:
                    rdir = ttv_output_dir(target, run_name)
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
        logger.debug("failed to read running ttv-fit status for %s", target, exc_info=True)
    return None


def _persisted_status(target: str, run_name: str = "") -> dict | None:
    run_seg = slugify_run_name(run_name)
    try:
        for entry in get_job_store().all():
            if (
                entry.get("type") != "ttv_fit"
                or entry.get("target") != target
                or (entry.get("run_name") != run_name and entry.get("run_id") != run_seg)
            ):
                continue
            state = entry["state"]
            if state not in ("done", "error", "cancelled"):
                return None
            try:
                rdir = ttv_output_dir(target, run_name)
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
        logger.debug("failed to read persisted ttv-fit status for %s", target, exc_info=True)
    return None


def job_status(target: str, run_name: str = "") -> dict:
    try:
        key = ttv_job_key(target, run_name)
    except ValueError as exc:
        return {"state": "none", "log": "", "returncode": None, "elapsed": 0, "error": str(exc)}
    with _TTV_LOCK:
        job = _TTV_JOBS.get(key)
        if job is None:
            pending = _pending_status(target, run_name)
            if pending is not None:
                return pending
            running = _running_status(target, run_name)
            if running is not None:
                return running
            persisted = _persisted_status(target, run_name)
            if persisted is not None:
                return persisted
            try:
                rdir = ttv_output_dir(target, run_name)
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


def cancel_ttv_fit(target: str, run_name: str = "") -> dict:
    try:
        key = ttv_job_key(target, run_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    store = get_job_store()
    run_seg = slugify_run_name(run_name)
    with _TTV_LOCK:
        job = _TTV_JOBS.get(key)
        if job is None:
            found = [
                j for j in store.all()
                if j.get("type") == "ttv_fit"
                and j.get("target") == target
                and (j.get("run_name") == run_name or j.get("run_id") == run_seg)
            ]
            if found and found[0]["state"] in ("running", "pending"):
                store.save(
                    type_="ttv_fit",
                    inst=found[0].get("inst") or "_",
                    date=found[0].get("date") or "_",
                    target=target,
                    state="cancelled", returncode=-1, elapsed=found[0].get("elapsed") or 0,
                    started_at=found[0]["started_at"],
                    error_desc="Cancelled by user",
                    run_id=run_seg, run_name=run_name,
                )
                return {"ok": True, "key": found[0]["key"]}
            return {"ok": False, "error": "no job to cancel"}
        if job.proc.poll() is not None:
            return {"ok": True, "already_finished": True}
        job.cancelled = True
        proc = job.proc
        store.save(
            type_="ttv_fit",
            inst=job.inst or "_", date=job.date or "_", target=target,
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


def delete_ttv_fit(target: str, run_name: str = "") -> dict:
    try:
        rdir = ttv_output_dir(target, run_name)
    except ValueError:
        return {"ok": False, "error": "invalid target"}
    removed = 0
    if rdir.is_dir():
        try:
            shutil.rmtree(rdir)
            removed += 1
        except OSError:
            pass
    job_key = ttv_job_key(target, run_name)
    db_key = f"ttv_fit:{job_key}"
    get_job_store().delete(db_key)
    with _TTV_LOCK:
        _TTV_JOBS.pop(job_key, None)
    _ttv_outputs_cache.clear()
    _ttv_model_cache.clear()
    return {"ok": True, "count": removed}


_ttv_outputs_cache = register_cache(ttl=300.0)
_ttv_model_cache = register_cache(ttl=300.0)


def has_ttv_outputs(target: str, run_name: str = "") -> bool:
    try:
        rdir = ttv_output_dir(target, run_name)
    except ValueError:
        return False
    return rdir.is_dir() and (rdir / "samples.csv.gz").is_file()


def list_ttv_runs(target: str) -> list[dict]:
    """Run-name slugs under ``<target>/_runs`` that hold at least one result file.

    Newest-first by directory mtime, so the freshest run is the one the
    ephemeris page selects by default.
    """
    try:
        runs_dir = ttv_output_dir(target, "").parent
    except ValueError:
        return []
    if not runs_dir.is_dir():
        return []

    runs: list[dict] = []
    for d in sorted(runs_dir.iterdir()):
        # Directory names are already slugs, so re-slugging them is a no-op.
        if not d.is_dir() or not get_ttv_outputs(target, d.name)["has_any"]:
            continue
        try:
            mtime = d.stat().st_mtime
        except OSError:
            mtime = 0.0
        runs.append({"run_name": d.name, "mtime": mtime})

    runs.sort(key=lambda r: (-r["mtime"], r["run_name"]))
    return runs


def get_ttv_outputs(target: str, run_name: str = "") -> dict:
    try:
        rdir = ttv_output_dir(target, run_name)
    except ValueError:
        return _get_ttv_outputs_mtime(target, run_name, -1.0)
    mtime = 0.0
    try:
        mtime = rdir.stat().st_mtime
    except OSError:
        pass
    return _get_ttv_outputs_mtime(target, run_name, mtime)


@_ttv_outputs_cache
def _get_ttv_outputs_mtime(
    target: str, run_name: str, _cache_mtime: float
) -> dict:
    outputs: dict = {
        "has_any": False,
        "plots": [],
        "has_log": False,
        "has_data_csv": False,
        "has_config_ini": False,
        "has_samples": False,
        "has_model": False,
        "extra_files": [],
        "input_snapshot": None,
    }
    try:
        rdir = ttv_output_dir(target, run_name)
    except ValueError:
        return outputs

    if not rdir.is_dir():
        return outputs

    meta_path = rdir / "meta.yaml"
    if meta_path.is_file():
        try:
            meta = yaml.safe_load(meta_path.read_text()) or {}
            outputs["input_snapshot"] = (meta.get("options") or {}).get("input_snapshot")
        except Exception:
            logger.warning("could not parse meta.yaml for %s/%s", target, run_name, exc_info=True)

    if (rdir / "harmonic.log").is_file():
        outputs["has_log"] = True
    if (rdir / "data.csv").is_file():
        outputs["has_data_csv"] = True
    if (rdir / "config.ini").is_file():
        outputs["has_config_ini"] = True
    if (rdir / "samples.csv.gz").is_file():
        outputs["has_samples"] = True
    outputs["has_model"] = all(
        (rdir / name).is_file()
        for name in ("samples.csv.gz", "data.csv", "config.ini", "fit_config.json")
    )

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


def get_ttv_model(
    target: str, run_name: str = "", end_date: str = ""
) -> dict:
    """Return the minimum-chi-square posterior model for a saved harmonic run.

    ``end_date`` is an optional ISO UTC calendar date.  It extends prediction
    through the end of that day; the helper always retains the full fitted
    data range, so an earlier date never truncates existing observations.
    """
    try:
        rdir = ttv_output_dir(target, run_name)
    except ValueError:
        return {"ok": False, "error": "invalid target"}

    end_bjd: float | None = None
    normalized_date = ""
    if end_date:
        try:
            parsed_date = datetime.date.fromisoformat(end_date)
        except ValueError:
            return {"ok": False, "error": "end_date must be YYYY-MM-DD"}
        normalized_date = parsed_date.isoformat()
        end_utc = datetime.datetime.combine(
            parsed_date, datetime.time.max, tzinfo=datetime.timezone.utc
        )
        end_bjd = end_utc.timestamp() / 86400.0 + 2440587.5

    required = ("samples.csv.gz", "data.csv", "config.ini", "fit_config.json")
    if not rdir.is_dir() or not all((rdir / name).is_file() for name in required):
        return {"ok": False, "error": "saved run has no complete TTV model output"}
    try:
        version = max((rdir / name).stat().st_mtime_ns for name in required)
    except OSError:
        return {"ok": False, "error": "could not inspect saved TTV model output"}
    return _get_ttv_model_cached(target, run_name, normalized_date, end_bjd, version)


@_ttv_model_cache
def _get_ttv_model_cached(
    target: str,
    run_name: str,
    end_date: str,
    end_bjd: float | None,
    _version: int,
) -> dict:
    rdir = ttv_output_dir(target, run_name)
    harmonic_python = _conda_env_python("harmonic")
    if not harmonic_python:
        return {"ok": False, "error": "harmonic conda environment is unavailable"}
    helper = pathlib.Path(__file__).with_name("_ttv_model_helper.py")
    command = [harmonic_python, str(helper), str(rdir)]
    if end_bjd is not None:
        command.extend(["--end-bjd", repr(end_bjd)])
    env = os.environ.copy()
    matplotlib_config = pathlib.Path.home() / "temp" / "matplotlib"
    try:
        matplotlib_config.mkdir(parents=True, exist_ok=True)
        env.setdefault("MPLCONFIGDIR", str(matplotlib_config))
    except OSError:
        pass
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("TTV model evaluation failed for %s/%s: %s", target, run_name, exc)
        return {"ok": False, "error": "TTV model evaluation failed"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        logger.warning(
            "TTV model evaluation failed for %s/%s: %s",
            target,
            run_name,
            detail[-1] if detail else f"exit {completed.returncode}",
        )
        return {"ok": False, "error": "TTV model evaluation failed"}
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error": "TTV model returned invalid output"}
    result.update({"ok": True, "run_name": slugify_run_name(run_name), "end_date": end_date})
    return result


def sync_jobs() -> None:
    store = get_job_store()
    with _TTV_LOCK:
        db_jobs = store.all()
        running_keys = {j["key"] for j in db_jobs if j["state"] == "running" and j["type"] == "ttv_fit"}
        db_by_key = {j["key"]: j for j in db_jobs}

        for key, job in _TTV_JOBS.items():
            db_key = f"ttv_fit:{job.inst}/{job.date}/{job.target.replace(' ', '')}"
            if job.run_id:
                db_key = f"{db_key}/{job.run_id}"
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
                inst=job.inst or "_",
                date=job.date or "_",
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
            target = row["target"]
            run_name = row.get("run_name") or ""
            completed_ok = False
            rdir = None
            try:
                rdir = ttv_output_dir(target, run_name)
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
                    inst=row.get("inst") or "_", date=row.get("date") or "_", target=target,
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
                    inst=row.get("inst") or "_", date=row.get("date") or "_", target=target,
                    state="error", returncode=-1,
                    elapsed=row["elapsed"],
                    started_at=row["started_at"],
                    error_desc="Process lost (server restart)",
                    run_id=row.get("run_id"),
                    run_name=run_name,
                )
                database.refresh_target_status(target)

        # Release any concurrency slot whose claimant's persisted job row is
        # no longer 'running' (crashed/restarted without releasing cleanly).
        # Checked against the durable jobs table, so this is safe to run from
        # any process -- unlike the per-process _TTV_JOBS dict above, it does
        # not assume this process is the one that held the slot.
        store.reconcile_slots("ttv_fit")

        if store.count_claimed("ttv_fit") < _MAX_FULL_JOBS:
            pending = store.pending("ttv_fit")
            for entry in pending:
                if store.count_claimed("ttv_fit") >= _MAX_FULL_JOBS:
                    break
                try:
                    p = json.loads(entry.get("params") or "{}")
                except (json.JSONDecodeError, TypeError):
                    p = {}
                opts = p.get("options", {})
                run_name = entry.get("run_name") or opts.get("run_name") or ""
                run_seg = slugify_run_name(run_name)
                target = entry["target"]
                try:
                    mem_key = ttv_job_key(target, run_name)
                except ValueError:
                    continue
                if mem_key in _TTV_JOBS:
                    store.save(type_="ttv_fit", inst="_", date="_", target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Duplicate entry", run_id=run_seg, run_name=run_name)
                    continue
                try:
                    key = ttv_job_key(target, run_name)
                    rdir = ttv_output_dir(target, run_name)
                except ValueError:
                    store.save(type_="ttv_fit", inst="_", date="_", target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Invalid target", run_id=run_seg, run_name=run_name)
                    continue
                # Atomic: at most one process/caller ever wins this claim for a
                # given key, so two workers draining the same pending row can
                # never both launch it.
                if not store.claim_slot("ttv_fit", key, _MAX_FULL_JOBS):
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
                mu_min_me = opts.get("mu_min_me")
                if mu_min_me is not None:
                    cmd.extend(["--mu-min-me", str(mu_min_me)])
                mu_max_me = opts.get("mu_max_me")
                if mu_max_me is not None:
                    cmd.extend(["--mu-max-me", str(mu_max_me)])
                z_max = opts.get("z_max")
                if z_max is not None:
                    cmd.extend(["--z-max", str(z_max)])
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
                    store.release_slot("ttv_fit", key)
                    store.save(type_="ttv_fit", inst="_", date="_", target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc=f"Failed to launch: {exc}", run_id=run_seg, run_name=run_name)
                    continue
                _TTV_JOBS[key] = TTVFitJob(key=key, inst="_", date="_", target=target, cmd=cmd, proc=proc, logf=logf, log_path=log_path, run_type="full", run_id=run_seg, run_name=run_name)
                try:
                    store.save(type_="ttv_fit", inst="_", date="_", target=target, state="running", returncode=None, elapsed=0, started_at=_TTV_JOBS[key].started_at, run_type="full", params=entry.get("params", ""), run_id=run_seg, run_name=run_name)
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
                    store.release_slot("ttv_fit", key)
                    store.save(type_="ttv_fit", inst="_", date="_", target=target, state="error", returncode=-1, elapsed=0, started_at=entry["started_at"], error_desc="Database error", run_id=run_seg, run_name=run_name)
