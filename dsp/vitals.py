"""Derive clinical vitals from detected beats.

Turns R-peak sample indices into the numbers a monitor actually displays - heart
rate, its variability, and a rhythm classification - along with the physiological
sanity checks that stop a single mis-detected beat from producing an alarming number
on screen.

The alert engine in the server reuses :func:`classify_rate` and
:data:`DEFAULT_LIMITS` so that the thresholds shown on the dashboard and the
thresholds used offline are the same thresholds, defined once.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# --------------------------------------------------------------------------------
# Physiological bounds
# --------------------------------------------------------------------------------

#: Beats closer than 250 ms (240 bpm) or further apart than 3 s (20 bpm) are treated
#: as detector artefacts rather than real beats when computing rate statistics.
MIN_PLAUSIBLE_RR_S = 0.25
MAX_PLAUSIBLE_RR_S = 3.0


@dataclass(frozen=True)
class RateLimits:
    """Alarm thresholds for heart rate, in bpm."""

    brady: float = 60.0  # below this: bradycardia
    tachy: float = 100.0  # above this: tachycardia

    def __post_init__(self) -> None:
        if not 0 < self.brady < self.tachy:
            raise ValueError(f"need 0 < brady ({self.brady}) < tachy ({self.tachy})")


DEFAULT_LIMITS = RateLimits()


@dataclass(frozen=True)
class HeartRateResult:
    """Heart-rate summary over a window of beats."""

    bpm: float  # mean rate over the window
    instantaneous_bpm: np.ndarray  # rate implied by each individual RR interval
    rr_s: np.ndarray  # the plausible RR intervals used
    n_beats: int  # beats seen before filtering
    n_rejected: int  # RR intervals discarded as implausible
    sdnn_ms: float  # HRV: standard deviation of RR intervals
    rmssd_ms: float  # HRV: root mean square of successive RR differences
    classification: str  # "bradycardia" | "normal" | "tachycardia" | "insufficient data"

    @property
    def valid(self) -> bool:
        return self.n_beats >= 2 and self.rr_s.size > 0


# --------------------------------------------------------------------------------
# Core computations
# --------------------------------------------------------------------------------


def rr_intervals(r_peaks: np.ndarray, fs: float) -> np.ndarray:
    """RR intervals in seconds, from R-peak sample indices."""
    peaks = np.asarray(r_peaks, dtype=np.float64)
    if peaks.size < 2:
        return np.array([], dtype=np.float64)
    if fs <= 0:
        raise ValueError(f"fs must be positive, got {fs}")
    return np.diff(peaks) / fs


def plausible_rr(rr_s: np.ndarray) -> tuple[np.ndarray, int]:
    """Drop physiologically impossible RR intervals.

    A missed beat doubles an interval and an extra detection halves it; both would
    drag a mean heart rate somewhere clinically misleading. Returns the surviving
    intervals and how many were dropped.
    """
    rr_s = np.asarray(rr_s, dtype=np.float64)
    if rr_s.size == 0:
        return rr_s, 0
    keep = (rr_s >= MIN_PLAUSIBLE_RR_S) & (rr_s <= MAX_PLAUSIBLE_RR_S)
    return rr_s[keep], int(np.sum(~keep))


def classify_rate(bpm: float, limits: RateLimits = DEFAULT_LIMITS) -> str:
    """Label a heart rate against the alarm thresholds."""
    if not np.isfinite(bpm) or bpm <= 0:
        return "insufficient data"
    if bpm < limits.brady:
        return "bradycardia"
    if bpm > limits.tachy:
        return "tachycardia"
    return "normal"


def sdnn_ms(rr_s: np.ndarray) -> float:
    """Standard deviation of RR intervals, in ms - overall heart-rate variability."""
    rr_s = np.asarray(rr_s, dtype=np.float64)
    return float(np.std(rr_s, ddof=1) * 1000.0) if rr_s.size >= 2 else 0.0


def rmssd_ms(rr_s: np.ndarray) -> float:
    """RMS of successive RR differences, in ms - short-term (beat-to-beat) HRV."""
    rr_s = np.asarray(rr_s, dtype=np.float64)
    if rr_s.size < 2:
        return 0.0
    return float(np.sqrt(np.mean(np.diff(rr_s) ** 2)) * 1000.0)


def heart_rate(
    r_peaks: np.ndarray,
    fs: float,
    limits: RateLimits = DEFAULT_LIMITS,
) -> HeartRateResult:
    """Compute heart rate and variability from detected beats.

    The mean rate is the **median** of the instantaneous rates rather than
    ``60 / mean(RR)``: a median shrugs off the occasional bad interval that survives
    plausibility filtering, which matters because one spurious beat should never move
    a displayed heart rate far enough to trip an alarm.
    """
    peaks = np.asarray(r_peaks, dtype=np.int64)
    rr_all = rr_intervals(peaks, fs)
    rr, n_rejected = plausible_rr(rr_all)

    if rr.size == 0:
        return HeartRateResult(
            bpm=0.0,
            instantaneous_bpm=np.array([], dtype=np.float64),
            rr_s=rr,
            n_beats=int(peaks.size),
            n_rejected=n_rejected,
            sdnn_ms=0.0,
            rmssd_ms=0.0,
            classification="insufficient data",
        )

    inst = 60.0 / rr
    bpm = float(np.median(inst))
    return HeartRateResult(
        bpm=bpm,
        instantaneous_bpm=inst,
        rr_s=rr,
        n_beats=int(peaks.size),
        n_rejected=n_rejected,
        sdnn_ms=sdnn_ms(rr),
        rmssd_ms=rmssd_ms(rr),
        classification=classify_rate(bpm, limits),
    )


def rolling_heart_rate(
    r_peaks: np.ndarray,
    fs: float,
    window_s: float = 10.0,
    step_s: float = 1.0,
    limits: RateLimits = DEFAULT_LIMITS,
) -> tuple[np.ndarray, np.ndarray]:
    """Heart rate over a sliding window - what the dashboard trend line plots.

    Returns ``(times_s, bpm)``; windows holding fewer than two beats yield NaN so
    gaps stay visible on a chart instead of being silently interpolated over.
    """
    peaks = np.asarray(r_peaks, dtype=np.int64)
    if peaks.size == 0 or window_s <= 0 or step_s <= 0:
        return np.array([]), np.array([])

    end_s = float(peaks[-1]) / fs
    starts = np.arange(0.0, max(end_s - window_s, 0.0) + step_s, step_s)
    times, rates = [], []
    peak_times = peaks / fs

    for start in starts:
        in_window = peaks[(peak_times >= start) & (peak_times < start + window_s)]
        hr = heart_rate(in_window, fs, limits)
        times.append(start + window_s / 2.0)  # label the window at its centre
        rates.append(hr.bpm if hr.valid else np.nan)

    return np.asarray(times), np.asarray(rates)


# --------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------


def _self_test() -> None:
    """Run: python dsp/vitals.py"""
    fs = 360.0
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    def peaks_at(bpm: float, n: int) -> np.ndarray:
        step = 60.0 / bpm * fs
        return np.round(np.arange(n) * step).astype(np.int64)

    # --- exact rates ---
    for bpm in (45.0, 60.0, 72.0, 100.0, 180.0):
        hr = heart_rate(peaks_at(bpm, 40), fs)
        check(f"{bpm:g} bpm recovered exactly", abs(hr.bpm - bpm) < 0.5, f"{hr.bpm:.2f} bpm")

    # --- classification boundaries ---
    for bpm, expect in ((50, "bradycardia"), (59.9, "bradycardia"), (60, "normal"), (80, "normal"), (100, "normal"), (110, "tachycardia")):
        got = classify_rate(bpm)
        check(f"{bpm} bpm classified {expect}", got == expect, got)
    check("zero rate is 'insufficient data'", classify_rate(0) == "insufficient data", "ok")
    check("NaN rate is 'insufficient data'", classify_rate(float("nan")) == "insufficient data", "ok")

    # --- artefact rejection ---
    p = peaks_at(60, 20).tolist()
    p.insert(10, p[9] + 20)  # extra detection 55 ms after a beat -> impossible RR
    hr = heart_rate(np.array(p, dtype=np.int64), fs)
    check("spurious extra beat rejected", hr.n_rejected >= 1, f"{hr.n_rejected} RR dropped")
    check("...and rate stays correct", abs(hr.bpm - 60) < 1.0, f"{hr.bpm:.2f} bpm")

    missed = np.delete(peaks_at(60, 30), 15)  # drop a beat -> one double-length RR
    hr_m = heart_rate(missed, fs)
    check("missed beat does not skew median rate", abs(hr_m.bpm - 60) < 1.0, f"{hr_m.bpm:.2f} bpm")

    # --- HRV metrics ---
    steady = heart_rate(peaks_at(60, 50), fs)
    check("perfectly regular rhythm: SDNN ~ 0", steady.sdnn_ms < 1.0, f"{steady.sdnn_ms:.3f} ms")
    check("perfectly regular rhythm: RMSSD ~ 0", steady.rmssd_ms < 1.0, f"{steady.rmssd_ms:.3f} ms")

    rng = np.random.default_rng(0)
    jittered = np.cumsum(rng.normal(360, 25, 200)).astype(np.int64)  # 60 bpm +/- jitter
    hrv = heart_rate(jittered, fs)
    check("jittered rhythm: SDNN > 40 ms", hrv.sdnn_ms > 40, f"{hrv.sdnn_ms:.1f} ms")
    check("jittered rhythm: RMSSD > 40 ms", hrv.rmssd_ms > 40, f"{hrv.rmssd_ms:.1f} ms")

    # --- degenerate inputs ---
    for arr, label in ((np.array([], dtype=np.int64), "no beats"), (np.array([100], dtype=np.int64), "one beat")):
        hr_d = heart_rate(arr, fs)
        check(
            f"handles {label}",
            hr_d.bpm == 0.0 and hr_d.classification == "insufficient data" and not hr_d.valid,
            "insufficient data",
        )

    try:
        rr_intervals(np.array([0, 100]), fs=0)
        check("rejects fs = 0", False, "no error raised")
    except ValueError:
        check("rejects fs = 0", True, "ValueError")
    try:
        RateLimits(brady=120, tachy=100)
        check("rejects brady > tachy limits", False, "no error raised")
    except ValueError:
        check("rejects brady > tachy limits", True, "ValueError")

    # --- rolling window ---
    times, rates = rolling_heart_rate(peaks_at(72, 200), fs, window_s=10, step_s=5)
    finite = rates[np.isfinite(rates)]
    check("rolling HR returns a trend", times.size > 5, f"{times.size} windows")
    check(
        "rolling HR tracks the true rate",
        finite.size > 0 and abs(float(np.median(finite)) - 72) < 1.0,
        f"median {np.median(finite):.2f} bpm" if finite.size else "no finite values",
    )
    t_empty, r_empty = rolling_heart_rate(np.array([], dtype=np.int64), fs)
    check("rolling HR handles no beats", t_empty.size == 0 and r_empty.size == 0, "empty")

    # --- a gap in the beats should surface as NaN, not be smoothed away ---
    gapped = np.concatenate([peaks_at(60, 20), peaks_at(60, 20)[-1] + int(30 * fs) + peaks_at(60, 20)])
    _, gap_rates = rolling_heart_rate(gapped, fs, window_s=10, step_s=5)
    check("gap in beats shows as NaN", bool(np.isnan(gap_rates).any()), f"{int(np.isnan(gap_rates).sum())} NaN windows")

    width = max(len(n_) for n_, _, _ in checks)
    passed = sum(ok for _, ok, _ in checks)
    print("\nvitals.py self-test\n" + "-" * (width + 24))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print("-" * (width + 24))
    print(f"{passed}/{len(checks)} checks passed\n")
    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
