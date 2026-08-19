"""Validate PPG pulse detection against the BIDMC dataset.

The synthetic tests in `dsp/ppg.py` answer "does the algorithm recover what it was
given". This answers the harder question: does it work on eight-minute recordings from
real ICU patients, scored against the bedside monitor's own pulse rate.

**What this can and cannot establish.** BIDMC provides `PLETH` - a *single* channel,
already processed by the monitor's front end. Ratio-of-ratios needs two wavelengths, so
nothing here validates the saturation figure; that half of SR-06 stays unverified and
`docs/requirements.md` says so (D-34). What is validated is the pulse-detection front
end: period estimation, sub-harmonic rejection, and the decision to decline on signals
that are not periodic.

Run: python dsp/validate_ppg.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import wfdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dsp"))

from fetch_ppg import DEFAULT_RECORDS  # noqa: E402
from ppg import MIN_PERIODICITY, _pulsatile, periodicity  # noqa: E402

DATA_DIR = ROOT / "data" / "bidmc"
WINDOW_S = 30.0
HOP_S = 30.0


@dataclass
class PpgRecordResult:
    """Pulse-rate agreement for one recording."""

    record: str
    n_windows: int
    n_accepted: int
    n_unstable_reference: int
    mean_abs_error: float
    max_abs_error: float
    within_5bpm: float
    median_periodicity: float
    reference_bpm: float

    @property
    def accepted_frac(self) -> float:
        return self.n_accepted / self.n_windows if self.n_windows else 0.0


#: A bedside monitor's pulse numeric is itself an estimate, updated every second and
#: smoothed by the monitor's own algorithm. When it moves by more than this inside the
#: comparison window there is no single reference value to compare against, and scoring
#: one anyway measures the reference's variability rather than ours (D-40).
REFERENCE_STABILITY_BPM = 8.0


def load(record: str, data_dir: Path = DATA_DIR) -> tuple[np.ndarray, float, np.ndarray]:
    """Return the PPG waveform, its sample rate, and the per-second reference pulse."""
    base = str(data_dir / record)
    if not Path(f"{base}.hea").exists():
        raise FileNotFoundError(f"{record} not found in {data_dir} - run: python dsp/fetch_ppg.py")
    wave = wfdb.rdrecord(base)
    numerics = wfdb.rdrecord(f"{base}n")
    # Channel names in this database carry a trailing comma.
    pleth = next(i for i, name in enumerate(wave.sig_name) if "PLETH" in name)
    pulse = next(i for i, name in enumerate(numerics.sig_name) if "PULSE" in name)
    return np.asarray(wave.p_signal[:, pleth], dtype=float), float(wave.fs), np.asarray(
        numerics.p_signal[:, pulse], dtype=float
    )


def validate_record(record: str, data_dir: Path = DATA_DIR) -> PpgRecordResult:
    """Score one recording window by window against the bedside monitor."""
    signal, fs, reference = load(record, data_dir)
    win = int(WINDOW_S * fs)
    hop = int(HOP_S * fs)

    errors: list[float] = []
    strengths: list[float] = []
    n_windows = 0
    n_unstable = 0

    for start in range(0, signal.size - win + 1, hop):
        segment = signal[start : start + win]
        t0 = int(start / fs)
        ref_window = reference[t0 : t0 + int(WINDOW_S)]
        ref = float(np.nanmedian(ref_window)) if ref_window.size else np.nan
        if not np.all(np.isfinite(segment)) or not np.isfinite(ref):
            continue

        # Exclude windows where the reference itself is moving. Comparing a stable
        # 30-second estimate against a numeric that swings 50-70 bpm inside the same
        # window scores the monitor's variability, not this pipeline's error.
        finite_ref = ref_window[np.isfinite(ref_window)]
        if finite_ref.size and float(np.ptp(finite_ref)) > REFERENCE_STABILITY_BPM:
            n_unstable += 1
            continue

        n_windows += 1

        period, strength = periodicity(_pulsatile(segment, fs), fs)
        if period is None or strength < MIN_PERIODICITY:
            # Declining is the correct outcome, not a missing measurement.
            continue
        strengths.append(strength)
        errors.append(60.0 / period - ref)

    err = np.abs(errors) if errors else np.array([np.nan])
    return PpgRecordResult(
        record=record,
        n_windows=n_windows,
        n_accepted=len(errors),
        n_unstable_reference=n_unstable,
        mean_abs_error=float(np.mean(err)),
        max_abs_error=float(np.max(err)),
        within_5bpm=float(np.mean(err <= 5.0)) if errors else 0.0,
        median_periodicity=float(np.median(strengths)) if strengths else 0.0,
        reference_bpm=float(np.nanmedian(reference)),
    )


def validate_all(records: list[str] | None = None, data_dir: Path = DATA_DIR) -> list[PpgRecordResult]:
    """Score every available record, skipping ones that were not downloaded."""
    out: list[PpgRecordResult] = []
    for record in records or DEFAULT_RECORDS:
        try:
            out.append(validate_record(record, data_dir))
        except FileNotFoundError as exc:
            print(f"{record}: SKIPPED - {exc}", file=sys.stderr)
    return out


def aggregate(results: list[PpgRecordResult]) -> dict[str, float]:
    """Pool by window, not by record, so a long record is not weighted like a short one."""
    if not results:
        return {}
    accepted = sum(r.n_accepted for r in results)
    windows = sum(r.n_windows for r in results)
    unstable = sum(r.n_unstable_reference for r in results)
    weighted = sum(r.mean_abs_error * r.n_accepted for r in results if r.n_accepted)
    within = sum(r.within_5bpm * r.n_accepted for r in results if r.n_accepted)
    return {
        "n_records": float(len(results)),
        "n_windows": float(windows),
        "n_unstable_reference": float(unstable),
        "n_accepted": float(accepted),
        "accepted_frac": accepted / windows if windows else 0.0,
        "mean_abs_error": weighted / accepted if accepted else float("nan"),
        "max_abs_error": max(r.max_abs_error for r in results),
        "within_5bpm": within / accepted if accepted else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    records = (argv or sys.argv[1:]) or DEFAULT_RECORDS
    results = validate_all(records)
    if not results:
        print("No records scored - run: python dsp/fetch_ppg.py", file=sys.stderr)
        return 1

    print(f"\n{'rec':>9} {'ref bpm':>8} {'windows':>8} {'used':>6} {'mean err':>9} "
          f"{'max err':>8} {'<=5 bpm':>8} {'periodicity':>12}")
    print("-" * 76)
    for r in results:
        print(f"{r.record:>9} {r.reference_bpm:8.0f} {r.n_windows:8d} {r.n_accepted:6d} "
              f"{r.mean_abs_error:9.2f} {r.max_abs_error:8.2f} {100 * r.within_5bpm:7.1f}% "
              f"{r.median_periodicity:12.3f}")
    totals = aggregate(results)
    print("-" * 76)
    print(f"{'POOLED':>9} {'':>8} {int(totals['n_windows']):8d} {int(totals['n_accepted']):6d} "
          f"{totals['mean_abs_error']:9.2f} {totals['max_abs_error']:8.2f} "
          f"{100 * totals['within_5bpm']:7.1f}%")

    ok = totals["mean_abs_error"] <= 5.0
    print(f"\n{'PASS' if ok else 'FAIL'}: mean pulse-rate error {totals['mean_abs_error']:.2f} bpm "
          f"against the ±5 bpm of SR-01, over {int(totals['n_accepted'])} accepted windows "
          f"({100 * totals['accepted_frac']:.1f} % of {int(totals['n_windows'])}).")
    print(f"      {int(totals['n_unstable_reference'])} further windows were excluded because the bedside "
          f"reference moved more than\n      {REFERENCE_STABILITY_BPM:.0f} bpm inside the window, "
          f"leaving nothing stable to compare against.\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
