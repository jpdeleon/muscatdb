"""Evaluate a saved harmonic run in its dedicated conda environment.

This file is executed as a script by :mod:`muscat_db.ttv_fit`.  Keeping the
actual TTV evaluation in ``harmonic.model`` avoids maintaining a second copy
of the scientific model in the web application.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from harmonic.harmonic import Harmonic
from harmonic.model import model


def _best_sample(ttv: Harmonic) -> tuple[dict[str, float], float]:
    """Return the posterior row with minimum chi-square on the fitted data."""
    ttv._require_chain()
    planets = np.asarray(ttv.times.planet)
    epochs = np.asarray(ttv.times.epoch, dtype=float)
    observed = np.asarray(ttv.times.tc, dtype=float)
    uncertainty = np.asarray(ttv.times.tc_unc, dtype=float)

    best_params: dict[str, float] | None = None
    best_chi2 = math.inf
    for values in ttv.flatchain.itertuples(index=False, name=None):
        params = dict(zip(ttv.flatchain.columns, values))
        predicted = model(
            params,
            planets,
            epochs,
            ttv.planet_letters,
            ttv.non_transiting_outer,
            ttv.phase_offsets,
            t_ref=ttv.spec.t_ref,
        )
        chi2 = float(np.sum(((observed - predicted) / uncertainty) ** 2))
        if math.isfinite(chi2) and chi2 < best_chi2:
            best_params = params
            best_chi2 = chi2

    if best_params is None:
        raise ValueError("saved posterior contains no finite model sample")
    return best_params, best_chi2


def evaluate(run_dir: Path, end_bjd: float | None) -> dict:
    config_path = run_dir / "fit_config.json"
    config = json.loads(config_path.read_text())
    letters = str(config.get("letters") or "bcdefghijk")
    ttv = Harmonic(
        fp_data=str(run_dir / "data.csv"),
        fp_config=str(run_dir / "config.ini"),
        letters=letters,
        outdir=str(run_dir),
        non_transiting_outer=bool(config.get("non_transiting_outer", False)),
        phase_offsets=bool(config.get("phase_offsets", False)),
    )
    params, chi2 = _best_sample(ttv)

    transiting = (
        ttv.planet_letters[:-1]
        if ttv.non_transiting_outer
        else ttv.planet_letters
    )
    points: dict[str, list[dict[str, float | int]]] = {}
    total_points = 0
    for planet in transiting:
        observed_epochs = np.asarray(
            ttv.times.loc[ttv.times.planet == planet, "epoch"], dtype=int
        )
        if observed_epochs.size == 0:
            continue
        epoch_min = int(observed_epochs.min())
        epoch_max = int(observed_epochs.max())
        if end_bjd is not None:
            period = float(params[f"per_{planet}"])
            t0 = float(params[f"t0_{planet}"])
            epoch_max = max(epoch_max, int(math.ceil((end_bjd - t0) / period)) + 1)
        count = epoch_max - epoch_min + 1
        if count <= 0 or total_points + count > 50_000:
            raise ValueError("requested model date produces too many transit epochs")
        epochs = np.arange(epoch_min, epoch_max + 1, dtype=float)
        planets = np.full(epochs.size, planet)
        transit_centers = model(
            params,
            planets,
            epochs,
            ttv.planet_letters,
            ttv.non_transiting_outer,
            ttv.phase_offsets,
            t_ref=ttv.spec.t_ref,
        )
        points[planet] = [
            {"epoch": int(epoch), "tc": float(tc)}
            for epoch, tc in zip(epochs, transit_centers)
            if math.isfinite(float(tc))
        ]
        total_points += count

    return {
        "points": points,
        "chi2": chi2,
        "sample_count": int(len(ttv.flatchain)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--end-bjd", type=float)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.run_dir, args.end_bjd), separators=(",", ":")))


if __name__ == "__main__":
    main()
