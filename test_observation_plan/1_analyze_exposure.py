
import sqlite3
import sys
from pathlib import Path
import csv

# Add project root to sys.path to allow imports from muscat_db and scripts
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# Now we can import the necessary functions
from test_exposure_predictions import measure_peak_from_dataset
from muscat_db.database import db_path

# --- Configuration ---
TARGET_OBJECT = "TOI-6109"
INSTRUMENTS = ["sinistro", "muscat3"]
FRAME_COUNT_THRESHOLD = 200
SATURATION_LIMIT = 89100  # 90% of 99000
OUTPUT_CSV = PROJECT_ROOT / "test_observation_plan/exposure_analysis.csv"

def get_test_observations():
    """Fetches test observations from the database."""
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    query = f"""
        SELECT
            instrument, obsdate, filename, filter, exptime,
            (SELECT COUNT(*) FROM frames f2 WHERE f2.object = f.object AND f2.obsdate = f.obsdate AND f2.instrument = f.instrument) as frame_count
        FROM frames f
        WHERE
            object = ? AND
            instrument IN ({','.join('?'*len(INSTRUMENTS))})
        GROUP BY obsdate, instrument, filter, exptime
        HAVING frame_count < ?
        ORDER BY instrument, exptime
    """
    params = [TARGET_OBJECT] + INSTRUMENTS + [FRAME_COUNT_THRESHOLD]
    cursor = conn.execute(query, params)
    frames = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return frames

def analyze_exposures():
    """
    Analyzes test observations to determine safe exposure times.
    """
    print("Starting Exposure Time Analysis...")
    observations = get_test_observations()
    print(f"Found {len(observations)} test observations to analyze.")

    results = []

    for obs in observations:
        instrument = obs["instrument"]
        obsdate = obs["obsdate"]
        # The filename in DB is often just the base.
        # e.g. ogg2m001-ep02-20251030-0269-e91
        # measure_peak_from_dataset handles globbing from this base.
        filename_base = obs["filename"]
        exptime = obs["exptime"]
        filter_name = obs["filter"]
        frame_count = obs["frame_count"]

        print(f"  Analyzing: {instrument} {obsdate} {filter_name} {exptime}s ({frame_count} frames)")

        # This function reads the actual FITS files and measures the peak
        try:
            measured_peak = measure_peak_from_dataset(obsdate, filename_base, instrument)
            if measured_peak is not None:
                saturated = measured_peak >= SATURATION_LIMIT
                status = "OK"
                if saturated:
                    status = "SATURATED"
            else:
                saturated = None
                status = "Measurement Failed"
        except Exception as e:
            print(f"    ERROR: Could not process {filename_base}. Reason: {e}")
            measured_peak = None
            saturated = None
            status = "Processing Error"


        results.append({
            "instrument": instrument,
            "obsdate": obsdate,
            "filter": filter_name,
            "exptime": exptime,
            "measured_peak_adu": f"{measured_peak:.0f}" if measured_peak is not None else "N/A",
            "saturated": saturated,
            "status": status,
        })

    # Save results to CSV
    if results:
        with open(OUTPUT_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        print(f"\nExposure analysis complete. Results saved to {OUTPUT_CSV}")
    else:
        print("\nNo results to save.")

if __name__ == "__main__":
    analyze_exposures()
