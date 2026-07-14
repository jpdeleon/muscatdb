import pandas as pd
import sys
from pathlib import Path

# Adjust sys.path to ensure muscat_db is importable
# Assuming the script is run from the project root or its immediate subdirectories
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from muscat_db.database import get_targets, db_path

# Define the path to the output CSV relative to the project root
# Assuming the script is placed in `scripts/` and needs to write to `data/`
output_csv_path = Path(__file__).resolve().parents[1] / "data" / "muscatdb_targets.csv"

def update_muscatdb_targets_csv():
    try:
        # Get the path to the muscat.db
        db_file_path = db_path()
        print(f"Using database: {db_file_path}")

        # Retrieve target data using the project's utility function
        targets_data = get_targets(db_file_path)

        if not targets_data:
            print("No target data found in the database. CSV will be empty.")
            df = pd.DataFrame()
        else:
            # Convert to DataFrame
            df = pd.DataFrame(targets_data)
            
            # These are the columns from the SCHEMA definition that are simple TEXT/REAL/INTEGER
            # and relevant for a basic targets export. `get_targets` returns a dict with more keys
            # so we select the ones we want to save.
            columns_to_export = [
                'object', 'n_dates', 'n_frames',
                'ra', 'declination', 'airmass_min', 'airmass_max',
                'is_identified', 'phot_status', 'fit_status'
            ]
            
            # Ensure all columns are present, fill missing with None/NaN if get_targets returns a different dict structure
            for col in columns_to_export:
                if col not in df.columns:
                    df[col] = None
            
            df = df[columns_to_export]


        # Save to CSV
        df.to_csv(output_csv_path, index=False)
        print(f"Successfully updated {output_csv_path} with {len(df)} rows.")

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    update_muscatdb_targets_csv()
