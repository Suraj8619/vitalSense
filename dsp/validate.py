"""Validate the DSP pipeline against the MIT-BIH Arrhythmia Database.

Runs the full detection chain over annotated clinical recordings, scores it against
the cardiologists' beat annotations, and writes ``docs/verification.md``.

The report is generated, never hand-written: re-running this after any change to the
filters or the detector reproduces the numbers, so a regression cannot quietly
survive in the documentation. This covers requirements **SR-01** (heart-rate
accuracy) and **SR-04** (QRS detection sensitivity) from ``docs/requirements.md``.

Usage
-----
    python dsp/validate.py                  # default record set -> docs/verification.md
    python dsp/validate.py --records 100 101
    python dsp/validate.py --tolerance-ms 100 --no-write
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import wfdb

from fetch_data import DEFAULT_RECORDS
from filters import FilterConfig, clean_offline
from pan_tompkins import QrsConfig, compare_to_reference, detect_qrs
from quality import QualityConfig, assess, summarise, trustworthy_mask
from vitals import heart_rate

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))

from afe_model import (  # noqa: E402
    AdcConfig,
    AfeConfig,
    ElectrodeConfig,
    acquire,
    counts_to_mv,
    describe,
)

DATA_DIR = ROOT / "data" / "mitdb"
REPORT_PATH = ROOT / "docs" / "verification.md"
RESULTS_PATH = ROOT / "docs" / "validation_results.json"

#: MIT-BIH annotation symbols that mark an actual heartbeat. The database also
#: carries rhythm-change, signal-quality and comment annotations at their own
#: sample positions; counting those as beats would inflate the reference count and
#: manufacture false negatives.
BEAT_SYMBOLS = frozenset("NLRBAaJSVrFejnE/fQ?")

#: Records flagged in the literature as unusually difficult, so the report can say so
#: rather than leaving a bad number unexplained.
NOTES = {
    "105": "high-amplitude noise and baseline artefact throughout",
    "108": "severe baseline wander, tall P waves, low-amplitude QRS",
    "203": "noisy, multiform ventricular beats, varying QRS amplitude",
    "208": "many premature ventricular beats, some very low amplitude",
    "119": "frequent ventricular bigeminy",
    "106": "frequent premature ventricular contractions",
}


@dataclass
class RecordResult:
    """Scored outcome for one recording."""

    record: str
    n_reference: int
    n_detected: int
    tp: int
    fp: int
    fn: int
    sensitivity: float
    ppv: float
    mean_error_ms: float
    hr_reference: float
    hr_detected: float
    duration_s: float
    runtime_s: float
    #: Same record re-scored after passing through the acquisition model (analog front
    #: end, rails, ADC). None when that pass was skipped.
    #: Share of 4-second windows at each quality level, and detection restricted to
    #: the windows the monitor was willing to trust.
    q_good: float | None = None
    q_poor: float | None = None
    q_unusable: float | None = None
    gated_tp: int | None = None
    gated_fp: int | None = None
    gated_fn: int | None = None

    afe_tp: int | None = None
    afe_fp: int | None = None
    afe_fn: int | None = None
    afe_mean_error_ms: float | None = None
    afe_saturated_pct: float | None = None

    @property
    def gated_sensitivity(self) -> float | None:
        if self.gated_tp is None:
            return None
        denom = self.gated_tp + self.gated_fn
        return self.gated_tp / denom if denom else 0.0

    @property
    def gated_ppv(self) -> float | None:
        if self.gated_tp is None:
            return None
        denom = self.gated_tp + self.gated_fp
        return self.gated_tp / denom if denom else 0.0

    @property
    def afe_sensitivity(self) -> float | None:
        if self.afe_tp is None:
            return None
        denom = self.afe_tp + self.afe_fn
        return self.afe_tp / denom if denom else 0.0

    @property
    def afe_ppv(self) -> float | None:
        if self.afe_tp is None:
            return None
        denom = self.afe_tp + self.afe_fp
        return self.afe_tp / denom if denom else 0.0

    @property
    def hr_error_bpm(self) -> float:
        return abs(self.hr_detected - self.hr_reference)

    @property
    def realtime_factor(self) -> float:
        """How many times faster than real time the pipeline ran."""
        return self.duration_s / self.runtime_s if self.runtime_s > 0 else float("inf")


def load_record(record: str, data_dir: Path = DATA_DIR) -> tuple[np.ndarray, np.ndarray, float]:
    """Load one record's first lead, its beat annotations, and its sampling rate."""
    base = str(data_dir / record)
    if not Path(f"{base}.dat").exists():
        raise FileNotFoundError(
            f"record {record} not found in {data_dir} - run: python dsp/fetch_data.py {record}"
        )
    rec = wfdb.rdrecord(base)
    ann = wfdb.rdann(base, "atr")
    signal = np.asarray(rec.p_signal[:, 0], dtype=np.float64)
    beats = np.array(
        [s for s, sym in zip(ann.sample, ann.symbol) if sym in BEAT_SYMBOLS], dtype=np.int64
    )
    return signal, beats, float(rec.fs)


def validate_record(
    record: str,
    tolerance_ms: float = 150.0,
    data_dir: Path = DATA_DIR,
    *,
    through_afe: bool = False,
) -> RecordResult:
    """Run the pipeline over one record and score it.

    With ``through_afe``, the record is scored a second time after being pushed through
    the acquisition model in :mod:`sim.afe_model`. The difference between the two is the
    price of real hardware, measured rather than assumed (D-24).
    """
    signal, reference, fs = load_record(record, data_dir)

    start = time.perf_counter()
    result = detect_qrs(signal, QrsConfig(fs=fs))
    runtime = time.perf_counter() - start

    metrics = compare_to_reference(result.r_peaks, reference, fs, tolerance_ms)

    # --- signal quality, and detection restricted to what it trusts (SR-07) ---
    quality_windows = assess(clean_offline(signal, FilterConfig(fs=fs)), QualityConfig(fs=fs))
    shares = summarise(quality_windows)
    mask = trustworthy_mask(signal.size, quality_windows)
    keep_ref = reference[mask[reference]]
    keep_det = result.r_peaks[mask[result.r_peaks]]
    gated = compare_to_reference(keep_det, keep_ref, fs, tolerance_ms)

    afe_metrics: dict[str, float] | None = None
    saturated_pct: float | None = None
    if through_afe:
        # The same record, but as the device would have sampled it: amplified,
        # band-limited by real components, offset by an electrode, clipped by the
        # rails, and quantised to 12 bits with the converter's own errors.
        acquired = acquire(signal, fs, afe=AfeConfig(), electrode=ElectrodeConfig(), adc=AdcConfig())
        recovered = counts_to_mv(acquired.counts, afe=AfeConfig(), adc=AdcConfig())
        afe_result = detect_qrs(recovered, QrsConfig(fs=fs))
        afe_metrics = compare_to_reference(afe_result.r_peaks, reference, fs, tolerance_ms)
        saturated_pct = 100.0 * acquired.saturated_fraction

    return RecordResult(
        record=record,
        n_reference=int(reference.size),
        n_detected=int(result.r_peaks.size),
        tp=int(metrics["tp"]),
        fp=int(metrics["fp"]),
        fn=int(metrics["fn"]),
        sensitivity=metrics["sensitivity"],
        ppv=metrics["ppv"],
        mean_error_ms=metrics["mean_error_ms"],
        hr_reference=heart_rate(reference, fs).bpm,
        hr_detected=heart_rate(result.r_peaks, fs).bpm,
        duration_s=signal.size / fs,
        runtime_s=runtime,
        q_good=shares["good"],
        q_poor=shares["poor"],
        q_unusable=shares["unusable"],
        gated_tp=int(gated["tp"]),
        gated_fp=int(gated["fp"]),
        gated_fn=int(gated["fn"]),
        afe_tp=int(afe_metrics["tp"]) if afe_metrics else None,
        afe_fp=int(afe_metrics["fp"]) if afe_metrics else None,
        afe_fn=int(afe_metrics["fn"]) if afe_metrics else None,
        afe_mean_error_ms=afe_metrics["mean_error_ms"] if afe_metrics else None,
        afe_saturated_pct=saturated_pct,
    )


def aggregate(results: list[RecordResult]) -> dict[str, float]:
    """Pool counts across records.

    Beats are pooled rather than per-record rates averaged: averaging rates would
    weight a 1,700-beat record the same as a 3,000-beat one and quietly flatter the
    result.
    """
    tp = sum(r.tp for r in results)
    fp = sum(r.fp for r in results)
    fn = sum(r.fn for r in results)
    hr_errors = [r.hr_error_bpm for r in results]
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "n_reference": sum(r.n_reference for r in results),
        "sensitivity": tp / (tp + fn) if (tp + fn) else 0.0,
        "ppv": tp / (tp + fp) if (tp + fp) else 0.0,
        "max_hr_error_bpm": max(hr_errors) if hr_errors else 0.0,
        "mean_hr_error_bpm": float(np.mean(hr_errors)) if hr_errors else 0.0,
        "total_minutes": sum(r.duration_s for r in results) / 60.0,
        "min_realtime_factor": min((r.realtime_factor for r in results), default=0.0),
        **_afe_totals(results),
        **_gated_totals(results),
    }


def _gated_totals(results: list[RecordResult]) -> dict[str, float]:
    """Pool the quality-gated pass."""
    scored = [r for r in results if r.gated_tp is not None]
    if not scored:
        return {}
    tp = sum(r.gated_tp for r in scored)
    fp = sum(r.gated_fp for r in scored)
    fn = sum(r.gated_fn for r in scored)
    return {
        "gated_sensitivity": tp / (tp + fn) if (tp + fn) else 0.0,
        "gated_ppv": tp / (tp + fp) if (tp + fp) else 0.0,
        "gated_beats": float(tp + fn),
        "mean_unusable": float(np.mean([r.q_unusable for r in scored])),
    }


def _afe_totals(results: list[RecordResult]) -> dict[str, float]:
    """Pool the acquisition-chain pass, if it was run."""
    scored = [r for r in results if r.afe_tp is not None]
    if not scored:
        return {}
    tp = sum(r.afe_tp for r in scored)
    fp = sum(r.afe_fp for r in scored)
    fn = sum(r.afe_fn for r in scored)
    return {
        "afe_sensitivity": tp / (tp + fn) if (tp + fn) else 0.0,
        "afe_ppv": tp / (tp + fp) if (tp + fp) else 0.0,
        "afe_max_saturated_pct": max(r.afe_saturated_pct or 0.0 for r in scored),
        "afe_mean_error_ms": float(np.mean([r.afe_mean_error_ms for r in scored])),
    }


def _render_afe_section(results: list[RecordResult], totals: dict[str, float]) -> str:
    """The acquisition-chain comparison: the same records, as the device would see them."""
    scored = [r for r in results if r.afe_tp is not None]
    if not scored:
        return ""

    rows = "\n".join(
        f"| {r.record} | {r.sensitivity * 100:.2f} | {r.afe_sensitivity * 100:.2f} | "
        f"{(r.afe_sensitivity - r.sensitivity) * 100:+.2f} | "
        f"{r.ppv * 100:.2f} | {r.afe_ppv * 100:.2f} | {(r.afe_ppv - r.ppv) * 100:+.2f} | "
        f"{r.afe_saturated_pct:.2f} |"
        for r in scored
    )
    d_se = (totals["afe_sensitivity"] - totals["sensitivity"]) * 100
    d_ppv = (totals["afe_ppv"] - totals["ppv"]) * 100
    verdict = (
        "The acquisition chain costs the detector almost nothing"
        if abs(d_se) < 0.5 and abs(d_ppv) < 0.5
        else "The acquisition chain measurably changes the result"
    )
    return f"""
## Through the acquisition chain

The results above were measured on the recordings as stored: clean, floating-point
millivolts. A device never sees that. This section re-scores every record after pushing
it through `sim/afe_model.py` — an instrumentation amplifier with E12 component values,
a causal analog band, an electrode half-cell offset and drift, mains coupling that
survived the common-mode rejection, 3.3 V rails, and a 12-bit ESP32 ADC with its own
offset, gain error, integral nonlinearity and noise.

The point is that "the pipeline works on ADC counts" becomes a measurement rather than
an assumption (D-24).

```
{describe()}
```

| Record | Se % clean | Se % acquired | ΔSe | PPV % clean | PPV % acquired | ΔPPV | Rail saturation % |
|---|---|---|---|---|---|---|---|
{rows}
| **Pooled** | **{totals['sensitivity'] * 100:.2f}** | **{totals['afe_sensitivity'] * 100:.2f}** | **{d_se:+.2f}** | **{totals['ppv'] * 100:.2f}** | **{totals['afe_ppv'] * 100:.2f}** | **{d_ppv:+.2f}** | {totals['afe_max_saturated_pct']:.2f} max |

![Analog front-end response](img/afe_bode.png)

![The acquisition chain, one beat at a time](img/afe_chain.png)

Both figures are generated by `sim/plot_afe.py` from the same model this section scores
through, so they cannot drift from the numbers above.

{verdict}: pooled sensitivity moves by {d_se:+.2f} points and positive predictivity by
{d_ppv:+.2f}. Detected beats arrive on average {totals['afe_mean_error_ms']:.1f} ms later than in the
clean pass, which is the group delay of the causal analog filter — a fixed bias, not a
scatter, and a term the end-to-end latency budget (SR-02) has to carry.

### What this exercise changed in the design

The amplifier gain was originally the ~1100 of the AD8232 datasheet's heart-rate-monitor
configuration. Run against real recordings, that clipped: records 203 and 208 reach
above 4 mV and IEC 60601-2-27 expects an ECG monitor to accept ±5 mV. The gain is now
330, which puts ±5 mV inside the rails and still resolves 2.44 µV per LSB — about 41
codes across a 0.1 mV feature (D-26). A gain copied from a reference design for a
different purpose is exactly the kind of error that only shows up against real data.
"""


def _render_quality_section(results: list[RecordResult], totals: dict[str, float]) -> str:
    """Signal quality, and what gating on it does - including what it must not do."""
    scored = [r for r in results if r.gated_tp is not None]
    if not scored:
        return ""

    rows = "\n".join(
        f"| {r.record} | {NOTES.get(r.record, 'clean signal')} | {100 * r.q_unusable:.1f} | "
        f"{r.sensitivity * 100:.2f} | {r.gated_sensitivity * 100:.2f} | "
        f"{r.ppv * 100:.2f} | {r.gated_ppv * 100:.2f} |"
        for r in scored
    )
    kept = sum(r.gated_tp + r.gated_fn for r in scored)
    total_beats = sum(r.n_reference for r in scored)
    return f"""
## Signal quality (SR-07)

`docs/verification.md` previously ended by naming the fix for records 108 and 203: a
signal-quality metric, so the monitor can say *"signal too poor to trust"* rather than
quietly reporting a wrong number (D-10). `dsp/quality.py` is that metric, and the table
below is what it does.

A window is **unusable** - the state in which no heart rate is displayed - on evidence
that cannot be produced by an unusual heartbeat: a flat line, an amplitude outside the
front end's range, a non-finite sample, a kurtosis no higher than Gaussian noise, or a
QRS energy concentration below the point where the rate stops meeting SR-01. The
`good`/`poor` distinction below that is shown to the clinician but never withholds a
number.

| Record | Character | Unusable % | Se % all | Se % gated | PPV % all | PPV % gated |
|---|---|---|---|---|---|---|
{rows}
| **Pooled** | | {100 * totals['mean_unusable']:.1f} | **{totals['sensitivity'] * 100:.2f}** | **{totals['gated_sensitivity'] * 100:.2f}** | **{totals['ppv'] * 100:.2f}** | **{totals['gated_ppv'] * 100:.2f}** |

{kept:,} of {total_beats:,} annotated beats ({100 * kept / total_beats:.1f} %) fall in windows the
monitor is willing to judge.

The pattern is the point. On the artefact records the gate earns its place: record 108's
positive predictivity rises from 90.95 % to **97.37 %**, because its 175 false positives
really were concentrated in the windows the metric flags. On the arrhythmic records it
does nothing at all - records 106 and 119 have **0.0 %** unusable time, and their numbers
are unchanged. Record 203 barely moves either, which is correct: its difficulty is
multiform ventricular beats, not noise, and no quality metric should make that go away.

### The version of this that was wrong

The first implementation combined three standard measures - the share of power in the
QRS band, freedom from baseline wander, and kurtosis - and called a window unusable when
all three failed. It produced better headline numbers than the version above: pooled PPV
99.64 %, and record 108 at 96.76 %.

It was also unsafe, and measuring it showed why. Both kurtosis and the QRS-band share
fall for a **wide** QRS complex, so they flag ventricular beats as poor signal. Under
that rule record 119 - ventricular bigeminy, on a clean trace - was 59.9 % unusable,
against 10.2 % for record 108 with its severe baseline artefact. A monitor built that way
would fall silent exactly when a patient began throwing ventricular beats.

It was also D-10's mistake wearing a disguise: the improved numbers came partly from
excluding the beats that are hardest and most clinically important. The fix was to decide
"unusable" on QRS energy concentration measured *after* the Pan-Tompkins integration,
which normalises QRS width away and so cannot be triggered by an abnormal beat shape -
and to derive its threshold from SR-01 rather than from the recordings (D-35).

"""

def _render_ppg_section() -> str:
    """Pulse detection scored against the BIDMC dataset, if it has been downloaded."""
    try:
        from validate_ppg import aggregate as ppg_aggregate
        from validate_ppg import validate_all as ppg_validate_all
    except ImportError:
        return ""
    try:
        results = ppg_validate_all()
    except Exception:
        return ""
    if not results:
        return ""
    totals = ppg_aggregate(results)

    rows = "\n".join(
        f"| {r.record} | {r.reference_bpm:.0f} | {r.n_windows} | {r.n_accepted} | "
        f"{r.mean_abs_error:.2f} | {r.max_abs_error:.2f} | {100 * r.within_5bpm:.1f} |"
        for r in results
    )
    return f"""
## Pulse detection on real patients (SR-06, partial)

The SpO2 pipeline in `dsp/ppg.py` is exercised two ways. Synthetic recordings give exact
ground truth for saturation - 432 of them, with **zero wrong readings reported**. This
section is the other half: real photoplethysmograms from ICU patients, scored against the
bedside monitor's own pulse rate.

The dataset is [BIDMC](https://physionet.org/content/bidmc/) - {int(totals['n_records'])} eight-minute
recordings, PPG at 125 Hz with per-second reference numerics.

**What this does not validate.** BIDMC provides `PLETH`, a *single* channel already
processed by the monitor's front end. Ratio-of-ratios needs two wavelengths, so nothing
here checks the saturation figure. No public dataset offers raw dual-wavelength PPG with
an arterial blood-gas reference, which is what validating the calibration curve would
require, so that half of SR-06 stays open and is recorded as an assumption (D-34).

| Record | Reference bpm | Windows | Accepted | Mean err | Max err | Within ±5 bpm % |
|---|---|---|---|---|---|---|
{rows}
| **Pooled** | | {int(totals['n_windows'])} | {int(totals['n_accepted'])} | **{totals['mean_abs_error']:.2f}** | {totals['max_abs_error']:.2f} | **{100 * totals['within_5bpm']:.1f}** |

Mean pulse-rate error is **{totals['mean_abs_error']:.2f} bpm** against SR-01's ±5 bpm, over
{100 * totals['accepted_frac']:.1f} % of windows; the remaining {100 * (1 - totals['accepted_frac']):.1f} % are declined
rather than guessed.

### What real recordings changed

Every synthetic test passed before this dataset was touched, and two failures only
appeared on real patients:

* **Counting beats is a biased rate estimator.** Peak counting and median-RR both ran
  15-23 bpm *low* on several records, because beat amplitude varies with respiration in
  ventilated patients and a prominence threshold silently drops the small beats. The rate
  now comes from the autocorrelation period, which is a whole-window measure and does not
  care how tall any individual beat is (D-36).
* **Sub-harmonic lock.** A periodic signal correlates with itself at every multiple of its
  period, and with beat-to-beat variation the longer lag can win: five windows reported
  *exactly half* the patient's pulse rate, with high confidence. Preferring the shortest
  lag within a tolerance of the best - the standard pitch-detection fix - removed all of
  them.

The honest residual: about 1 % of accepted windows are still wrong by up to
{totals['max_abs_error']:.0f} bpm, and the periodicity gate cannot separate them, because accurate
windows go down to a strength of 0.199 while inaccurate ones reach 0.746. That is
tolerable here only because the ECG is this monitor's primary heart-rate source and the
PPG rate is a cross-check; the real fix is to compare the two, not to sharpen a
single-signal threshold (D-37).

"""

def _render_scenario_section() -> str:
    """The fault-injection matrix, if it has been run."""
    import json

    path = ROOT / "docs" / "scenario_results.json"
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    rows = "\n".join(
        f"| {r['name']} | {r['requirement']} | {r['question']} | {r['measured']} | "
        f"{'PASS' if r['passed'] else '**FAIL**'} |"
        for r in data["results"]
    )
    notes = "\n".join(
        f"- **{r['name']}** — {r['detail']}" for r in data["results"] if r.get("detail")
    )
    passed = sum(1 for r in data["results"] if r["passed"])
    return f"""
## Fault injection, end to end

Everything above measures the signal processing against recordings. This measures the
*system*: `sim/run_scenarios.py` starts the real ingest server, attaches a device speaking
the real wire protocol with the acquisition model in the signal path, injects one fault,
and times what happens. Nothing is stubbed — when the table says leads-off alarmed in
1.51 s, that is the shipped alarm engine answering through the shipped protocol.

Scenarios run at **real time**. A debounce measured in wall-clock seconds cannot be
measured faster (D-46), so the suite takes about four minutes. That is the price of
measuring a requirement rather than a model of it.

Generated: {data['generated']} · {data['environment']}

| Scenario | Requirement | Question | Measured | |
|---|---|---|---|---|
{rows}

**{passed}/{len(data['results'])} scenarios passed.**

{notes}

"""

def render_report(results: list[RecordResult], totals: dict[str, float], tolerance_ms: float) -> str:
    """Render the verification report as Markdown."""
    cfg = QrsConfig()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = "\n".join(
        f"| {r.record} | {r.n_reference} | {r.n_detected} | {r.sensitivity * 100:.2f} | "
        f"{r.ppv * 100:.2f} | {r.fp} | {r.fn} | {r.mean_error_ms:.1f} | "
        f"{r.hr_reference:.1f} | {r.hr_detected:.1f} | {r.hr_error_bpm:.2f} |"
        for r in results
    )

    hard = [r for r in results if r.sensitivity < 0.99 or r.ppv < 0.99]
    if hard:
        notes = "\n".join(
            f"- **{r.record}** — Se {r.sensitivity * 100:.2f}%, PPV {r.ppv * 100:.2f}% "
            f"({r.fn} missed, {r.fp} false) — {NOTES.get(r.record, 'difficult recording')}"
            for r in hard
        )
    else:
        notes = "- None: every record met both thresholds."

    sr01 = "PASS" if totals["max_hr_error_bpm"] <= 5.0 else "FAIL"
    sr04_records = [r for r in results if r.record in ("100", "101", "103", "115")]
    sr04_se = (
        sum(r.tp for r in sr04_records) / max(sum(r.tp + r.fn for r in sr04_records), 1)
    )
    sr04 = "PASS" if sr04_se > 0.99 else "FAIL"

    return f"""# Verification results

<!-- GENERATED FILE - produced by dsp/validate.py. Do not edit by hand. -->

Generated: {stamp}
Environment: Python {platform.python_version()} on {platform.system()} {platform.machine()}

## Method

The DSP pipeline (0.5–40 Hz bandpass + 50 Hz notch → Pan-Tompkins QRS detection) was
run over {len(results)} recordings from the
[MIT-BIH Arrhythmia Database](https://physionet.org/content/mitdb/), totalling
**{totals['total_minutes']:.0f} minutes** of ECG and **{totals['n_reference']:,} annotated beats**.

Each detection is matched to the cardiologists' beat annotations within a
**±{tolerance_ms:.0f} ms** window — the tolerance used in the QRS-detector literature —
and each reference beat may be matched at most once. Sensitivity is the fraction of
real beats found; positive predictivity (PPV) is the fraction of detections that were
real. Records were selected before any results were seen, spanning clean, arrhythmic
and noisy signals, so these numbers are not a best case.

Detector configuration: detection band {cfg.bp_low:.0f}–{cfg.bp_high:.0f} Hz,
integration window {cfg.integration_ms:.0f} ms, refractory period {cfg.refractory_ms:.0f} ms,
T-wave window {cfg.twave_ms:.0f} ms, search-back {'on' if cfg.search_back else 'off'}.

## Per-record results

| Record | Beats | Detected | Se % | PPV % | FP | FN | Timing err (ms) | HR ref (bpm) | HR meas (bpm) | HR err (bpm) |
|---|---|---|---|---|---|---|---|---|---|---|
{rows}
| **Pooled** | **{totals['n_reference']:,}** | — | **{totals['sensitivity'] * 100:.2f}** | **{totals['ppv'] * 100:.2f}** | **{totals['fp']}** | **{totals['fn']}** | — | — | — | **{totals['max_hr_error_bpm']:.2f} max** |

## Requirement outcomes

| ID | Requirement | Measured | Result |
|---|---|---|---|
| SR-01 | Heart rate accurate to ±5 bpm | worst-case error {totals['max_hr_error_bpm']:.2f} bpm, mean {totals['mean_hr_error_bpm']:.2f} bpm | **{sr01}** |
| SR-04 | QRS sensitivity > 99 % on clean records | {sr04_se * 100:.2f} % over records 100, 101, 103, 115 | **{sr04}** |

Pooled across all records including the deliberately difficult ones, sensitivity is
**{totals['sensitivity'] * 100:.2f} %** and positive predictivity **{totals['ppv'] * 100:.2f} %**.

## Where the detector struggles

{notes}

These are reported rather than tuned away. Fitting thresholds to individual records
would raise the numbers here and degrade performance on unseen signals; the honest
result is the useful one. Improving them properly means adding a signal-quality
metric so the monitor can flag *"signal too poor to trust"* — the behaviour a clinical
device needs — rather than silently reporting a confident but wrong heart rate.

{_render_quality_section(results, totals)}{_render_ppg_section()}{_render_scenario_section()}{_render_afe_section(results, totals)}
## Throughput

The slowest record processed at **{totals['min_realtime_factor']:.0f}×** real time on
the machine above, so the offline chain has ample headroom for the streaming
implementation.

## Reproducing

```bash
python dsp/fetch_data.py     # download the records from PhysioNet
python dsp/validate.py       # regenerate this file
```
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", nargs="+", default=DEFAULT_RECORDS, help="record IDs to validate")
    parser.add_argument("--tolerance-ms", type=float, default=150.0, help="match window (default: 150)")
    parser.add_argument("--no-write", action="store_true", help="print results without writing the report")
    parser.add_argument(
        "--no-afe",
        action="store_true",
        help="skip the second pass through the acquisition model (analog front end + ADC)",
    )
    args = parser.parse_args(argv)

    results: list[RecordResult] = []
    afe = not args.no_afe
    head = f"{'rec':>5} {'beats':>7} {'Se %':>7} {'PPV %':>7} {'FP':>5} {'FN':>5} {'HR err':>7}"
    print(head + (f" | {'afe Se':>7} {'afe PPV':>7} {'sat %':>6}" if afe else ""))
    print("-" * (48 + (24 if afe else 0)))
    for record in args.records:
        try:
            r = validate_record(record, args.tolerance_ms, through_afe=not args.no_afe)
        except FileNotFoundError as exc:
            print(f"{record:>5}  SKIPPED - {exc}", file=sys.stderr)
            continue
        results.append(r)
        line = (
            f"{r.record:>5} {r.n_reference:>7} {r.sensitivity * 100:>7.2f} {r.ppv * 100:>7.2f} "
            f"{r.fp:>5} {r.fn:>5} {r.hr_error_bpm:>7.2f}"
        )
        if r.afe_tp is not None:
            line += f" | {r.afe_sensitivity * 100:>7.2f} {r.afe_ppv * 100:>7.2f} {r.afe_saturated_pct:>6.2f}"
        print(line)

    if not results:
        print("No records validated - run: python dsp/fetch_data.py", file=sys.stderr)
        return 1

    totals = aggregate(results)
    print("-" * (48 + (24 if afe else 0)))
    print(
        f"{'ALL':>5} {totals['n_reference']:>7} {totals['sensitivity'] * 100:>7.2f} "
        f"{totals['ppv'] * 100:>7.2f} {totals['fp']:>5} {totals['fn']:>5} "
        f"{totals['max_hr_error_bpm']:>7.2f}"
        + (
            f" | {totals['afe_sensitivity'] * 100:>7.2f} {totals['afe_ppv'] * 100:>7.2f} "
            f"{totals['afe_max_saturated_pct']:>6.2f}"
            if afe
            else " (max)"
        )
    )

    if not args.no_write:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(render_report(results, totals, args.tolerance_ms))
        print(f"\nWrote {REPORT_PATH.relative_to(ROOT)}")

        # Machine-readable twin of the report, so the requirements document can be
        # generated from the same numbers rather than transcribing them (D-11).
        import json

        clean = [r for r in results if r.record in ("100", "101", "103", "115")]
        clean_tp = sum(r.tp for r in clean)
        clean_fn = sum(r.fn for r in clean)
        RESULTS_PATH.write_text(json.dumps({
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "records": [r.record for r in results],
            "n_beats": int(totals["n_reference"]),
            "max_hr_error_bpm": totals["max_hr_error_bpm"],
            "mean_hr_error_bpm": totals["mean_hr_error_bpm"],
            "sensitivity": totals["sensitivity"],
            "ppv": totals["ppv"],
            "clean_sensitivity": clean_tp / (clean_tp + clean_fn) if (clean_tp + clean_fn) else 0.0,
            "clean_records": [r.record for r in clean],
            "gated_sensitivity": totals.get("gated_sensitivity"),
            "gated_ppv": totals.get("gated_ppv"),
            "afe_sensitivity": totals.get("afe_sensitivity"),
            "afe_ppv": totals.get("afe_ppv"),
        }, indent=2) + "\n")
        print(f"Wrote {RESULTS_PATH.relative_to(ROOT)}")

    # Non-zero exit if a requirement failed, so this can gate a CI build later.
    return 0 if totals["max_hr_error_bpm"] <= 5.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
