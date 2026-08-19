"""Synthetic ECG generator.

Produces an ECG-like waveform **with known R-peak locations**, which real recordings
cannot give for free. Two uses:

* unit-testing the QRS detector against exact ground truth, with noise dialled up or
  down on demand (MIT-BIH gives realism, this gives control);
* feeding the firmware simulator and the ESP32 self-test mode when no patient signal
  and no dataset are available.

The morphology is a sum of Gaussians per beat (P, Q, R, S, T) - not a physiological
model, but it has the right shape, timing and frequency content for testing signal
processing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_FS = 360.0

# (name, centre offset from R peak in seconds, amplitude in mV, Gaussian sigma in s)
_BEAT_TEMPLATE: tuple[tuple[str, float, float, float], ...] = (
    ("P", -0.20, 0.12, 0.025),
    ("Q", -0.02, -0.15, 0.008),
    ("R", 0.00, 1.00, 0.009),
    ("S", 0.025, -0.25, 0.010),
    ("T", 0.30, 0.30, 0.045),
)


@dataclass(frozen=True)
class SynthConfig:
    """Parameters for a synthetic recording."""

    duration_s: float = 30.0
    fs: float = DEFAULT_FS
    hr_bpm: float = 72.0
    hrv_frac: float = 0.0  # beat-to-beat RR jitter as a fraction of the mean RR
    noise_std: float = 0.0  # white/EMG noise, mV RMS
    mains_amp: float = 0.0  # 50 Hz interference amplitude, mV
    mains_freq: float = 50.0
    wander_amp: float = 0.0  # baseline wander amplitude, mV
    wander_freq: float = 0.15  # respiration-rate drift, Hz
    dc_offset: float = 0.0  # electrode half-cell potential, mV
    seed: int = 0

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError(f"duration_s must be positive, got {self.duration_s}")
        if not 20.0 <= self.hr_bpm <= 300.0:
            raise ValueError(f"hr_bpm out of plausible range: {self.hr_bpm}")
        if self.fs <= 0:
            raise ValueError(f"fs must be positive, got {self.fs}")
        if not 0.0 <= self.hrv_frac < 0.5:
            raise ValueError(f"hrv_frac must be in [0, 0.5), got {self.hrv_frac}")


def synthetic_ecg(cfg: SynthConfig | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Generate a synthetic ECG.

    Returns
    -------
    signal : np.ndarray
        The waveform, in mV, length ``round(duration_s * fs)``.
    r_peaks : np.ndarray
        Sample indices of the true R peaks - the ground truth for detector tests.
    """
    cfg = cfg or SynthConfig()
    rng = np.random.default_rng(cfg.seed)

    n = int(round(cfg.duration_s * cfg.fs))
    t = np.arange(n) / cfg.fs
    sig = np.zeros(n, dtype=np.float64)

    # --- R-peak times: start half a beat in so the first beat is complete ---
    mean_rr = 60.0 / cfg.hr_bpm
    peak_times: list[float] = []
    current = mean_rr * 0.5
    while current < cfg.duration_s - 0.4:  # leave room for the trailing T wave
        peak_times.append(current)
        rr = mean_rr
        if cfg.hrv_frac > 0:
            rr *= 1.0 + rng.normal(0.0, cfg.hrv_frac)
            rr = float(np.clip(rr, 0.25, 3.0))  # keep RR physiologically sane
        current += rr

    # --- lay down the beat morphology ---
    for pt in peak_times:
        for _name, offset, amp, sigma in _BEAT_TEMPLATE:
            centre = pt + offset
            # Evaluate each Gaussian only within +/-4 sigma; beyond that it is
            # numerically zero, and this keeps generation O(beats) not O(beats * n).
            lo = max(0, int((centre - 4 * sigma) * cfg.fs))
            hi = min(n, int((centre + 4 * sigma) * cfg.fs) + 1)
            if lo >= hi:
                continue
            seg = t[lo:hi]
            sig[lo:hi] += amp * np.exp(-0.5 * ((seg - centre) / sigma) ** 2)

    # --- corruptions, in the order they occur physically ---
    if cfg.wander_amp:
        sig += cfg.wander_amp * np.sin(2 * np.pi * cfg.wander_freq * t)
    if cfg.mains_amp:
        sig += cfg.mains_amp * np.sin(2 * np.pi * cfg.mains_freq * t)
    if cfg.noise_std:
        sig += rng.normal(0.0, cfg.noise_std, n)
    if cfg.dc_offset:
        sig += cfg.dc_offset

    r_peaks = np.array([int(round(pt * cfg.fs)) for pt in peak_times], dtype=np.int64)
    r_peaks = r_peaks[(r_peaks >= 0) & (r_peaks < n)]
    return sig, r_peaks


def _self_test() -> None:
    """Run: python dsp/synth.py"""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    # --- clean signal, basic shape ---
    sig, peaks = synthetic_ecg(SynthConfig(duration_s=10, hr_bpm=60))
    check("length matches duration * fs", sig.size == 3600, f"{sig.size} samples")
    check("60 bpm over 10 s gives ~10 beats", 8 <= peaks.size <= 10, f"{peaks.size} beats")
    check("no NaN/Inf", not (np.isnan(sig).any() or np.isinf(sig).any()), "clean")

    # --- R peaks must actually be the local maxima ---
    ok_peak = all(
        sig[p] == max(sig[max(0, p - 5) : p + 6]) for p in peaks
    )
    check("R peak index is the local maximum", ok_peak, f"checked {peaks.size} peaks")
    check("R amplitude ~1 mV", 0.9 < float(np.max(sig[peaks])) < 1.1, f"{np.max(sig[peaks]):.3f} mV")

    # --- RR intervals match the requested heart rate ---
    rr = np.diff(peaks) / DEFAULT_FS
    hr = 60.0 / rr.mean()
    check("measured HR matches requested 60 bpm", abs(hr - 60) < 0.5, f"{hr:.2f} bpm")

    sig2, peaks2 = synthetic_ecg(SynthConfig(duration_s=20, hr_bpm=150))
    hr2 = 60.0 / (np.diff(peaks2) / DEFAULT_FS).mean()
    check("measured HR matches requested 150 bpm", abs(hr2 - 150) < 1.0, f"{hr2:.2f} bpm")

    # --- HRV widens the RR spread but keeps the mean ---
    _, peaks3 = synthetic_ecg(SynthConfig(duration_s=60, hr_bpm=72, hrv_frac=0.08, seed=1))
    rr3 = np.diff(peaks3) / DEFAULT_FS
    check(
        "HRV produces RR variation (SDNN > 20 ms)",
        np.std(rr3) * 1000 > 20,
        f"SDNN = {np.std(rr3) * 1000:.1f} ms",
    )
    check(
        "HRV keeps mean HR near target",
        abs(60.0 / rr3.mean() - 72) < 4,
        f"{60.0 / rr3.mean():.1f} bpm",
    )

    # --- corruptions land where they should ---
    noisy, _ = synthetic_ecg(
        SynthConfig(duration_s=10, mains_amp=0.5, wander_amp=1.0, noise_std=0.05, dc_offset=0.8)
    )
    from scipy import signal as _sp

    freqs, psd = _sp.welch(noisy, fs=DEFAULT_FS, nperseg=2048)
    mains_bin = int(np.argmin(np.abs(freqs - 50.0)))
    check(
        "50 Hz interference is the dominant spectral peak above 30 Hz",
        mains_bin == int(np.argmax(psd[freqs > 30]) + np.sum(freqs <= 30)),
        f"peak at {freqs[mains_bin]:.1f} Hz",
    )
    check("DC offset present in raw signal", abs(float(noisy.mean())) > 0.5, f"mean = {noisy.mean():.3f}")

    # --- determinism ---
    a, pa = synthetic_ecg(SynthConfig(duration_s=5, noise_std=0.05, seed=42))
    b, pb = synthetic_ecg(SynthConfig(duration_s=5, noise_std=0.05, seed=42))
    check("same seed reproduces the signal", np.array_equal(a, b) and np.array_equal(pa, pb), "identical")

    # --- input validation ---
    for kwargs, label in (
        (dict(duration_s=0), "duration_s = 0"),
        (dict(hr_bpm=5), "hr_bpm = 5"),
        (dict(hrv_frac=0.9), "hrv_frac = 0.9"),
    ):
        try:
            SynthConfig(**kwargs)
            check(f"rejects invalid config ({label})", False, "no error raised")
        except ValueError:
            check(f"rejects invalid config ({label})", True, "ValueError")

    width = max(len(n_) for n_, _, _ in checks)
    passed = sum(ok for _, ok, _ in checks)
    print(f"\nsynth.py self-test\n" + "-" * (width + 22))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print("-" * (width + 22))
    print(f"{passed}/{len(checks)} checks passed\n")
    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
