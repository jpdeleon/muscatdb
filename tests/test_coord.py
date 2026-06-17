"""Tests for robust coordinate selection (muscat_db.coord)."""
import sqlite3

import pytest

from muscat_db.coord import (
    CoordRepr,
    clean_dec,
    is_valid_dec,
    is_valid_ra,
    pick_representative,
    unpack,
)


# ── format validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "04:05:35.0521",   # full precision, leading zero hour
    "4:06:46",         # legacy single-digit hour, integer seconds
    "4:22:55",
])
def test_valid_ra_accepts_well_formed(value):
    assert is_valid_ra(value)


@pytest.mark.parametrize("value", [
    "+20:11:30.90",    # decimal seconds
    "+20:12:48",       # integer seconds
    "-04:05:06",       # negative
])
def test_valid_dec_accepts_well_formed(value):
    assert is_valid_dec(value)


@pytest.mark.parametrize("value", [
    "q", "OQ", "U f", "} |", "y&", "X1M", "",     # corrupt header garbage
    "+20:16:251",      # 3-digit seconds (TCS dropped the decimal point)
    "+20:14:071",
    "+20:16:60",       # seconds out of range
    "+20:60:00",       # minutes out of range
    None,
])
def test_valid_dec_rejects_malformed(value):
    assert not is_valid_dec(value)


# ── dropped-decimal recovery (muscat2 TCS bug) ───────────────────────────────

@pytest.mark.parametrize("raw,recovered", [
    ("+20:16:251", "+20:16:25.1"),
    ("+20:14:071", "+20:14:07.1"),
    ("+20:11:121", "+20:11:12.1"),
    ("+20:13:161", "+20:13:16.1"),
])
def test_clean_dec_recovers_dropped_decimal(raw, recovered):
    assert clean_dec(raw) == recovered
    # strict validation still rejects the raw form
    assert not is_valid_dec(raw)


@pytest.mark.parametrize("value", ["q", "OQ", "} |", "+20:60:001", "+20:16:601"])
def test_clean_dec_rejects_unrecoverable(value):
    # garbage, and bad recoveries where minutes/seconds would exceed 59
    assert clean_dec(value) is None


def test_pick_representative_recovers_all_dropped_decimal_group():
    pairs = [("4:06:37", "+20:11:121"), ("4:06:37", "+20:11:131"),
             ("4:06:37", "+20:11:141"), ("4:09:41", "OQ")]
    ra, dec = pick_representative(pairs)
    assert ra == "4:06:37"
    assert dec == "+20:11:13.1"  # median of the recovered values


# ── representative selection ─────────────────────────────────────────────────

def test_pick_representative_ignores_garbage_and_pairs_from_same_frame():
    pairs = [
        ("4:22:55", "q"),                 # rogue: would win a string MAX
        ("04:05:35.05", "+20:04:27.36"),  # good
        ("04:05:35.07", "+20:04:27.33"),  # good (median dec)
        ("04:05:35.09", "+20:04:27.31"),  # good
        ("4:09:41", "OQ"),                # rogue
    ]
    ra, dec = pick_representative(pairs)
    # Must be a well-formed pair, never the lexicographic garbage.
    assert is_valid_ra(ra) and is_valid_dec(dec)
    assert (ra, dec) == ("04:05:35.07", "+20:04:27.33")


def test_pick_representative_returns_empty_when_all_malformed():
    assert pick_representative([("x", "q"), ("", ""), (None, None)]) == ("", "")


def test_pick_representative_keeps_ra_dec_from_one_frame():
    # RA ascending while Dec descending: independent max/median would mismatch.
    pairs = [
        ("04:00:01", "+20:00:03"),
        ("04:00:02", "+20:00:02"),  # median dec -> this exact pair
        ("04:00:03", "+20:00:01"),
    ]
    assert pick_representative(pairs) == ("04:00:02", "+20:00:02")


# ── SQLite aggregate end-to-end ──────────────────────────────────────────────

def test_coord_repr_aggregate_matches_v1298tau_pattern():
    conn = sqlite3.connect(":memory:")
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.execute("CREATE TABLE f (obj TEXT, ra TEXT, dec TEXT)")
    conn.executemany(
        "INSERT INTO f VALUES (?,?,?)",
        [
            ("V1298Tau", "4:22:55", "q"),                 # 1 rogue frame
            ("V1298Tau", "04:05:35.05", "+20:04:27.36"),
            ("V1298Tau", "04:05:35.07", "+20:04:27.33"),
            ("V1298Tau", "04:05:35.09", "+20:04:27.31"),
        ],
    )
    (packed,) = conn.execute(
        "SELECT coord_repr(ra, dec) FROM f WHERE obj='V1298Tau'"
    ).fetchone()
    ra, dec = unpack(packed)
    assert dec != "q"
    assert is_valid_ra(ra) and is_valid_dec(dec)
    conn.close()


def test_coord_repr_empty_group_returns_blank_pair():
    conn = sqlite3.connect(":memory:")
    conn.create_aggregate("coord_repr", 2, CoordRepr)
    conn.execute("CREATE TABLE f (ra TEXT, dec TEXT)")
    conn.execute("INSERT INTO f VALUES ('q', 'OQ')")
    (packed,) = conn.execute("SELECT coord_repr(ra, dec) FROM f").fetchone()
    assert unpack(packed) == ("", "")
    conn.close()
