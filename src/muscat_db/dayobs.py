"""Parse the LCO DAY-OBS token embedded in archive frame filenames.

LCO stamps the observing night into every archive filename as
``<site><telescope>-<camera>-<YYYYMMDD>-<sequence>-<product>``, e.g.
``lsc1m004-fa03-20240417-0092-e91.fits``. That token is the UTC date at the
*start* of the night, so it stays constant across local midnight — unlike
``DATE_OBS``, which is the frame's actual UTC timestamp and therefore rolls over
mid-night at sites where the night straddles 00:00 UTC.

Deriving the obsdate from this token is what keeps a single night in a single
directory. See the "UTC-midnight dataset split" section of README.md.

MuSCAT and MuSCAT2 filenames carry no such token (their directory naming is
already observing-night based), so this returns ``None`` for them.
"""

from __future__ import annotations

import datetime
import re

# The sequence field is also numeric, so anchor on 8 digits between dashes and
# validate that the match is a real calendar date before accepting it.
_DAYOBS_RE = re.compile(r"-(\d{8})(?=-)")


def dayobs_from_filename(filename: str) -> str | None:
    """Return the YYMMDD DAY-OBS token from an LCO filename, or ``None``.

    ``None`` means "this filename does not carry a DAY-OBS token" — a
    non-LCO frame, or a hand-copied file with a non-standard name. Callers must
    treat that as "cannot judge", never as "belongs here".
    """
    if not filename:
        return None
    for match in _DAYOBS_RE.finditer(filename):
        token = match.group(1)
        try:
            datetime.datetime.strptime(token, "%Y%m%d")
        except ValueError:
            continue
        return token[2:]
    return None
