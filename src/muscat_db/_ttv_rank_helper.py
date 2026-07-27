"""Rank predicted transits by information gain, in the harmonic conda env.

Executed as a script by :mod:`muscat_db.ttv_fit`, mirroring
``_ttv_model_helper.py``. Answers "which of these transits is worth observing",
where the gain is the entropy reduction in the TTV parameters from measuring one
more mid-time at an assumed precision.

``Harmonic.predict`` also produces this table, but it always writes a
``predict-<timestamp>.png`` into the run directory as a side effect, so the two
underlying functions are called directly instead: reading a saved run should not
litter it.
"""
from __future__ import annotations

import argparse
import configparser
import json
import math
from pathlib import Path

from astropy.time import Time

from harmonic.fisher import rank_transits
from harmonic.harmonic import Harmonic
from harmonic.predict import scan_transits

_RANK_COLS = ("sigma", "gain_total", "gain_ttv", "greedy_rank", "greedy_gain")


def _planet_key(key: str) -> str:
    """Planet letter for one ``[T14]`` entry, tolerating the historical spelling.

    ``scan_transits`` indexes its duration mapping by bare planet letter, which
    is now what the ephemeris page writes. Runs created before that carry
    ``t14_b = ...`` in their saved ``config.ini``, and those files are read in
    place rather than rewritten, so the prefix is still accepted here. Drop this
    only once no run directory on disk uses the old form.
    """
    key = key.strip().lower()
    return key[4:] if key.startswith("t14_") else key


def evaluate(run_dir: Path, window: list[str], rank_by: str) -> dict:
    config = json.loads((run_dir / "fit_config.json").read_text())
    ttv = Harmonic(
        fp_data=str(run_dir / "data.csv"),
        fp_config=str(run_dir / "config.ini"),
        letters=str(config.get("letters") or "bcdefghijk"),
        outdir=str(run_dir),
        non_transiting_outer=bool(config.get("non_transiting_outer", False)),
        phase_offsets=bool(config.get("phase_offsets", False)),
    )
    ttv._require_chain()

    parser = configparser.ConfigParser()
    parser.read(run_dir / "config.ini")
    if "T14" not in parser:
        raise ValueError("config.ini has no [T14] section, required to predict transits")
    t14s = {_planet_key(key): float(value) for key, value in parser["T14"].items()}

    t_offset = float(config.get("t_offset") or 0.0)
    transits = scan_transits(
        ttv.flatchain,
        ttv.ephem,
        ttv.planet_letters,
        ttv.non_transiting_outer,
        t14s,
        [Time(w) for w in window],
        ttv.phase_offsets,
        t_offset=t_offset,
        t_ref=ttv.spec.t_ref,
    )

    # Assumed precision of a future measurement, per planet. harmonic's own
    # default is this target's median timing uncertainty, which reflects what
    # these instruments actually achieve on this star, so it is used unchanged.
    letters = (
        ttv.planet_letters[:-1] if ttv.non_transiting_outer else ttv.planet_letters
    )
    sigmas = {
        p: float(ttv.times[ttv.times.planet == p].tc_unc.median()) for p in letters
    }

    ranked = rank_transits(
        ttv.flatchain,
        list(ttv.spec.names),
        transits,
        ttv.planet_letters,
        ttv.non_transiting_outer,
        ttv.phase_offsets,
        sigmas,
        ttv.spec.t_ref,
        rank_by=rank_by,
    )

    rows = []
    for row in ranked.to_dict("records"):
        entry = {"planet": str(row.get("planet", "")), "epoch": int(row.get("epoch", 0))}
        for col in _RANK_COLS:
            value = row.get(col)
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                entry[col] = None
            else:
                entry[col] = int(value) if col == "greedy_rank" else float(value)
        rows.append(entry)
    return {"rows": rows, "rank_by": rank_by, "sigmas": sigmas}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--start", required=True, help="window start, ISO UTC")
    parser.add_argument("--end", required=True, help="window end, ISO UTC")
    parser.add_argument("--rank-by", default="total", choices=["total", "ttv"])
    args = parser.parse_args()
    print(json.dumps(
        evaluate(args.run_dir, [args.start, args.end], args.rank_by),
        separators=(",", ":"),
    ))


if __name__ == "__main__":
    main()
