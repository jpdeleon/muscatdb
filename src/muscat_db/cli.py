from __future__ import annotations

import os
import re
import signal
import time
from datetime import date

import click
import typer
from typer.main import TyperCommand
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE
from muscat_db.scanner import scan_date, scan_missing_dates, scan_yesterday
from muscat_db.summarizer import summarize_csv

_INST_CHOICES = click.Choice(list(INSTRUMENTS))
_CCD_CHOICES = click.Choice(["0", "1", "2", "3"])


class _Cmd(TyperCommand):
    """Command that shows valid choices for missing arguments."""

    def main(self, *args, **kwargs):
        try:
            return super().main(*args, **kwargs)
        except click.MissingParameter as e:
            param = e.param
            if param is not None:
                ptype = param.type
                choices = getattr(ptype, "choices", None)
                if choices:
                    msg = f"Missing argument '{param.name}'. Choose from: {', '.join(choices)}"
                    raise click.UsageError(msg) from e
                if isinstance(ptype, click.IntRange):
                    r = f"{ptype.min or 0}-{ptype.max or '?'}"
                    msg = f"Missing argument '{param.name}'. Valid range: {r}"
                    raise click.UsageError(msg) from e
            raise


app = typer.Typer(
    name="muscat-db",
    help="MuSCAT observation log pipeline",
    no_args_is_help=True,
)
console = Console()

_WORKER_OPTION = typer.Option(None, "--workers", "-w", help="Parallel worker count (default: cpu_count)")


def _complete_instrument() -> list[str]:
    return list(INSTRUMENTS)


def _complete_ccd() -> list[str]:
    return ["0", "1", "2", "3"]


def _complete_year() -> list[str]:
    this_year = date.today().year
    return ["all", *(str(y)[2:] for y in range(this_year, this_year - 5, -1))]


def _complete_obsdate(ctx: typer.Context) -> list[str]:
    instrument = ctx.params.get("instrument")
    if not instrument or instrument not in INSTRUMENTS:
        return _complete_year()
    basedir = f"{OBSLOG_BASE}/{instrument}"
    if not os.path.isdir(basedir):
        return _complete_year()
    return sorted(
        (d for d in os.listdir(basedir) if os.path.isdir(f"{basedir}/{d}") and d.isdigit()),
        reverse=True,
    )[:50]


def _obsdate_callback(value: str) -> str:
    if not re.fullmatch(r"\d{6}", value):
        raise typer.BadParameter(f"must be 6-digit yymmdd, got '{value}'")
    return value


@app.command(cls=_Cmd)
def scan(
    instrument: str = typer.Argument(
        ..., help="Instrument name", autocompletion=_complete_instrument,
        click_type=_INST_CHOICES,
    ),
    obsdate: str = typer.Argument(
        ..., help="Observation date (yymmdd)", autocompletion=_complete_obsdate,
        callback=_obsdate_callback,
    ),
    workers: int | None = _WORKER_OPTION,
):
    """Scan FITS files and generate observation log CSV for a single date."""
    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[bold]{task.fields[filename]}"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            result = scan_date(instrument, obsdate, max_workers=workers, progress=progress)
        if not result:
            console.print(f"[yellow]No FITS files found for {instrument} {obsdate}[/]")
        else:
            per_ccd = result["per_ccd"]
            parts = [f"CCD{c}: {n}" for c, n in sorted(per_ccd.items())]
            console.print(f"[green]{instrument} {obsdate}: {result['total']} frames ({', '.join(parts)})[/]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise typer.Exit(1)


@app.command(cls=_Cmd)
def scan_missing(
    instrument: str = typer.Argument(
        ..., help="Instrument name", autocompletion=_complete_instrument,
        click_type=_INST_CHOICES,
    ),
    year: str = typer.Argument(
        ..., help="Year prefix (e.g. 25) or 'all' for every date dir",
        autocompletion=_complete_year,
    ),
    workers: int | None = _WORKER_OPTION,
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Rescan every date with FITS data, overwriting existing obslog CSVs.",
    ),
):
    """Scan all dates for an instrument that don't yet have an obslog (or all, with --force)."""
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[bold]{task.fields[filename]}"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        dates = scan_missing_dates(
            instrument, year, max_workers=workers, progress=progress, force=force,
        )
    label = "Rescanned" if force else "Scanned"
    if dates:
        console.print(f"[green]{label} {len(dates)} dates for {instrument}[/]")
    else:
        console.print(f"[yellow]No dates found for {instrument} {year}[/]")


@app.command(cls=_Cmd)
def scan_all(
    year: str = typer.Argument(
        ..., help="Year prefix (e.g. 25) or 'all' for every date dir",
        autocompletion=_complete_year,
    ),
    workers: int | None = _WORKER_OPTION,
):
    """Scan missing dates for all instruments."""
    from muscat_db.scanner import scan_all_instruments
    result = scan_all_instruments(year, max_workers=workers)
    total = sum(len(v) for v in result.values())
    if result:
        for name, dates in result.items():
            for d in dates:
                console.print(f"  [green]{name} {d}[/]")
        console.print(f"[green]Scanned {total} dates across {len(result)} instruments[/]")
    else:
        console.print("[yellow]No new dates found[/]")


@app.command(cls=_Cmd)
def scan_yesterday_cmd(
    workers: int | None = _WORKER_OPTION,
):
    """Scan yesterday's data for all instruments (cron-friendly)."""
    scanned = scan_yesterday(max_workers=workers)
    if scanned:
        console.print(f"[green]Scanned yesterday for: {', '.join(scanned)}[/]")
    else:
        console.print("[yellow]No data found for yesterday[/]")


@app.command(cls=_Cmd)
def summary(
    instrument: str = typer.Argument(
        ..., help="Instrument name", autocompletion=_complete_instrument,
        click_type=_INST_CHOICES,
    ),
    obsdate: str = typer.Argument(
        ..., help="Observation date (yymmdd)", autocompletion=_complete_obsdate,
        callback=_obsdate_callback,
    ),
    ccd: int = typer.Argument(
        ..., help="CCD number", autocompletion=_complete_ccd,
        click_type=_CCD_CHOICES,
    ),
):
    """Print summary of an observation log (table layout)."""
    rows = summarize_csv(instrument, obsdate, ccd)
    if not rows:
        console.print(f"[red]No obslog for {instrument} {obsdate} ccd{ccd}[/]")
        return
    table = Table(title=f"{instrument} {obsdate} CCD{ccd} — Summary")
    table.add_column("OBJECT", style="cyan")
    table.add_column("EXPTIME", justify="right")
    table.add_column("READ_MODE")
    table.add_column("FRAME#1", justify="right")
    table.add_column("FRAME#2", justify="right")
    table.add_column("UT-STRT1")
    table.add_column("UT-STRT2")
    table.add_column("NFRAMES", justify="right")
    for r in rows:
        table.add_row(
            r.object, r.exptime, r.read_mode,
            r.frame_start, r.frame_end,
            r.ut_start, r.ut_end,
            str(r.nframes),
        )
    console.print(table)


@app.command(cls=_Cmd)
def build_db(
    db: str = typer.Option("muscat.db", "--db", help="SQLite database path"),
):
    """Build SQLite database from all CSV observation logs."""
    from muscat_db.database import build_db as _build_db
    console.print("[cyan]Scanning observation logs...[/]")
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[bold]{task.fields[filename]}"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        count = _build_db(db, progress=progress)
    console.print(f"[green]Database built: {count} frames indexed in {db}[/]")


def _pids_listening_on(port: int) -> list[int]:
    """PIDs of processes holding a LISTEN socket on ``port`` (Linux /proc).

    Reads the listening-socket inodes for the port from ``/proc/net/tcp{,6}``,
    then maps them to owning PIDs via each process's ``/proc/<pid>/fd`` links.
    Dependency-free; returns an empty list if nothing is listening.
    """
    listen_state = "0A"  # TCP_LISTEN in /proc/net/tcp
    inodes: set[str] = set()
    for proto in ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{proto}") as fh:
                next(fh, None)  # skip header
                for line in fh:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != listen_state:
                        continue
                    local_port = int(parts[1].rsplit(":", 1)[1], 16)
                    if local_port == port:
                        inodes.add(parts[9])
        except OSError:
            continue
    if not inodes:
        return []

    pids: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.append(int(entry))
                    break
        except OSError:
            continue  # process vanished or not ours to inspect
    return pids


def _stop_running_servers(port: int, *, timeout: float = 5.0) -> list[int]:
    """Terminate any servers listening on ``port``. Returns the PIDs signalled.

    Sends SIGTERM first (catches uvicorn's reloader parent and worker, both of
    which hold the socket), waits up to ``timeout`` for a clean exit, then
    SIGKILLs any straggler still bound to the port.
    """
    me = os.getpid()
    pids = [p for p in _pids_listening_on(port) if p != me]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not [p for p in _pids_listening_on(port) if p != me]:
            return pids
        time.sleep(0.2)

    for pid in _pids_listening_on(port):
        if pid == me:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return pids


def _run_server(db: str, host: str, port: int, reload: bool, workers: int = 1) -> None:
    os.environ["MUSCAT_DB_PATH"] = db
    import uvicorn
    if reload:
        uvicorn.run("muscat_db.web:app", host=host, port=port, reload=True)
    else:
        uvicorn.run("muscat_db.web:app", host=host, port=port, workers=workers)


@app.command(cls=_Cmd)
def serve(
    db: str = typer.Option("muscat.db", "--db", help="SQLite database path"),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address"),
    port: int = typer.Option(8000, "--port", "-p", help="Port number"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of worker processes"),
):
    """Start the web frontend."""
    _run_server(db, host, port, reload, workers)


@app.command(cls=_Cmd)
def restart(
    db: str = typer.Option("muscat.db", "--db", help="SQLite database path"),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address"),
    port: int = typer.Option(8000, "--port", "-p", help="Port number"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of worker processes"),
):
    """Stop any server already running on the port, then start a fresh one."""
    stopped = _stop_running_servers(port)
    if stopped:
        console.print(f"[yellow]Stopped running server (pid {', '.join(map(str, stopped))}) on port {port}[/]")
    else:
        console.print(f"[dim]No server running on port {port}[/]")
    console.print(f"[green]Starting server on {host}:{port}[/]")
    _run_server(db, host, port, reload, workers)


if __name__ == "__main__":
    app()
