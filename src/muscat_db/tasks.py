import os
import sys
import ctypes
import signal
import subprocess
import shlex
import time
import threading
from pathlib import Path
from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded

# Import configuration helper functions from photometry and transit_fit
from muscat_db.photometry import (
    normalize_run_options,
    build_command,
    prose_project_dir,
    _RUN_LOG_NAME,
    results_dir,
)
from muscat_db.transit_fit import (
    fit_output_dir,
    _timer_prefix,
)

# Redis URL configuration
REDIS_URL = os.environ.get("MUSCAT_REDIS_URL", "redis://localhost:6379/0")

# Determine if we should run in local fallback mode (e.g., during tests or if Redis is not configured/wanted)
IS_TESTING = "pytest" in sys.modules or os.environ.get("MUSCAT_TESTING") == "1"
USE_LOCAL_FALLBACK = IS_TESTING or not REDIS_URL

# Linux-specific PDEATHSIG to clean up child processes when parent dies
def set_pdeathsig():
    try:
        # PR_SET_PDEATHSIG = 1, SIGTERM = 15
        ctypes.CDLL("libc.so.6").prctl(1, 15)
    except Exception:
        pass

# ── Celery Tasks Definition ──────────────────────────────────────────────────
app = Celery("muscat_tasks", broker=REDIS_URL, backend=REDIS_URL)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

def execute_photometry(inst: str, date: str, target: str, options: dict, test_run: bool) -> int:
    opts = normalize_run_options(options)
    cmd = build_command(inst, date, target, opts, test_run=test_run)
    rdir = results_dir(inst, date)
    rdir.mkdir(parents=True, exist_ok=True)
    log_path = rdir / _RUN_LOG_NAME

    with open(log_path, "w") as logf:
        logf.write(f"$ {shlex.join(cmd)}\n\n")
        logf.flush()
        
        proc = subprocess.Popen(
            cmd,
            cwd=str(prose_project_dir()),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=set_pdeathsig,
            start_new_session=True,
        )
        
        # Store process reference on the current thread for cancellation/revoke handling
        threading.current_thread().active_proc = proc
        
        try:
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"Photometry process exited with code {rc}")
            return rc
        except Exception as exc:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except OSError:
                    pass
            raise exc

def execute_transit_fit(inst: str, date: str, target: str, test_run: bool) -> int:
    rdir = fit_output_dir(inst, date, target)
    cmd = [*_timer_prefix(), "-v", str(rdir)]
    if test_run:
        cmd.append("--test_run")
    log_path = rdir / "timer-fit.log"

    with open(log_path, "w") as logf:
        logf.write(f"$ {shlex.join(cmd)}\n\n")
        logf.flush()
        
        proc = subprocess.Popen(
            cmd,
            cwd=str(rdir),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=set_pdeathsig,
            start_new_session=True,
        )
        
        threading.current_thread().active_proc = proc
        
        try:
            rc = proc.wait()
            if rc != 0:
                raise RuntimeError(f"Transit fit process exited with code {rc}")
            return rc
        except Exception as exc:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except OSError:
                    pass
            raise exc

@app.task(name="muscat_db.tasks.run_photometry")
def run_photometry(inst: str, date: str, target: str, options: dict, test_run: bool) -> int:
    return execute_photometry(inst, date, target, options, test_run)

@app.task(name="muscat_db.tasks.run_transit_fit")
def run_transit_fit(inst: str, date: str, target: str, test_run: bool) -> int:
    return execute_transit_fit(inst, date, target, test_run)

# ── Local Fallback Runner (Mocking Celery for Testing/Development) ────────────

class MockAsyncResult:
    def __init__(self, task_id: str):
        self.task_id = task_id

    @property
    def state(self) -> str:
        with _LOCAL_JOBS_LOCK:
            job = _LOCAL_JOBS.get(self.task_id)
            if not job:
                return "PENDING"
            return job["state"]

    @property
    def result(self):
        with _LOCAL_JOBS_LOCK:
            job = _LOCAL_JOBS.get(self.task_id)
            if not job:
                return None
            return job.get("result")

    def revoke(self, terminate=True, signal=None):
        with _LOCAL_JOBS_LOCK:
            job = _LOCAL_JOBS.get(self.task_id)
            if not job or job["state"] in ("SUCCESS", "FAILURE", "REVOKED"):
                return
            
            job["state"] = "REVOKED"
            thread = job.get("thread")
            if thread and hasattr(thread, "active_proc"):
                proc = thread.active_proc
                if proc and proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal or 15)
                    except OSError:
                        try:
                            proc.terminate()
                        except OSError:
                            pass


_LOCAL_JOBS = {}
_LOCAL_JOBS_LOCK = threading.Lock()

def local_run_task(task_id: str, func, *args):
    def target_fn():
        with _LOCAL_JOBS_LOCK:
            _LOCAL_JOBS[task_id]["state"] = "STARTED"
            _LOCAL_JOBS[task_id]["thread"] = threading.current_thread()
        
        try:
            res = func(*args)
            with _LOCAL_JOBS_LOCK:
                # Only set to SUCCESS if not already revoked
                if _LOCAL_JOBS[task_id]["state"] != "REVOKED":
                    _LOCAL_JOBS[task_id]["state"] = "SUCCESS"
                    _LOCAL_JOBS[task_id]["result"] = res
        except Exception as exc:
            with _LOCAL_JOBS_LOCK:
                if _LOCAL_JOBS[task_id]["state"] != "REVOKED":
                    _LOCAL_JOBS[task_id]["state"] = "FAILURE"
                    _LOCAL_JOBS[task_id]["result"] = exc

    with _LOCAL_JOBS_LOCK:
        _LOCAL_JOBS[task_id] = {
            "state": "PENDING",
            "thread": None,
            "result": None,
        }
    
    t = threading.Thread(target=target_fn, daemon=True)
    t.start()


# Helper function to invoke tasks and retrieve results
def dispatch_run_photometry(inst: str, date: str, target: str, options: dict, test_run: bool, task_id: str) -> None:
    if USE_LOCAL_FALLBACK:
        local_run_task(task_id, execute_photometry, inst, date, target, options, test_run)
    else:
        run_photometry.apply_async(
            args=[inst, date, target, options, test_run],
            task_id=task_id,
        )

def dispatch_run_transit_fit(inst: str, date: str, target: str, test_run: bool, task_id: str) -> None:
    if USE_LOCAL_FALLBACK:
        local_run_task(task_id, execute_transit_fit, inst, date, target, test_run)
    else:
        run_transit_fit.apply_async(
            args=[inst, date, target, test_run],
            task_id=task_id,
        )

def get_task_result(task_id: str):
    if USE_LOCAL_FALLBACK:
        return MockAsyncResult(task_id)
    else:
        from celery.result import AsyncResult
        return AsyncResult(task_id)
