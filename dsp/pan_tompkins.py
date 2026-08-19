"""Pan-Tompkins QRS detection.

Implements the classic real-time QRS detection algorithm (Pan & Tompkins, *IEEE
Trans. Biomed. Eng.*, 1985), still the reference baseline against which beat
detectors are compared.

The pipeline turns the ECG into a signal where each QRS complex becomes one large,
isolated bump, then adapts its decision threshold as the recording goes on:

```
ECG ─► bandpass 5-15 Hz ─► derivative ─► square ─► moving-window integrate ─► threshold
       (isolate QRS         (slope is    (all      (150 ms: merges the        (adaptive,
        energy; reject       what makes   positive,  QRS's multiple slope       with search
        P/T waves and        a QRS a      emphasise  peaks into one bump)       back and
        muscle noise)        QRS)         big slopes)                          T-wave logic)
```

Two design notes worth knowing for their own sake:

* **The detection bandpass is not the display bandpass.** Beat detection uses
  5-15 Hz, where QRS energy peaks, deliberately distorting the waveform to make beats
  stand out. The 0.5-40 Hz chain in :mod:`filters` is what a clinician would look at.
  Separating the two paths is standard practice in monitors - one is optimised for
  the eye, the other for the algorithm.
* **The threshold adapts, it is not fixed.** ECG amplitude varies with electrode
  contact, patient, and posture, so any fixed threshold either misses beats or fires
  on noise. Running estimates of signal and noise peak levels track that drift.

This module is the *offline* (zero-phase) form used for dataset validation; the
causal port that runs on live streams is derived from it in Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from filters import DEFAULT_FS, FilterConfig, clean_offline


@dataclass(frozen=True)
class QrsConfig:
    """Pan-Tompkins tuning parameters. Defaults follow the original paper."""

    fs: float = DEFAULT_FS
    bp_low: float = 5.0  # Hz - detection bandpass, QRS energy band
    bp_high: float = 15.0  # Hz
    bp_order: int = 3
    integration_ms: float = 150.0  # ~ the width of a wide QRS complex
    refractory_ms: float = 200.0  # physiological floor: no two beats closer than this
    twave_ms: float = 360.0  # below this RR, check whether it is really a T wave
    twave_slope_frac: float = 0.5  # a T wave's slope is well under half a QRS's
    learning_s: float = 2.0  # initial window used to seed the thresholds
    search_back: bool = True  # re-scan with a lower threshold after a long gap
    fiducial_ms: float = 100.0  # window for locating the exact R peak
    # Flat-line gate, in the units of the input (mV here). An adaptive threshold
    # scales to whatever it is given, so on a dead input - detached electrode, railed
    # amplifier - it will happily lock onto microvolt numerical residue and report a
    # heart rate. Real monitors gate on absolute signal amplitude for this reason;
    # a genuine ECG lead is >= 0.1 mV, so 0.01 mV is far below signal and far above
    # numerical noise.
    min_signal_std: float = 0.01

    def __post_init__(self) -> None:
        nyquist = self.fs / 2.0
        if not 0 < self.bp_low < self.bp_high < nyquist:
            raise ValueError(
                f"need 0 < bp_low ({self.bp_low}) < bp_high ({self.bp_high}) < {nyquist}"
            )
        if self.min_signal_std < 0:
            raise ValueError(f"min_signal_std must be >= 0, got {self.min_signal_std}")
        for name in ("integration_ms", "refractory_ms", "twave_ms", "fiducial_ms"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if not 0 < self.twave_slope_frac < 1:
            raise ValueError(f"twave_slope_frac must be in (0, 1), got {self.twave_slope_frac}")


@dataclass
class QrsResult:
    """Detector output plus every intermediate stage, for plots and debugging."""

    r_peaks: np.ndarray  # sample indices of detected R peaks
    fs: float
    stages: dict[str, np.ndarray] = field(default_factory=dict)
    n_searchback: int = 0  # beats recovered by search-back
    n_twave_rejected: int = 0  # peaks discarded as T waves
    #: The detector's final signal-to-noise estimate, spki / npki - what the adaptive
    #: threshold actually experienced, and therefore what its accuracy depended on.
    snr: float = float("inf")

    @property
    def rr_intervals_s(self) -> np.ndarray:
        """Intervals between successive detected beats, in seconds."""
        return np.diff(self.r_peaks) / self.fs

    def __len__(self) -> int:
        return int(self.r_peaks.size)


# --------------------------------------------------------------------------------
# Pipeline stages
# --------------------------------------------------------------------------------


def _detection_bandpass(x: np.ndarray, cfg: QrsConfig) -> np.ndarray:
    """5-15 Hz zero-phase bandpass: isolate the QRS energy band."""
    sos = signal.butter(
        cfg.bp_order, [cfg.bp_low, cfg.bp_high], btype="bandpass", fs=cfg.fs, output="sos"
    )
    return signal.sosfiltfilt(sos, x)


def _derivative(x: np.ndarray, fs: float) -> np.ndarray:
    """Five-point derivative - emphasises the steep QRS slopes.

    ``y[n] = (2x[n+1] + x[n+2] - x[n-2] - 2x[n-1]) / (8T)``, the paper's kernel.
    """
    kernel = np.array([1.0, 2.0, 0.0, -2.0, -1.0]) * (fs / 8.0)
    return np.convolve(x, kernel, mode="same")


def _square(x: np.ndarray) -> np.ndarray:
    """Pointwise squaring: makes everything positive and amplifies large slopes."""
    return x * x


def _integrate(x: np.ndarray, cfg: QrsConfig) -> np.ndarray:
    """Moving-window integration - merges the QRS's slope peaks into a single bump.

    The window is centred (``mode="same"``) because this offline form is zero-phase;
    a real-time implementation uses a trailing window and accepts the resulting
    half-window group delay.
    """
    width = max(1, int(round(cfg.integration_ms * cfg.fs / 1000.0)))
    return np.convolve(x, np.ones(width) / width, mode="same")


# --------------------------------------------------------------------------------
# Adaptive decision stage
# --------------------------------------------------------------------------------


def _locate_r_peak(ecg: np.ndarray, centre: int, cfg: QrsConfig) -> int:
    """Snap a detection to the true R peak by searching the filtered ECG nearby.

    Uses the largest *absolute* deflection so that inverted leads (where the R wave
    points down, e.g. MIT-BIH record 108) are handled without special-casing.
    """
    half = max(1, int(round(cfg.fiducial_ms * cfg.fs / 1000.0)))
    lo, hi = max(0, centre - half), min(ecg.size, centre + half + 1)
    if lo >= hi:
        return int(centre)
    return int(lo + np.argmax(np.abs(ecg[lo:hi])))


def detect_qrs(
    ecg: np.ndarray,
    cfg: QrsConfig | None = None,
    *,
    prefilter: bool = True,
) -> QrsResult:
    """Detect QRS complexes and return their R-peak sample indices.

    Parameters
    ----------
    ecg
        Raw or pre-cleaned single-lead ECG.
    cfg
        Detector configuration; defaults follow the original paper.
    prefilter
        Apply the 0.5-40 Hz cleaning chain from :mod:`filters` first. Leave on for
        raw input; turn off if the signal is already cleaned.
    """
    cfg = cfg or QrsConfig()
    x = np.asarray(ecg, dtype=np.float64)
    if x.ndim != 1:
        raise ValueError(f"expected a 1-D signal, got shape {x.shape}")

    # Too short to hold even one beat plus the learning window: nothing to detect.
    min_samples = int(cfg.fs * 0.5)
    if x.size < min_samples:
        return QrsResult(np.array([], dtype=np.int64), cfg.fs)

    cleaned = clean_offline(x, FilterConfig(fs=cfg.fs)) if prefilter else x
    banded = _detection_bandpass(cleaned, cfg)
    deriv = _derivative(banded, cfg.fs)
    squared = _square(deriv)
    integrated = _integrate(squared, cfg)

    stages = {
        "cleaned": cleaned,
        "bandpassed": banded,
        "derivative": deriv,
        "squared": squared,
        "integrated": integrated,
    }

    # A flat or dead signal - detached electrode, railed amplifier, asystole - has no
    # beats to find. This gate must come before the adaptive threshold, which would
    # otherwise scale itself down to numerical residue and invent a heart rate.
    if not np.any(integrated > 0) or float(np.std(cleaned)) < cfg.min_signal_std:
        return QrsResult(np.array([], dtype=np.int64), cfg.fs, stages)

    refractory = max(1, int(round(cfg.refractory_ms * cfg.fs / 1000.0)))
    twave_gap = int(round(cfg.twave_ms * cfg.fs / 1000.0))

    # Candidate peaks: local maxima no closer together than the refractory period.
    # Screening candidates first (rather than walking every sample) keeps the
    # decision logic readable without changing the algorithm's behaviour.
    cand_idx, _ = signal.find_peaks(integrated, distance=refractory)
    if cand_idx.size == 0:
        return QrsResult(np.array([], dtype=np.int64), cfg.fs, stages)
    cand_val = integrated[cand_idx]

    # --- seed the running peak estimates from an initial learning window ---
    learn_n = min(int(cfg.learning_s * cfg.fs), integrated.size)
    learn = integrated[:learn_n]
    spki = float(np.max(learn)) * 0.25  # running estimate of QRS peak level
    npki = float(np.mean(learn)) * 0.5  # running estimate of noise peak level

    def threshold_i1() -> float:
        return npki + 0.25 * (spki - npki)

    qrs_idx: list[int] = []  # detections, in integrated-signal coordinates
    rr_recent: list[float] = []  # last 8 RR intervals (samples)
    rr_ok: list[float] = []  # last 8 RR intervals inside the accepted band
    n_searchback = 0
    n_twave = 0

    def slope_at(i: int) -> float:
        """Peak absolute slope near a candidate - a QRS is steep, a T wave is not."""
        half = max(1, int(0.035 * cfg.fs))
        lo, hi = max(0, i - half), min(deriv.size, i + half + 1)
        return float(np.max(np.abs(deriv[lo:hi]))) if hi > lo else 0.0

    def accept(i: int, val: float, *, via_searchback: bool = False) -> None:
        nonlocal spki
        # Search-back detections are weaker by definition, so they nudge the signal
        # estimate less (the paper's 0.25 vs 0.125 weighting).
        spki = 0.25 * val + 0.75 * spki if via_searchback else 0.125 * val + 0.875 * spki
        if qrs_idx:
            rr = float(i - qrs_idx[-1])
            rr_recent.append(rr)
            del rr_recent[:-8]
            if rr_ok:
                lo, hi = 0.92 * float(np.mean(rr_ok)), 1.16 * float(np.mean(rr_ok))
                if lo <= rr <= hi:
                    rr_ok.append(rr)
                    del rr_ok[:-8]
            else:
                rr_ok.append(rr)
        qrs_idx.append(i)

    for k, (idx, val) in enumerate(zip(cand_idx.tolist(), cand_val.tolist())):
        idx = int(idx)

        # --- search back: a gap longer than 166% of the recent average means a beat
        # was probably missed, so re-scan that gap at the lower threshold ---
        if cfg.search_back and qrs_idx and rr_ok:
            missed_limit = 1.66 * float(np.mean(rr_ok))
            if (idx - qrs_idx[-1]) > missed_limit:
                lo_bound = qrs_idx[-1] + refractory
                gap = [
                    (int(ci), float(cv))
                    for ci, cv in zip(cand_idx[:k].tolist(), cand_val[:k].tolist())
                    if lo_bound < ci < idx and cv > 0.5 * threshold_i1()
                ]
                if gap:
                    best_i, best_v = max(gap, key=lambda p: p[1])
                    accept(best_i, best_v, via_searchback=True)
                    n_searchback += 1

        if val > threshold_i1():
            # --- T-wave discrimination: a "beat" arriving suspiciously soon after
            # the last one is a T wave unless it rises as steeply as a QRS ---
            if qrs_idx and (idx - qrs_idx[-1]) < twave_gap:
                if slope_at(idx) < cfg.twave_slope_frac * slope_at(qrs_idx[-1]):
                    npki = 0.125 * val + 0.875 * npki
                    n_twave += 1
                    continue
            accept(idx, val)
        else:
            npki = 0.125 * val + 0.875 * npki

    r_peaks = np.array(
        [_locate_r_peak(cleaned, i, cfg) for i in sorted(qrs_idx)], dtype=np.int64
    )
    # Snapping can collide two detections onto one peak; keep them strictly ordered.
    if r_peaks.size:
        r_peaks = np.unique(r_peaks)

    return QrsResult(
        r_peaks, cfg.fs, stages,
        n_searchback=n_searchback, n_twave_rejected=n_twave,
        snr=(spki / npki) if npki > 0 else float("inf"),
    )


# --------------------------------------------------------------------------------
# Detector scoring (used by validate.py and the tests below)
# --------------------------------------------------------------------------------


def energy_concentration(ecg: np.ndarray, cfg: QrsConfig | None = None) -> float:
    """How concentrated in time the QRS energy is, after width has been integrated away.

    The ratio of the 99th percentile to the median of the Pan-Tompkins integrated
    signal. A real ECG puts its energy into brief bursts, so the ratio is large; noise
    spreads energy evenly, so it approaches 1.

    Measuring it *after* the 150 ms integration is the point: QRS width stops mattering.
    Kurtosis of the raw signal, and the share of power in 5-15 Hz, both fall for a wide
    ventricular beat, which makes them flag arrhythmias as poor quality. Integration
    normalises width away, so a wide ventricular complex still produces a tall localised
    bump and this measure stays high. It separates *noisy* from *abnormal*, which the
    spectral measures cannot.
    """
    cfg = cfg or QrsConfig()
    x = np.asarray(ecg, dtype=np.float64)
    if x.size < 8 or not np.all(np.isfinite(x)):
        return 0.0
    integrated = _integrate(_square(_derivative(_detection_bandpass(x, cfg), cfg.fs)), cfg)
    median = float(np.median(integrated))
    if median <= 0:
        return float("inf") if np.any(integrated > 0) else 0.0
    return float(np.percentile(integrated, 99) / median)


def compare_to_reference(
    detected: np.ndarray,
    reference: np.ndarray,
    fs: float,
    tolerance_ms: float = 150.0,
) -> dict[str, float]:
    """Match detections against reference beats and score the result.

    A detection counts as a true positive if it falls within ``tolerance_ms`` of a
    reference beat (150 ms is the window used in the QRS-detector literature), and
    each reference beat can be matched at most once.

    Returns sensitivity (fraction of real beats found), positive predictivity
    (fraction of detections that were real), TP/FP/FN counts and the mean timing
    error of matched beats.
    """
    detected = np.asarray(detected, dtype=np.int64)
    reference = np.asarray(reference, dtype=np.int64)
    tol = tolerance_ms * fs / 1000.0

    matched_ref: set[int] = set()
    errors: list[float] = []
    tp = 0

    for d in detected:
        # Nearest unmatched reference beat within tolerance.
        best_j, best_dist = -1, tol + 1
        j = int(np.searchsorted(reference, d))
        for cand in (j - 1, j, j + 1):
            if 0 <= cand < reference.size and cand not in matched_ref:
                dist = abs(int(reference[cand]) - int(d))
                if dist < best_dist:
                    best_j, best_dist = cand, dist
        if best_j >= 0 and best_dist <= tol:
            matched_ref.add(best_j)
            errors.append(best_dist)
            tp += 1

    fp = int(detected.size - tp)
    fn = int(reference.size - tp)
    return {
        "tp": float(tp),
        "fp": float(fp),
        "fn": float(fn),
        "sensitivity": tp / (tp + fn) if (tp + fn) else 0.0,
        "ppv": tp / (tp + fp) if (tp + fp) else 0.0,
        "mean_error_ms": float(np.mean(errors) / fs * 1000.0) if errors else 0.0,
    }


# --------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------


def _self_test() -> None:
    """Run: python dsp/pan_tompkins.py"""
    from synth import SynthConfig, synthetic_ecg

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    def score(sc: SynthConfig) -> dict[str, float]:
        sig, truth = synthetic_ecg(sc)
        res = detect_qrs(sig, QrsConfig(fs=sc.fs))
        return compare_to_reference(res.r_peaks, truth, sc.fs)

    # --- clean signal: should be perfect ---
    m = score(SynthConfig(duration_s=60, hr_bpm=72))
    check("clean 72 bpm: Se = 100%", m["sensitivity"] == 1.0, f"Se={m['sensitivity']:.3f}")
    check("clean 72 bpm: PPV = 100%", m["ppv"] == 1.0, f"PPV={m['ppv']:.3f}, FP={m['fp']:.0f}")
    check("clean 72 bpm: timing error < 20 ms", m["mean_error_ms"] < 20, f"{m['mean_error_ms']:.1f} ms")

    # --- heart-rate extremes ---
    for hr, label in ((45, "bradycardia"), (150, "tachycardia"), (200, "extreme tachy")):
        mm = score(SynthConfig(duration_s=60, hr_bpm=hr))
        check(
            f"{label} ({hr} bpm): Se > 99%",
            mm["sensitivity"] > 0.99,
            f"Se={mm['sensitivity']:.3f} PPV={mm['ppv']:.3f}",
        )

    # --- corrupted signals: the whole point of the filter chain ---
    mains = score(SynthConfig(duration_s=60, hr_bpm=72, mains_amp=0.6))
    check("50 Hz mains at 60% of R: Se > 99%", mains["sensitivity"] > 0.99, f"Se={mains['sensitivity']:.3f}")

    wander = score(SynthConfig(duration_s=60, hr_bpm=72, wander_amp=1.5, dc_offset=1.0))
    check(
        "baseline wander 1.5x R amplitude: Se > 99%",
        wander["sensitivity"] > 0.99,
        f"Se={wander['sensitivity']:.3f}",
    )

    noisy = score(SynthConfig(duration_s=60, hr_bpm=72, noise_std=0.08, seed=3))
    check(
        "EMG noise (SNR ~22 dB): Se > 98%",
        noisy["sensitivity"] > 0.98,
        f"Se={noisy['sensitivity']:.3f} PPV={noisy['ppv']:.3f}",
    )

    combined = score(
        SynthConfig(duration_s=60, hr_bpm=72, mains_amp=0.4, wander_amp=1.0, noise_std=0.05, dc_offset=0.7, seed=5)
    )
    check(
        "all corruptions together: Se > 98%",
        combined["sensitivity"] > 0.98,
        f"Se={combined['sensitivity']:.3f} PPV={combined['ppv']:.3f}",
    )

    # --- irregular rhythm ---
    hrv = score(SynthConfig(duration_s=120, hr_bpm=72, hrv_frac=0.15, seed=7))
    check(
        "irregular rhythm (HRV 15%): Se > 98%",
        hrv["sensitivity"] > 0.98,
        f"Se={hrv['sensitivity']:.3f} PPV={hrv['ppv']:.3f}",
    )

    # --- inverted lead ---
    sig_i, truth_i = synthetic_ecg(SynthConfig(duration_s=60, hr_bpm=72))
    inv = compare_to_reference(detect_qrs(-sig_i).r_peaks, truth_i, DEFAULT_FS)
    check("inverted lead: Se > 99%", inv["sensitivity"] > 0.99, f"Se={inv['sensitivity']:.3f}")

    # --- T-wave rejection: tall T waves must not be counted as beats ---
    sig_t, truth_t = synthetic_ecg(SynthConfig(duration_s=60, hr_bpm=72))
    res_t = detect_qrs(sig_t)
    check(
        "no extra beats from T waves",
        len(res_t) <= truth_t.size + 1,
        f"{len(res_t)} detected vs {truth_t.size} true, {res_t.n_twave_rejected} T waves rejected",
    )

    # --- degenerate inputs must not crash ---
    for sig_d, label in (
        (np.array([]), "empty signal"),
        (np.zeros(3600), "flat line (leads off)"),
        (np.ones(100), "signal shorter than one beat"),
        (np.full(3600, 5.0), "constant DC"),
        (np.random.default_rng(0).normal(0, 0.002, 3600), "microvolt noise (dead lead)"),
    ):
        try:
            r = detect_qrs(sig_d)
            check(f"handles {label}", len(r) == 0, f"{len(r)} beats detected")
        except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
            check(f"handles {label}", False, f"{type(exc).__name__}: {exc}")

    # --- scoring function sanity ---
    ref = np.array([100, 200, 300, 400], dtype=np.int64)
    perfect = compare_to_reference(ref, ref, 360.0)
    check("scorer: identical inputs give Se=PPV=1", perfect["sensitivity"] == 1.0 and perfect["ppv"] == 1.0, "ok")
    partial = compare_to_reference(np.array([100, 300]), ref, 360.0)
    check(
        "scorer: 2 of 4 found gives Se=0.5, PPV=1",
        partial["sensitivity"] == 0.5 and partial["ppv"] == 1.0,
        f"Se={partial['sensitivity']}, PPV={partial['ppv']}",
    )
    spurious = compare_to_reference(np.array([100, 150, 200, 300, 400]), ref, 360.0, tolerance_ms=50)
    check(
        "scorer: one spurious detection counts as FP",
        spurious["fp"] == 1.0 and spurious["sensitivity"] == 1.0,
        f"FP={spurious['fp']:.0f}",
    )

    # --- config validation ---
    for kwargs, label in ((dict(bp_low=20.0, bp_high=15.0), "bp_low > bp_high"), (dict(refractory_ms=0), "refractory = 0")):
        try:
            QrsConfig(**kwargs)
            check(f"rejects invalid config ({label})", False, "no error raised")
        except ValueError:
            check(f"rejects invalid config ({label})", True, "ValueError")

    width = max(len(n_) for n_, _, _ in checks)
    passed = sum(ok for _, ok, _ in checks)
    print("\npan_tompkins.py self-test\n" + "-" * (width + 34))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print("-" * (width + 34))
    print(f"{passed}/{len(checks)} checks passed\n")
    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
