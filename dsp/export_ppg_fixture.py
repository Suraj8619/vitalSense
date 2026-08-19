"""Export PPG test cases and the Python results, for the JavaScript port to be scored against.

The JS implementation in `server/src/dsp/ppg.js` is a port, and a port is a claim until
someone measures it (D-13). This writes the inputs *and* the reference outputs so the
comparison happens on identical data rather than on two independently generated signals
that merely look alike.

Two kinds of case:

* **synthetic** - full comparison, since saturation ground truth exists;
* **bidmc** - real ICU photoplethysmograms, single channel, so only the pulse period can
  be compared. That is still the half where real recordings found two bugs.

Run: python dsp/export_ppg_fixture.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dsp"))

from ppg import PpgConfig, _pulsatile, estimate_spo2, periodicity, synthetic_ppg  # noqa: E402

OUT = ROOT / "data" / "fixtures" / "ppg.json"

#: Chosen to span what the estimator must handle *and* what it must refuse: normal and
#: desaturated, well and poorly perfused, slow and fast, clean and corrupted.
CASES: tuple[tuple[str, PpgConfig], ...] = (
    ("normal", PpgConfig(duration_s=20, spo2_pct=97, pulse_bpm=72, seed=1)),
    ("mild-desat", PpgConfig(duration_s=20, spo2_pct=92, pulse_bpm=88, seed=2)),
    ("severe-desat", PpgConfig(duration_s=20, spo2_pct=85, pulse_bpm=110, seed=3)),
    ("full-sat", PpgConfig(duration_s=20, spo2_pct=100, pulse_bpm=60, seed=4)),
    ("bradycardia", PpgConfig(duration_s=20, spo2_pct=96, pulse_bpm=45, seed=5)),
    ("tachycardia", PpgConfig(duration_s=20, spo2_pct=95, pulse_bpm=165, seed=6)),
    ("low-perfusion", PpgConfig(duration_s=20, spo2_pct=96, perfusion_pct=0.4, seed=7)),
    ("very-low-perfusion", PpgConfig(duration_s=20, spo2_pct=96, perfusion_pct=0.05, seed=8)),
    ("noisy", PpgConfig(duration_s=20, spo2_pct=95, noise_frac=0.10, seed=9)),
    ("very-noisy", PpgConfig(duration_s=20, spo2_pct=95, noise_frac=0.80, seed=10)),
    ("motion", PpgConfig(duration_s=20, spo2_pct=95, motion_frac=1.5, seed=11)),
    ("wander", PpgConfig(duration_s=20, spo2_pct=94, wander_frac=0.05, seed=12)),
    ("hrv", PpgConfig(duration_s=20, spo2_pct=97, hrv_frac=0.08, seed=13)),
)

BIDMC_RECORDS = ("bidmc01", "bidmc29", "bidmc41")
BIDMC_WINDOW_S = 30.0
BIDMC_WINDOWS = 4


def _result_dict(res) -> dict:
    return {
        "spo2Pct": res.spo2_pct,
        "ratio": res.ratio,
        "pulseBpm": res.pulse_bpm,
        "perfusionPct": res.perfusion_pct,
        "nBeats": res.n_beats,
        "quality": res.quality,
        "channelCorr": res.channel_corr,
        "periodicity": res.periodicity,
    }


def build() -> dict:
    cases = []
    for name, cfg in CASES:
        red, ir, _ = synthetic_ppg(cfg)
        cases.append({
            "kind": "synthetic",
            "name": name,
            "fs": cfg.fs,
            "trueSpo2": cfg.spo2_pct,
            "truePulseBpm": cfg.pulse_bpm,
            "red": [round(v, 4) for v in red],
            "ir": [round(v, 4) for v in ir],
            "expected": _result_dict(estimate_spo2(red, ir, cfg.fs)),
        })

    try:
        from validate_ppg import load
    except ImportError:
        load = None

    if load is not None:
        for record in BIDMC_RECORDS:
            try:
                signal, fs, _ = load(record)
            except FileNotFoundError:
                print(f"  {record}: not downloaded, skipping", file=sys.stderr)
                continue
            win = int(BIDMC_WINDOW_S * fs)
            for k in range(BIDMC_WINDOWS):
                start = (k + 2) * win
                segment = signal[start : start + win]
                if segment.size < win or not np.all(np.isfinite(segment)):
                    continue
                period, strength = periodicity(_pulsatile(segment, fs), fs)
                cases.append({
                    "kind": "bidmc",
                    "name": f"{record}-w{k}",
                    "fs": fs,
                    "signal": [round(float(v), 6) for v in segment],
                    "expected": {
                        "periodS": period,
                        "strength": strength,
                        "pulseBpm": (60.0 / period) if period else None,
                    },
                })
    return {"cases": cases}


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    OUT.write_text(json.dumps(data))
    kinds: dict[str, int] = {}
    for c in data["cases"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    size_mb = OUT.stat().st_size / 1e6
    print(f"wrote {OUT.relative_to(ROOT)} - {kinds}, {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
