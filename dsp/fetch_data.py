"""Download MIT-BIH Arrhythmia Database records from PhysioNet into data/.

The dataset is not committed to the repo (see .gitignore); this script makes the
download reproducible for anyone cloning the project.

Usage:
    python dsp/fetch_data.py            # download the default validation set
    python dsp/fetch_data.py 100 101    # download specific records
"""

import sys
from pathlib import Path

import wfdb

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "mitdb"

# Chosen deliberately to span signal quality, so verification results are honest
# rather than cherry-picked:
#   clean, textbook morphology ....... 100, 101, 103, 115
#   abnormal beats / arrhythmia ...... 106, 208, 119
#   noisy, hard for any detector ..... 105, 108, 203
DEFAULT_RECORDS = ["100", "101", "103", "115", "106", "208", "119", "105", "108", "203"]


def fetch(records: list[str], dest: Path = DATA_DIR) -> Path:
    """Download the given MIT-BIH records (signal + annotations) into dest."""
    dest.mkdir(parents=True, exist_ok=True)
    missing = [r for r in records if not (dest / f"{r}.dat").exists()]

    if not missing:
        print(f"All {len(records)} records already present in {dest}")
        return dest

    print(f"Downloading {len(missing)} record(s) from PhysioNet -> {dest}")
    wfdb.dl_database("mitdb", str(dest), records=missing, annotators=["atr"])
    print("Done.")
    return dest


if __name__ == "__main__":
    requested = sys.argv[1:] or DEFAULT_RECORDS
    fetch(requested)
