"""Repair "midnight splits" in the raw LCO data tree.

A single observing night that straddles 00:00 UTC was historically filed into
two ``<inst>/<yymmdd>/`` directories, because the destination came from
``DATE_OBS`` (the frame's UTC timestamp) rather than the DAY-OBS token LCO
stamps into the filename. The database groups by directory name, so the night
shows up as two datasets. See the "UTC-midnight dataset split" section of
README.md.

The repair rule is entirely filename-driven, and therefore idempotent and
verifiable without any timezone or longitude arithmetic:

    a file belongs in the directory matching the DAY-OBS token in its own name.

Consolidation has to happen in the *raw FITS tree*, because ``build-db`` drops
and rebuilds ``frames``/``summaries``/``targets`` from the obslog CSVs on every
run — anything written to ``muscat.db`` is erased nightly, and the CSVs are
themselves regenerated from the FITS tree.

Scope is deliberately narrow. Only *mixed* directories (holding more than one
DAY-OBS) are repaired. A directory whose files all agree with each other but
disagree with the directory name is reported as an ``offset`` issue and left
alone: it is internally consistent, it does not produce the split symptom, and
renaming it would change a dataset label that photometry output paths,
``target_notes`` primary keys and job records already reference.
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from muscat_db.dayobs import dayobs_from_filename
from muscat_db.instruments import INSTRUMENTS, OBSLOG_BASE
from muscat_db.scanner import _find_fits_files, _read_fits_header_keys

logger = logging.getLogger(__name__)

# Instruments fed by the LCO archive, whose filenames carry a DAY-OBS token.
# muscat/muscat2 are excluded: their names have no token and their directory
# naming is already observing-night based.
LCO_INSTRUMENTS: tuple[str, ...] = ("sinistro", "muscat3", "muscat4")

# A file more than this many days from its directory is not a midnight artifact;
# it is misfiled data and needs a human, not an automatic move.
_MAX_DAY_DELTA = 1


@dataclass(frozen=True)
class Move:
    """One file relocation from its current directory to its DAY-OBS directory."""

    src: Path
    dst: Path
    src_date: str
    dst_date: str


@dataclass(frozen=True)
class Issue:
    """Something the normalizer refuses to touch, reported for review.

    ``reason`` is one of:

    ``offset``
        Every file in the directory carries the same DAY-OBS, and it is not the
        directory name. Not a split — a whole-directory relabel, out of scope.
    ``stray``
        A file whose DAY-OBS is more than a day from its directory. Misfiled
        data rather than a midnight artifact.
    ``conflict``
        The destination already holds a file of that name.
    """

    instrument: str
    obsdate: str
    reason: str
    detail: str
    # Populated for ``conflict`` only, so callers can act on the pair without
    # re-parsing ``detail``.
    src: Path | None = None
    dst: Path | None = None


@dataclass(frozen=True)
class Plan:
    """The moves and issues found for one instrument. Building it is read-only."""

    instrument: str
    moves: tuple[Move, ...]
    issues: tuple[Issue, ...]
    obslog_root: Path

    @property
    def touched_dates(self) -> tuple[str, ...]:
        """Every date directory a move reads from or writes to.

        Both ends must be rescanned: the source loses frames and the
        destination gains them.
        """
        dates = {m.src_date for m in self.moves} | {m.dst_date for m in self.moves}
        return tuple(sorted(dates))


@dataclass(frozen=True)
class ApplyResult:
    moved: int
    touched_dates: tuple[str, ...]
    removed_csvs: tuple[Path, ...]
    removed_dirs: tuple[Path, ...]


@dataclass(frozen=True)
class DedupeResult:
    """``deleted`` are redundant source copies; ``kept`` pairs each retained
    path with the reason it was not safe to remove."""

    deleted: tuple[Path, ...]
    kept: tuple[tuple[Path, str], ...]


def _is_obsdate_dir(name: str) -> bool:
    """True only for a canonical YYMMDD directory name."""
    if len(name) != 6 or not name.isdigit():
        return False
    try:
        datetime.datetime.strptime(name, "%y%m%d")
    except ValueError:
        return False
    return True


def _day_delta(a: str, b: str) -> int:
    fmt = "%y%m%d"
    da = datetime.datetime.strptime(a, fmt)
    db = datetime.datetime.strptime(b, fmt)
    return abs((da - db).days)


def _instrument_dir(inst_name: str, data_root: str | os.PathLike[str] | None) -> Path:
    inst = INSTRUMENTS[inst_name]
    if data_root is None:
        return Path(inst.data_dir)
    return Path(data_root).expanduser() / inst.data_subdir


def _scannable_files(
    inst_name: str, obsdate: str, data_root: str | os.PathLike[str] | None
) -> list[Path]:
    """Files the instrument's own scanner would ingest for this date.

    Reuses ``scanner._find_fits_files`` rather than re-deriving the glob, so the
    normalizer can never act on a file the pipeline ignores. This matters: the
    raw tree contains cross-filed data (e.g. ``coj2m002-`` MuSCAT4 frames under
    ``/data/MuSCAT3/``) that no instrument scans. Relocating those would churn
    files without changing anything the database sees, and would leave them in
    the wrong instrument tree regardless.
    """
    inst = INSTRUMENTS[inst_name]
    found: list[Path] = []
    seen: set[Path] = set()
    for ccd in range(inst.nccd):
        for raw in _find_fits_files(inst, obsdate, ccd, data_root=data_root):
            path = Path(raw)
            if path not in seen:
                seen.add(path)
                found.append(path)
    return sorted(found)


def _group_by_dayobs(files: list[Path]) -> dict[str, list[Path]]:
    """Map DAY-OBS token -> files. Files without a token are omitted entirely;
    "no token" means "cannot judge", never "belongs here"."""
    groups: dict[str, list[Path]] = {}
    for path in files:
        token = dayobs_from_filename(path.name)
        if token is not None:
            groups.setdefault(token, []).append(path)
    return groups


def plan_instrument(
    inst_name: str,
    *,
    data_root: str | os.PathLike[str] | None = None,
    obslog_root: str | os.PathLike[str] | None = None,
    max_day_delta: int = _MAX_DAY_DELTA,
) -> Plan:
    """Survey one instrument's raw tree. Read-only — nothing is modified."""
    inst_dir = _instrument_dir(inst_name, data_root)
    obslog = Path(obslog_root) if obslog_root is not None else Path(OBSLOG_BASE)
    moves: list[Move] = []
    issues: list[Issue] = []

    if not inst_dir.is_dir():
        return Plan(inst_name, (), (), obslog)

    try:
        date_dirs = sorted(p for p in inst_dir.iterdir() if p.is_dir())
    except (PermissionError, OSError) as e:
        logger.warning("cannot list %s: %s", inst_dir, e)
        return Plan(inst_name, (), (), obslog)

    for date_dir in date_dirs:
        obsdate = date_dir.name
        if not _is_obsdate_dir(obsdate):
            continue
        groups = _group_by_dayobs(_scannable_files(inst_name, obsdate, data_root))
        if not groups or set(groups) == {obsdate}:
            continue

        # A directory holding more than one DAY-OBS has two nights in it: the
        # minority must move regardless of whether its destination exists yet.
        # A directory whose files all agree with each other is only a split when
        # that night already has a home elsewhere — otherwise moving them is a
        # bare directory rename, which is out of scope (see module docstring).
        is_mixed = len(groups) > 1

        for token, files in sorted(groups.items()):
            if token == obsdate:
                continue
            if _day_delta(token, obsdate) > max_day_delta:
                issues.append(Issue(
                    inst_name, obsdate, "stray",
                    f"DAY-OBS {token} is >{max_day_delta}d away: "
                    + ", ".join(p.name for p in files),
                ))
                continue
            if not is_mixed and not (inst_dir / token).is_dir():
                issues.append(Issue(
                    inst_name, obsdate, "offset",
                    f"all {len(files)} frame(s) carry DAY-OBS {token} and no "
                    f"{token}/ directory exists; whole-directory relabel, out of scope",
                ))
                continue
            for path in files:
                dst = inst_dir / token / path.name
                if dst.exists():
                    issues.append(Issue(
                        inst_name, obsdate, "conflict",
                        f"{dst} already exists; not overwriting {path.name}",
                        src=path, dst=dst,
                    ))
                    continue
                moves.append(Move(path, dst, obsdate, token))

    return Plan(inst_name, tuple(moves), tuple(issues), obslog)


def plan_all(
    *,
    data_root: str | os.PathLike[str] | None = None,
    obslog_root: str | os.PathLike[str] | None = None,
    instruments: tuple[str, ...] = LCO_INSTRUMENTS,
) -> tuple[Plan, ...]:
    """Survey every LCO-fed instrument. Read-only."""
    return tuple(
        plan_instrument(name, data_root=data_root, obslog_root=obslog_root)
        for name in instruments
    )


def _stale_obslog_csvs(obslog_root: Path, inst_name: str, obsdate: str) -> list[Path]:
    obsdir = obslog_root / inst_name / obsdate
    if not obsdir.is_dir():
        return []
    try:
        return sorted(p for p in obsdir.glob("obslog-*.csv") if p.is_file())
    except (PermissionError, OSError) as e:
        logger.warning("cannot list %s: %s", obsdir, e)
        return []


def _datasum(path: Path) -> str | None:
    """The frame's FITS ``DATASUM`` (a checksum over the data unit), or ``None``.

    ``DATASUM`` covers the pixel data only, so it is the right equality test for
    "same frame". Comparing bytes or ``md5sum`` is misleading: the ``CHECKSUM``
    card is recomputed whenever a file is written, so two copies of one frame
    routinely differ in that single 80-byte card while their data is identical.
    """
    try:
        value = _read_fits_header_keys(str(path), ["DATASUM"]).get("DATASUM", "")
    except Exception:
        logger.warning("cannot read DATASUM from %s", path, exc_info=True)
        return None
    value = str(value).strip().strip("'\"").strip()
    return value or None


def dedupe_conflicts(plan: Plan, *, dry_run: bool = True) -> DedupeResult:
    """Delete source copies whose destination holds the same frame.

    A ``conflict`` means the frame already sits in its DAY-OBS directory, so the
    copy still in the wrong directory is redundant. Every deletion is gated on a
    ``DATASUM`` match read from both files — never on a sample, a file size or a
    byte comparison. Anything that cannot be positively verified is kept.
    """
    deleted: list[Path] = []
    kept: list[tuple[Path, str]] = []

    for issue in plan.issues:
        if issue.reason != "conflict" or issue.src is None or issue.dst is None:
            continue
        src, dst = issue.src, issue.dst
        if not src.is_file():
            kept.append((src, "source missing"))
            continue
        if not dst.is_file():
            kept.append((src, "destination missing"))
            continue
        src_sum, dst_sum = _datasum(src), _datasum(dst)
        if src_sum is None or dst_sum is None:
            kept.append((src, "DATASUM unavailable"))
            continue
        if src_sum != dst_sum:
            kept.append((src, "DATASUM differs"))
            continue
        if dry_run:
            deleted.append(src)
            continue
        try:
            src.unlink()
        except OSError as e:
            logger.warning("cannot delete %s: %s", src, e)
            kept.append((src, f"delete failed: {e}"))
            continue
        deleted.append(src)

    return DedupeResult(tuple(deleted), tuple(kept))


def apply_plan(plan: Plan) -> ApplyResult:
    """Perform ``plan``'s moves, then clear the obslog CSVs it invalidated.

    Removing those CSVs is not optional. ``scan_date`` overwrites a CSV only for
    CCDs that produced rows and never deletes one, returning early when a
    directory holds no FITS at all. Leave a stale CSV behind and ``build-db``
    re-ingests the pre-move split — the frames moved, but the database looks
    untouched.
    """
    moved = 0
    removed_csvs: list[Path] = []
    removed_dirs: list[Path] = []
    src_dirs: set[Path] = set()

    for move in plan.moves:
        # Re-check rather than trust the plan: it was built earlier, and
        # clobbering a frame is unrecoverable.
        if move.dst.exists():
            logger.warning("skipping %s: destination %s appeared", move.src, move.dst)
            continue
        if not move.src.is_file():
            logger.warning("skipping %s: source vanished", move.src)
            continue
        move.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.src), str(move.dst))
        src_dirs.add(move.src.parent)
        moved += 1

    touched = plan.touched_dates
    for obsdate in touched:
        for csv_path in _stale_obslog_csvs(plan.obslog_root, plan.instrument, obsdate):
            try:
                csv_path.unlink()
            except OSError as e:
                logger.warning("cannot remove stale obslog %s: %s", csv_path, e)
                continue
            removed_csvs.append(csv_path)

    # An emptied raw directory would otherwise stay in scan-missing's work list
    # forever, rescanned on every pass and producing nothing. rmdir refuses on a
    # non-empty directory, so this cannot discard anything.
    for directory in sorted(src_dirs):
        try:
            directory.rmdir()
        except OSError:
            continue
        removed_dirs.append(directory)

    return ApplyResult(moved, touched, tuple(removed_csvs), tuple(removed_dirs))
