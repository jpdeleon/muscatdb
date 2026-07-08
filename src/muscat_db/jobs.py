"""Shared background-job lifecycle primitives for the photometry and
transit-fit pipelines.

Both pipelines launch external science tools as detached subprocesses
(``start_new_session=True``) whose multiprocessing workers keep appending to the
run log *after* the tracked parent process exits. Declaring a job terminal the
instant ``proc.poll()`` returns freezes the live-log view mid-output while the
Jobs page (which reads the log file directly) keeps advancing. This module is the
single source of truth for that lifecycle — the ``finalizing`` grace-window state
machine, the run-id / path-segment helpers, and the process-group kill helpers —
so the two pipelines cannot drift (architecture audit finding C1).

Pipeline-specific knobs (grace windows, the log lines that mark a pipeline's own
completion, and whether a zero-exit run can still be a partial failure) are
passed in via :class:`FinalizeConfig`; the callers build that config from their
own module-level, env-tunable settings on every call so those settings stay
monkeypatch-/override-able at runtime.

The module is intentionally dependency-free (stdlib only) to avoid an import
cycle: ``photometry`` and ``transit_fit`` import from here, never the reverse.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO


# --------------------------- run-id / path-segment helpers ---------------------------

_RUN_NAME_MAX = 40
_RUN_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_run_name(run_name: str | None) -> str:
    """Slug a user run label: lowercase, non-alphanumeric -> ``_``, trimmed,
    length-capped. Blank input -> ``default``. Never contains ``-`` so it stays
    unambiguous under the ``-`` run-id join."""
    s = _RUN_SLUG_RE.sub("_", (run_name or "").strip().lower()).strip("_")
    return s[:_RUN_NAME_MAX].strip("_") or "default"


def target_dir_name(target: str) -> str:
    """Validate a target used as a single path segment.

    Spaces are stripped (mirrors prose ``build_stem``). Rejects empty names and
    any path-traversal token so the result is always a safe single segment.
    """
    name = (target or "").replace(" ", "")
    if not name or ".." in name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("invalid target")
    return name


def run_dir_name(run_id: str) -> str:
    """Validate a run-id used as a single path segment (same rules as target)."""
    rid = (run_id or "").strip()
    if not rid or ".." in rid or "/" in rid or "\\" in rid or rid in {".", ".."}:
        raise ValueError("invalid run id")
    return rid


def build_run_id(site: str | None, mode: str | None, run_name: str | None) -> str:
    """Compose a run id from optional site, optional mode, and a run-name slug.

    Components are joined by ``-`` (the components themselves only ever contain
    ``_``, so ``-`` keeps the id readable and splittable). ``site``/``mode`` are
    blank for non-sinistro or when undetermined; ``mixed`` when the selected
    lightcurves span more than one value (mixing is allowed). Sinistro's
    ``central_2k_2x2`` readout mode is the default and is omitted; non-default
    modes such as ``full_frame`` remain explicit.
    """
    mode_part = (mode or "").strip().lower()
    if mode_part == "central_2k_2x2":
        mode_part = ""
    parts = [
        p
        for p in (
            (site or "").strip().lower(),
            mode_part,
            slugify_run_name(run_name),
        )
        if p
    ]
    return "-".join(parts)


# --------------------------- background job record ---------------------------


@dataclass
class PipelineJob:
    """In-memory record of one launched pipeline subprocess.

    Shared by both pipelines; each re-exports it under its historical name
    (``photometry.Job`` / ``transit_fit.TransitFitJob``).
    """

    key: str
    inst: str
    date: str
    target: str
    cmd: list[str]
    proc: subprocess.Popen
    logf: IO
    log_path: Path
    started_at: float = field(default_factory=time.time)
    state: str = "running"  # running | done | error | cancelled
    returncode: int | None = None
    cancelled: bool = False
    elapsed: int | None = None
    run_type: str = "full"  # "test" | "full"
    run_id: str = ""  # site-mode-runname slug; "" == legacy single-dir run
    site: str = ""
    mode: str = ""
    run_name: str = ""


def count_running_full(registry: dict[str, PipelineJob]) -> int:
    """Number of currently-running full (non-test) jobs in *registry*."""
    return sum(
        1 for j in registry.values() if j.run_type == "full" and j.proc.poll() is None
    )


# --------------------------- finalizing state machine ---------------------------


@dataclass(frozen=True)
class FinalizeConfig:
    """Pipeline-specific knobs for the finalizing grace-window state machine.

    - ``grace_s``: quiescence window before a finished parent is declared
      terminal, so workers writing after parent-exit keep the live log streaming.
    - ``grace_terminal_s``: the shorter window used once the pipeline has logged a
      terminal result line (remaining writes are just worker teardown).
    - ``terminal_markers``: log substrings the pipeline emits once its real work is
      decided. Any of them shrinks the quiescence window to ``grace_terminal_s``.
    - ``partial_failure_marker``: a log substring that turns a zero-exit run into
      the ``error`` state (photometry partial runs). ``None`` disables the mapping.
    - ``success_marker``: a log substring the pipeline emits only after all real
      work has succeeded (photometry writes it last, after every output file).
      When present it makes success authoritative over a non-zero/lost parent
      return code — the reduction runs in detached workers that outlive and are
      independent of the tracked parent, so a killed/reloaded/lost parent must
      not override a logged success. ``None`` disables the mapping (the parent's
      return code stays authoritative, e.g. transit-fit).
    """

    grace_s: int
    grace_terminal_s: int
    terminal_markers: tuple[str, ...]
    partial_failure_marker: str | None = None
    success_marker: str | None = None


def tail(path: Path, n: int = 200) -> str:
    """Return the last *n* lines of a log file, or ``""`` if unreadable/absent."""
    if not path.is_file():
        return ""
    try:
        with open(path, errors="replace") as f:
            return "".join(deque(f, maxlen=n))
    except OSError:
        return ""


def log_has_terminal_marker(path: Path | None, cfg: FinalizeConfig) -> bool:
    """True once the pipeline has logged a final result line. After this the
    only remaining writes are worker teardown, so the finalize window can shrink."""
    if path is None or not path.is_file():
        return False
    t = tail(path, n=1000)
    return any(marker in t for marker in cfg.terminal_markers)


def log_has_partial_failure(path: Path | None, cfg: FinalizeConfig) -> bool:
    """True when the log records a partial-failure result (zero-exit but not all
    work succeeded). Always False when the pipeline has no such concept."""
    if cfg.partial_failure_marker is None or path is None or not path.is_file():
        return False
    return cfg.partial_failure_marker in tail(path, n=1000)


def log_has_success(path: Path | None, cfg: FinalizeConfig) -> bool:
    """True when the log records a successful result line. Always False when the
    pipeline defines no success marker (its parent return code stays authoritative)."""
    if cfg.success_marker is None or path is None or not path.is_file():
        return False
    return cfg.success_marker in tail(path, n=1000)


def finalize_grace_s(path: Path | None, cfg: FinalizeConfig) -> int:
    """Effective finalize quiescence window for a log: the short terminal window
    once a result line is logged, else the conservative default. The ``min``
    guards against a default set below the terminal window — there is never a
    reason to wait longer after the result line than before it."""
    if log_has_terminal_marker(path, cfg):
        return min(cfg.grace_terminal_s, cfg.grace_s)
    return cfg.grace_s


def log_quiescent(path: Path, now: float, cfg: FinalizeConfig) -> bool:
    """True when the log has not been written for at least the finalize grace
    window. A missing/unreadable log means nothing more is coming, so it counts
    as quiescent; each append by a still-running worker refreshes the mtime and
    keeps the job finalizing."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True
    return (now - mtime) >= finalize_grace_s(path, cfg)


def terminal_job_state(
    returncode: int,
    cancelled: bool,
    log_path: Path | None,
    cfg: FinalizeConfig,
) -> str:
    """Map process completion to a terminal state.

    Two log markers override the raw parent return code:

    - a logged *partial failure* is an error even on a zero exit; and
    - a logged *success* is a success even on a non-zero/lost parent, because the
      real work runs in detached workers that outlive the tracked parent (a
      ``--reload`` restart, watchdog, or SIGHUP can leave the parent non-zero
      while the reduction completed and wrote every output). Partial failure
      wins over success when both somehow appear.
    """
    if cancelled:
        return "cancelled"
    if log_has_partial_failure(log_path, cfg):
        return "error"
    if returncode != 0:
        return "done" if log_has_success(log_path, cfg) else "error"
    return "done"


def resolve_job_state(
    job: PipelineJob, cfg: FinalizeConfig, now: float | None = None
) -> tuple[str, int | None, bool]:
    """Resolve a tracked job's live state as ``(state, returncode, is_terminal)``.

    While the parent process runs the job is ``running``/``cancelling``. Once it
    exits, a non-cancelled job is reported as ``finalizing`` (non-terminal) until
    its log goes quiescent, so the live view keeps streaming the output workers
    emit after parent-exit. A cancelled job goes terminal immediately to keep the
    Cancel flow responsive.
    """
    rc = job.proc.poll()
    if rc is None:
        return ("cancelling" if job.cancelled else "running"), None, False
    if not job.cancelled and not log_quiescent(
        job.log_path, now if now is not None else time.time(), cfg
    ):
        return "finalizing", rc, False
    return terminal_job_state(rc, job.cancelled, job.log_path, cfg), rc, True


# --------------------------- process-group control ---------------------------


def kill_after(proc: subprocess.Popen, grace: float = 6.0) -> None:
    """Escalate to SIGKILL on the process group if SIGTERM was ignored."""
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def terminate_pg(proc: subprocess.Popen) -> None:
    """SIGTERM a job's whole process group, escalating to SIGKILL in the
    background. The whole tree must be signalled because the pipelines spawn
    multiprocessing workers in their own session."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    threading.Thread(target=kill_after, args=(proc,), daemon=True).start()
