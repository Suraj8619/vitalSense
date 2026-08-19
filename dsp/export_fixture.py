"""Export MIT-BIH records as JSON fixtures for the JavaScript test suite.

The Node.js streaming detector has to be scored against the same clinical data and
the same annotations as the Python offline detector, otherwise "the streaming port
matches" is an assertion rather than a measurement. Python owns the dataset reader,
so it writes the fixture and JavaScript consumes it.

Fixtures are gitignored - regenerate with:

    python dsp/export_fixture.py 100 105
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from validate import BEAT_SYMBOLS, load_record  # noqa: F401  (BEAT_SYMBOLS documents intent)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "data" / "fixtures"


def export(record: str, minutes: float | None = None) -> Path:
    """Write one record's samples and beat annotations as JSON."""
    signal, beats, fs = load_record(record)
    if minutes is not None:
        limit = int(minutes * 60 * fs)
        signal = signal[:limit]
        beats = beats[beats < limit]

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{record}.json"
    path.write_text(
        json.dumps(
            {
                "record": record,
                "fs": fs,
                # Rounded to 4 decimals: MIT-BIH is digitised at ~5 uV resolution, so
                # further precision is noise and would triple the fixture size.
                "signal": [round(float(v), 4) for v in signal],
                "referenceBeats": [int(b) for b in beats],
            }
        )
    )
    return path


if __name__ == "__main__":
    records = sys.argv[1:] or ["100", "105", "203"]
    for rec in records:
        p = export(rec)
        print(f"wrote {p.relative_to(ROOT)} ({p.stat().st_size / 1e6:.1f} MB)")
