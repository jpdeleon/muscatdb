
import sqlite3
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
DB_PATH = PROJECT_ROOT / "muscat.db"

def get_test_frames(target_name, instruments):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = """
    SELECT
        instrument,
        obsdate,
        filename,
        object,
        exptime,
        filter,
        airmass,
        focus,
        (SELECT COUNT(*) FROM frames f2 WHERE f2.object = f.object AND f2.obsdate = f.obsdate AND f2.instrument = f.instrument) as frame_count
    FROM frames f
    WHERE
        object LIKE ?
        AND instrument IN ({})
    GROUP BY object, obsdate, instrument
    HAVING frame_count < 200
    ORDER BY obsdate DESC
    """.format(','.join('?'*len(instruments)))

    params = [f'%{target_name}%'] + instruments
    cursor.execute(query, params)

    frames = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return frames

if __name__ == "__main__":
    target = "TOI6109"
    instruments = ["sinistro", "muscat3"]
    test_frames = get_test_frames(target, instruments)
    print(json.dumps(test_frames, indent=4))
