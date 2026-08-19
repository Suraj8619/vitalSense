"""Download BIDMC PPG records from PhysioNet into data/bidmc.

The BIDMC PPG and Respiration dataset is 53 eight-minute recordings from adult ICU
patients: a photoplethysmogram at 125 Hz alongside per-second reference numerics (heart
rate, pulse rate, respiration, SpO2) from the bedside monitor.

What it can and cannot validate is worth stating before downloading it. `PLETH` is a
**single** channel - one wavelength, already processed by the monitor's own front end.
Ratio-of-ratios needs two, so this dataset cannot check the saturation figure at all.
What it does provide is real clinical PPG from real patients, with a reference pulse
rate, which is exactly what is needed to check that pulse detection survives contact
with reality - including the dicrotic-notch failure that doubled the rate on synthetic
data (D-33).

Run: python dsp/fetch_ppg.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import wfdb

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "bidmc"

#: A spread across the cohort rather than the first N, chosen before any results were
#: seen - the same discipline as the MIT-BIH selection in fetch_data.py.
DEFAULT_RECORDS = [
    "bidmc01", "bidmc05", "bidmc11", "bidmc16", "bidmc22",
    "bidmc29", "bidmc35", "bidmc41", "bidmc47", "bidmc53",
]


def fetch(records: list[str], dest: Path = DATA_DIR) -> Path:
    """Download the waveform and numerics files for each record."""
    dest.mkdir(parents=True, exist_ok=True)
    for record in records:
        # Each record has a waveform file and a matching `n` numerics file.
        for name in (record, f"{record}n"):
            if (dest / f"{name}.hea").exists():
                print(f"  {name}: already present")
                continue
            print(f"  {name}: downloading...")
            wfdb.dl_files("bidmc", str(dest), [f"{name}.hea", f"{name}.dat"])
    return dest


def main(argv: list[str] | None = None) -> int:
    records = (argv or sys.argv[1:]) or DEFAULT_RECORDS
    print(f"Fetching {len(records)} BIDMC records into {DATA_DIR.relative_to(ROOT)}")
    fetch(records)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
