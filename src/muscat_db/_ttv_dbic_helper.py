"""Compute a saved harmonic run's ΔBIC in its dedicated conda environment.

Executed as a script by :mod:`muscat_db.ttv_fit`, mirroring
``_ttv_model_helper.py``. harmonic writes ``fit_stats.json`` only when it
actually samples, so a run that was resumed without ``--clobber`` -- or that
predates the feature -- has no stored ΔBIC. ``Harmonic.delta_bic()`` recovers it
by re-running the deterministic least-squares optimum (no MCMC), which is why
this is cheap enough to do on demand.

Nothing is written back into the run directory: a ΔBIC we recomputed is not the
same artefact harmonic's own fit persists (it lacks the sampler diagnostics), and
writing a partial ``fit_stats.json`` would make harmonic treat our file as
authoritative on the next read.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from harmonic.harmonic import Harmonic


def evaluate(run_dir: Path) -> dict:
    config = json.loads((run_dir / "fit_config.json").read_text())
    ttv = Harmonic(
        fp_data=str(run_dir / "data.csv"),
        fp_config=str(run_dir / "config.ini"),
        letters=str(config.get("letters") or "bcdefghijk"),
        outdir=str(run_dir),
        non_transiting_outer=bool(config.get("non_transiting_outer", False)),
        phase_offsets=bool(config.get("phase_offsets", False)),
    )
    stats = ttv.delta_bic()
    # Non-finite values are not valid JSON and would break the browser's parse of
    # the whole response, so drop them here as well as on the reading side.
    return {
        key: (None if isinstance(value, float) and not math.isfinite(value) else value)
        for key, value in dict(stats).items()
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.run_dir), separators=(",", ":")))


if __name__ == "__main__":
    main()
