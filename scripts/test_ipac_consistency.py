#!/usr/bin/env python3
"""
Consistency verification test for transit predictions.

This script verifies that the local /lco/schedule predictions are consistent
with the IPAC Exoplanet Archive Transit API.

Usage:
    python test_ipac_consistency.py [--planet WASP-12b] [--start 2026-07-01] [--end 2026-07-10]

Requirements:
    - requests library: pip install requests
"""

import requests
import sys
from datetime import datetime, timezone
from typing import List, Tuple, Optional

# Configuration
DEFAULT_PLANET = "WASP-12b"
DEFAULT_START = "2026-07-01"
DEFAULT_END = "2026-07-10"
LOCAL_API_TIMEOUT = 10
IPAC_API_TIMEOUT = 30
MAX_ACCEPTABLE_DIFF_MINUTES = 1.0  # Threshold for consistency


def calendar_date_to_jd(date_str: str) -> float:
    """Convert calendar date (YYYY-MM-DD) to Julian Date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    a = (14 - dt.month) // 12
    y = dt.year + 4800 - a
    m = dt.month + 12 * a - 3
    jdn = dt.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd = jdn - 0.5
    return float(jd)


def jd_to_datetime(jd: float) -> datetime:
    """Convert Julian Date to datetime in UTC."""
    unix_seconds = (jd - 2440587.5) * 86400
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)


def fetch_local_windows(planet: str, start_date: str, end_date: str) -> Optional[List[dict]]:
    """Fetch transit windows from local /lco/schedule endpoint."""
    print(f"\n{'='*70}")
    print("Step 1: Fetch ephemeris and generate windows (LOCAL API)")
    print(f"{'='*70}")

    # Parse planet name
    target = planet.rsplit(' ', 1)[0] if ' ' in planet else planet
    planet_letter = planet.split()[-1] if ' ' in planet else 'b'

    print(f"Target: {target}")
    print(f"Planet: {planet_letter}")

    # Fetch ephemeris from catalog
    try:
        resp = requests.get(
            "http://localhost:8000/api/ephemeris/target-info",
            params={"target": target},
            timeout=LOCAL_API_TIMEOUT
        )
        if resp.status_code != 200:
            print(f"❌ Failed to fetch ephemeris: HTTP {resp.status_code}")
            return None

        ephem_data = resp.json()
        if not ephem_data.get("ok"):
            print(f"❌ Error: {ephem_data.get('error')}")
            return None

        # Get catalog ephemeris
        catalog_ephem = ephem_data.get("catalog_ephem", {})
        if planet_letter not in catalog_ephem:
            print(f"❌ Planet {planet_letter} not found in catalog")
            return None

        t0 = catalog_ephem[planet_letter].get("t0")
        period = catalog_ephem[planet_letter].get("period")
        duration = catalog_ephem[planet_letter].get("duration")

        print("✓ Retrieved ephemeris:")
        print(f"  t0: {t0}")
        print(f"  period: {period}")
        print(f"  duration: {duration}")

        if t0 is None or period is None:
            print("❌ Missing t0 or period")
            return None

    except requests.RequestException as e:
        print(f"❌ Connection error: {e}")
        return None

    # Generate windows
    try:
        payload = {
            "t0": t0,
            "period": period,
            "duration": duration or 3.0,
            "range_start": start_date,
            "range_end": end_date,
            "pad_before_min": 0,
            "pad_after_min": 0
        }

        resp = requests.post(
            "http://localhost:8000/api/lco/windows",
            json=payload,
            timeout=LOCAL_API_TIMEOUT
        )

        if resp.status_code != 200:
            print(f"❌ Failed to generate windows: HTTP {resp.status_code}")
            return None

        windows_data = resp.json()
        if not windows_data.get("ok"):
            print(f"❌ Error: {windows_data.get('error')}")
            return None

        windows = windows_data.get("windows", [])
        print(f"✓ Generated {len(windows)} transit windows")

        if windows:
            print("\n  Sample windows:")
            for i, w in enumerate(windows[:3]):
                print(f"    {i}: epoch={w['epoch']}, mid={w['mid']}")

        return windows

    except requests.RequestException as e:
        print(f"❌ Connection error: {e}")
        return None


def fetch_ipac_transits(planet: str, start_date: str, end_date: str) -> Optional[List[Tuple[datetime, float]]]:
    """Fetch transit predictions from IPAC Exoplanet Archive."""
    print(f"\n{'='*70}")
    print("Step 2: Fetch predictions from IPAC API")
    print(f"{'='*70}")

    base_url = "https://exoplanetarchive.ipac.caltech.edu/cgi-bin/TransitSearch/nph-transits-api"

    jd_start = calendar_date_to_jd(start_date)
    jd_end = calendar_date_to_jd(end_date) + 1.0

    params = {
        "sname": planet,
        "begin": jd_start,
        "end": jd_end,
        "format": "json"
    }

    print(f"Query: {planet}")
    print(f"JD range: {jd_start:.2f} to {jd_end:.2f}")

    try:
        resp = requests.get(base_url, params=params, timeout=IPAC_API_TIMEOUT)

        if resp.status_code != 200:
            print(f"❌ HTTP {resp.status_code}")
            return None

        data = resp.json()

        if data.get("stat") != "OK":
            print(f"❌ IPAC error: {data.get('msg', 'Unknown error')}")
            return None

        transits = data.get("data", [])
        print(f"✓ Retrieved {len(transits)} IPAC records")

        # Extract unique midpoint times
        ipac_mids = []
        for t in transits:
            mid_jd = t.get("midpointjd")
            if mid_jd:
                try:
                    jd_val = float(mid_jd)
                    dt = jd_to_datetime(jd_val)
                    ipac_mids.append((dt, jd_val))
                except:
                    pass

        # Sort and deduplicate (some IPAC entries may be nearly identical)
        ipac_mids.sort(key=lambda x: x[1])

        print(f"✓ Extracted {len(ipac_mids)} midpoint times")

        if ipac_mids:
            print("\n  Sample IPAC transits:")
            for i, (dt, jd) in enumerate(ipac_mids[:3]):
                print(f"    {i}: {dt.isoformat()}")

        return ipac_mids

    except requests.RequestException as e:
        print(f"❌ Connection error: {e}")
        return None


def compare_predictions(local_windows: List[dict], ipac_mids: List[Tuple[datetime, float]]) -> bool:
    """Compare local predictions with IPAC predictions."""
    print(f"\n{'='*70}")
    print("Step 3: COMPARISON")
    print(f"{'='*70}")

    if not local_windows:
        print("❌ No local windows to compare")
        return False

    if not ipac_mids:
        print("❌ No IPAC transits to compare")
        return False

    print(f"Local windows: {len(local_windows)}")
    print(f"IPAC transits: {len(ipac_mids)}")

    # For each local window, find the closest IPAC transit
    diffs = []
    mismatches = []

    print(f"\n{'#':<3} {'Local Time':<26} {'IPAC Time':<26} {'Diff (min)':<12} {'Status':<12}")
    print("-" * 80)

    for i, local_window in enumerate(local_windows):
        local_dt = datetime.fromisoformat(local_window['mid'].replace("Z", "+00:00"))

        # Find closest IPAC transit
        closest_ipac_dt = None
        closest_diff_minutes = float('inf')

        for ipac_dt, ipac_jd in ipac_mids:
            diff_minutes = abs((local_dt - ipac_dt).total_seconds() / 60)
            if diff_minutes < closest_diff_minutes:
                closest_diff_minutes = diff_minutes
                closest_ipac_dt = ipac_dt

        diffs.append(closest_diff_minutes)

        if closest_diff_minutes > MAX_ACCEPTABLE_DIFF_MINUTES:
            status = "❌ FAIL"
            mismatches.append(i)
        elif closest_diff_minutes > 0.5:
            status = "⚠️  WARN"
        else:
            status = "✅ OK"

        print(f"{i:<3} {local_dt.isoformat():<26} {closest_ipac_dt.isoformat():<26} {closest_diff_minutes:>10.2f} {status:<12}")

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")

    max_diff = max(diffs) if diffs else 0
    avg_diff = sum(diffs) / len(diffs) if diffs else 0

    print(f"Maximum difference: {max_diff:.2f} minutes")
    print(f"Average difference: {avg_diff:.2f} minutes")
    print(f"Threshold:          {MAX_ACCEPTABLE_DIFF_MINUTES:.2f} minutes")

    if not mismatches:
        print(f"\n✅ CONSISTENT: All transits match IPAC to within {MAX_ACCEPTABLE_DIFF_MINUTES} minute(s)")
        return True
    else:
        print(f"\n❌ INCONSISTENT: {len(mismatches)} transit(s) differ by more than {MAX_ACCEPTABLE_DIFF_MINUTES} minute(s)")
        print(f"   Affected transits: {mismatches}")
        return False


def main():
    """Main test function."""
    import argparse

    parser = argparse.ArgumentParser(description="Test transit prediction consistency")
    parser.add_argument("--planet", default=DEFAULT_PLANET, help=f"Planet to test (default: {DEFAULT_PLANET})")
    parser.add_argument("--start", default=DEFAULT_START, help=f"Start date YYYY-MM-DD (default: {DEFAULT_START})")
    parser.add_argument("--end", default=DEFAULT_END, help=f"End date YYYY-MM-DD (default: {DEFAULT_END})")

    args = parser.parse_args()

    print(f"\n{'='*70}")
    print("TRANSIT PREDICTION CONSISTENCY TEST")
    print(f"{'='*70}")
    print(f"Planet: {args.planet}")
    print(f"Date range: {args.start} to {args.end}")

    # Fetch data
    local_windows = fetch_local_windows(args.planet, args.start, args.end)
    ipac_mids = fetch_ipac_transits(args.planet, args.start, args.end)

    # Compare
    if local_windows and ipac_mids:
        result = compare_predictions(local_windows, ipac_mids)
        return 0 if result else 1
    else:
        print("\n❌ Could not obtain data from both sources")
        return 1


if __name__ == "__main__":
    sys.exit(main())
