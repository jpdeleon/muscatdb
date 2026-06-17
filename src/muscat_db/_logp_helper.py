"""Standalone helper to compute a ``timer`` TransitFit model's log-probability.

This runs INSIDE the ``timer`` conda env (which provides ``pymc`` and the
``timer`` package); it is *not* imported by the web app. The web app invokes it
as a subprocess::

    <timer-env-python> _logp_helper.py <work_dir>

``<work_dir>`` must contain ``fit.yaml``, ``sys.yaml`` and the light-curve CSVs
(exactly what ``transit_fit.start_fit`` prepares). On success it prints a single
line::

    logP= <value>

evaluating ``model.logp()`` at the model's initial (prior) point — i.e. the
log-probability given the parameters entered in the form. The expensive MAP
optimization performed by ``build_model`` is neutralized, since it is not needed
to evaluate the log-probability at the priors. On failure it prints
``logP error: <message>`` and exits non-zero.
"""
from __future__ import annotations

import logging
import sys
import warnings


def compute_logp(work_dir: str) -> float:
    """Build the model from ``work_dir`` and return logp at the prior point."""
    import pymc as pm
    from timer import optim
    from timer.fit import TransitFit

    # We only need the model's log-probability at the entered priors, not the
    # MAP solution. Replace the optimizers with identities that return the start
    # point unchanged so model construction stays cheap (seconds, not minutes).
    def _identity_map(*args, start=None, model=None, **kwargs):
        ctx = pm.modelcontext(model)
        return dict(start) if start is not None else ctx.initial_point()

    pm.find_MAP = _identity_map
    optim.optimize = lambda start=None, model=None, **kwargs: (
        dict(start) if start is not None else pm.modelcontext(model).initial_point()
    )

    tf = TransitFit.from_dir(work_dir)
    tf.build_model(verbose=False, plot=False)
    model = tf.model
    return float(model.compile_logp()(model.initial_point()))


def main(argv: list[str]) -> int:
    logging.disable(logging.CRITICAL)
    warnings.filterwarnings("ignore")

    if len(argv) != 2:
        print("logP error: usage: _logp_helper.py <work_dir>")
        return 2

    value = compute_logp(argv[1])
    # .2f with thousands separators, e.g. logP= -1,234.57
    print(f"logP= {value:,.2f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # surface any failure to the calling process
        print(f"logP error: {exc}")
        sys.exit(1)
