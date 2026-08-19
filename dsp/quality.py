"""Signal quality: deciding when the monitor should decline to answer.

`docs/verification.md` names this module as the right fix for records 108 and 203, and
says so in the section explaining why their numbers were published rather than tuned
away (D-10). The detector's failure on those recordings is not a threshold that wants
adjusting - it is a signal a clinician would also refuse to read. What was missing was
the device's ability to say so.

Three measures, each answering a different question about whether there is an ECG here:

* **pSQI** - what fraction of in-band power sits in 5-15 Hz, where QRS energy lives.
  Low when the signal is dominated by something that is not a QRS complex.
* **basSQI** - what fraction of power is *not* below 1 Hz. Low under baseline wander,
  which is the dominant artefact in record 108.
* **kSQI** - kurtosis. A clean ECG is a flat line interrupted by sharp spikes, which is
  extremely peaked; Gaussian noise has a kurtosis of 3. This turns out to be the
  strongest of the three, separating the clean records (median 25-32) from the noisy
  ones (median 8-13) with almost no overlap.

None of them asks whether the *detections* agree with each other. That is deliberate:
twice now this project has been caught by a self-consistent artefact - the fabricated
190 bpm after electrode reconnection (D-22) and the false desaturation under motion
(D-32) - and both times the checks that failed were consistency checks. These measure
properties of the waveform instead.

**Where the thresholds come from.** The `good` boundaries are the 1st percentile of each
measure over every 4-second window of the four clean records (100, 101, 103, 115) -
3,604 windows. The noisy records were not looked at when setting them, so their results
are held out rather than fitted. A window is `good` only if all three measures pass;
measured afterwards, that is true of 98-99.8 % of windows on the clean records and 0-0.1 %
on the noisy ones.

`unusable` - the label that actually withholds a number - is not fitted to any record.
It fires on the absolute gates (flat line, amplitude outside the front end's range,
non-finite samples), on a kurtosis no higher than Gaussian noise, or when *all three*
measures fail at once. Any single measure can fail for reasons that are not quality; all
three failing together means nothing here looks like an ECG.

**Why the three-measure vote does not withhold numbers.** It was built to, and measuring
it showed it must not. Both kSQI and pSQI are morphology-dependent by construction: a
ventricular beat has a wide QRS, which is less peaked (lower kurtosis) and carries less
of its energy in 5-15 Hz (lower pSQI). Measured across the database, the *arrhythmic*
records fail these measures more often than the genuinely noisy ones - record 119
(ventricular bigeminy) fails on 60-74 % of windows against record 108's 19-27 % for pSQI
and basSQI, despite 108 having severe baseline artefact and 119 having a clean signal.

A monitor that withheld its reading whenever those measures failed would go quiet exactly
when the patient started throwing ventricular beats. That is the opposite of what the
device is for, and it is D-10's mistake in a new disguise: gating on this vote would have
raised the published sensitivity and positive predictivity by suppressing the beats that
are hardest and most clinically important.

So the responsibilities are split:

* **`unusable` - withholds the number.** Fires only on evidence that is not
  morphology-dependent: a flat line, an amplitude outside the front end's range, a
  non-finite sample, or a kurtosis no higher than Gaussian noise. None of these is fitted
  to any record, and none of them can be triggered by an unusual heartbeat.
* **`good` / `poor` - shown, never withheld.** The three-measure vote, surfaced to the
  clinician as an indication of how clean the trace is. Useful information; not grounds
  for the device to fall silent.

A production system would want a per-patient adaptive baseline, and a separate beat
classifier, so that "the signal is bad" and "the rhythm is abnormal" stop being the same
measurement. Distinguishing them properly is beyond amplitude and spectral statistics.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal as sps
from scipy import stats

from filters import DEFAULT_FS
from pan_tompkins import QrsConfig as _QrsConfig
from pan_tompkins import energy_concentration

#: Derived from the 1st percentile of 3,604 four-second windows across MIT-BIH records
#: 100, 101, 103 and 115. Reproduce with: python dsp/quality.py --calibrate
GOOD_PSQI = 0.386
GOOD_BASSQI = 0.880
GOOD_KSQI = 20.76

#: Not fitted. Gaussian noise has a kurtosis of 3, so a signal that is no more peaked
#: than 3.5 has no QRS structure in it, whatever else is true.
UNUSABLE_KSQI = 3.5

#: QRS energy concentration below which the heart rate stops meeting SR-01.
#:
#: Derived from the requirement rather than from the recordings: sweeping additive noise
#: over synthetic ECG with a known rate, the measured heart-rate error stays at 0.00 bpm
#: down to a concentration of 15.9 and jumps past 24 bpm at 13.9. The threshold sits
#: between them. That makes this the one place where the quality metric and the accuracy
#: requirement are the same statement - below this, the device cannot meet SR-01, so it
#: must not answer.
#:
#: Crucially it is morphology-independent: the arrhythmic records sit far above it
#: (record 119 at the 5th percentile is 34, record 106 is 60), so a patient throwing
#: ventricular beats does not silence the monitor.
UNUSABLE_CONCENTRATION = 15.0

#: The detector's own signal-to-noise estimate (spki/npki) below which a low energy
#: concentration means "noise" rather than "fast rhythm". The concentration statistic is
#: rate-confounded: above ~125 bpm the QRS bumps crowd together, the median of the
#: integrated signal rises, and a clean tachycardia scores below the concentration gate -
#: silencing the monitor for exactly the rhythm it exists to alarm on (the D-35 trap
#: reappearing along the rate axis). Withholding requires BOTH conditions. Measured on
#: the streaming detector: clean 130 bpm has SNR ~32, clean 150 ~13; every noise case
#: with a wrong rate scored <= 5.7. A clean ~180 bpm still scores ~5.7 and stays
#: withheld - a documented limitation, not a silent one (D-51).
UNUSABLE_SNR = 8.0

#: A genuine ECG lead is at least ~0.1 mV peak to peak; below 0.01 mV the input is a
#: dead lead and an adaptive threshold will happily invent beats in it (D-08).
FLATLINE_STD_MV = 0.01
MIN_AMPLITUDE_MV = 0.1
#: Beyond the front end's own input range the amplifier has clipped (D-26).
MAX_AMPLITUDE_MV = 5.0


@dataclass(frozen=True)
class QualityConfig:
    """Window geometry and thresholds."""

    fs: float = DEFAULT_FS
    window_s: float = 4.0
    hop_s: float = 1.0
    good_psqi: float = GOOD_PSQI
    good_bassqi: float = GOOD_BASSQI
    good_ksqi: float = GOOD_KSQI
    unusable_ksqi: float = UNUSABLE_KSQI
    unusable_concentration: float = UNUSABLE_CONCENTRATION

    def __post_init__(self) -> None:
        if self.fs <= 0:
            raise ValueError(f"fs must be positive, got {self.fs}")
        if self.window_s <= 0 or self.hop_s <= 0:
            raise ValueError("window_s and hop_s must be positive")
        if self.hop_s > self.window_s:
            raise ValueError("hop_s must not exceed window_s, or samples would be skipped")


@dataclass(frozen=True)
class QualityResult:
    """One window's verdict, with the numbers behind it."""

    label: str  # "good" | "poor" | "unusable"
    psqi: float
    bassqi: float
    ksqi: float
    amplitude_mv: float
    reason: str
    #: QRS energy concentration; 0 when the absolute gates fired before it was computed.
    concentration: float = 0.0

    @property
    def trustworthy(self) -> bool:
        """Whether a number derived from this window may be displayed."""
        return self.label != "unusable"


def _band_power(freqs: np.ndarray, psd: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs <= hi)
    return float(psd[mask].sum())


def window_quality(x: np.ndarray, cfg: QualityConfig | None = None) -> QualityResult:
    """Assess one window of *filtered* ECG, in mV."""
    cfg = cfg or QualityConfig()
    x = np.asarray(x, dtype=np.float64)
    if x.size < 8:
        return QualityResult("unusable", 0.0, 0.0, 0.0, 0.0, "window too short to assess")
    if not np.all(np.isfinite(x)):
        return QualityResult("unusable", 0.0, 0.0, 0.0, 0.0, "window contains non-finite samples")

    amplitude = float(np.ptp(x))
    std = float(np.std(x))

    # --- absolute gates first: these do not need a spectrum to be decidable ---
    if std < FLATLINE_STD_MV:
        return QualityResult("unusable", 0.0, 0.0, 0.0, amplitude,
                             f"flat line - std {std * 1000:.2f} µV")
    if amplitude < MIN_AMPLITUDE_MV:
        return QualityResult("unusable", 0.0, 0.0, 0.0, amplitude,
                             f"amplitude {amplitude:.3f} mV below the {MIN_AMPLITUDE_MV} mV floor")
    if amplitude > MAX_AMPLITUDE_MV:
        return QualityResult("unusable", 0.0, 0.0, 0.0, amplitude,
                             f"amplitude {amplitude:.2f} mV beyond the front end's range")

    nperseg = min(int(cfg.fs * 2), x.size)
    freqs, psd = sps.welch(x, fs=cfg.fs, nperseg=nperseg)
    inband = _band_power(freqs, psd, 0.5, 40.0)
    to40 = _band_power(freqs, psd, 0.0, 40.0)
    psqi = _band_power(freqs, psd, 5.0, 15.0) / inband if inband > 0 else 0.0
    bassqi = 1.0 - (_band_power(freqs, psd, 0.0, 1.0) / to40) if to40 > 0 else 0.0
    ksqi = float(stats.kurtosis(x, fisher=False))

    concentration = energy_concentration(x, _QrsConfig(fs=cfg.fs))
    if concentration < cfg.unusable_concentration:
        # Low concentration alone can also mean a fast clean rhythm (D-51). Ask the
        # detector what it experienced: only when its signal/noise estimate is low too
        # is the energy genuinely spread by noise.
        from pan_tompkins import detect_qrs

        snr = detect_qrs(x, _QrsConfig(fs=cfg.fs), prefilter=False).snr
        if snr < UNUSABLE_SNR:
            return QualityResult("unusable", psqi, bassqi, ksqi, amplitude,
                                 f"QRS energy concentration {concentration:.1f} below "
                                 f"{cfg.unusable_concentration:.0f} and detector SNR "
                                 f"{snr:.1f} below {UNUSABLE_SNR:.0f} - a rate from this "
                                 f"signal would not meet SR-01", concentration)

    if ksqi < cfg.unusable_ksqi:
        return QualityResult("unusable", psqi, bassqi, ksqi, amplitude,
                             f"kurtosis {ksqi:.1f} - no more peaked than noise, no QRS structure")

    failures = []
    if psqi < cfg.good_psqi:
        failures.append(f"pSQI {psqi:.2f}")
    if bassqi < cfg.good_bassqi:
        failures.append(f"basSQI {bassqi:.2f}")
    if ksqi < cfg.good_ksqi:
        failures.append(f"kSQI {ksqi:.1f}")

    if failures:
        return QualityResult("poor", psqi, bassqi, ksqi, amplitude, "below clean-record floor: " + ", ".join(failures))
    return QualityResult("good", psqi, bassqi, ksqi, amplitude,
                         f"pSQI {psqi:.2f}, basSQI {bassqi:.2f}, kSQI {ksqi:.1f}")


def assess(x: np.ndarray, cfg: QualityConfig | None = None) -> list[tuple[int, int, QualityResult]]:
    """Assess a whole recording as overlapping windows.

    Returns ``(start, stop, result)`` per window, so a caller can map a verdict back onto
    the samples - and onto the beats detected inside them.
    """
    cfg = cfg or QualityConfig()
    x = np.asarray(x, dtype=np.float64)
    w = int(round(cfg.window_s * cfg.fs))
    hop = int(round(cfg.hop_s * cfg.fs))
    if x.size < w:
        return [(0, int(x.size), window_quality(x, cfg))]
    return [(i, i + w, window_quality(x[i : i + w], cfg)) for i in range(0, x.size - w + 1, hop)]


def summarise(windows: list[tuple[int, int, QualityResult]]) -> dict[str, float]:
    """Fraction of time spent at each quality level."""
    if not windows:
        return {"good": 0.0, "poor": 0.0, "unusable": 0.0}
    labels = [r.label for _, _, r in windows]
    n = len(labels)
    return {label: labels.count(label) / n for label in ("good", "poor", "unusable")}


def trustworthy_mask(n_samples: int, windows: list[tuple[int, int, QualityResult]]) -> np.ndarray:
    """Per-sample boolean: may a number derived from this sample be displayed?

    A sample is untrustworthy if *any* window covering it is unusable. Overlapping
    windows mean a bad patch taints its neighbourhood, which is the conservative
    direction and the right one - the alternative is reporting a number from the clean
    half of a window straddling an artefact.
    """
    mask = np.ones(n_samples, dtype=bool)
    for start, stop, res in windows:
        if not res.trustworthy:
            mask[start:stop] = False
    return mask


# --------------------------------------------------------------------------------
# Calibration (reproduces the thresholds above)
# --------------------------------------------------------------------------------


def calibrate(records: tuple[str, ...] = ("100", "101", "103", "115")) -> dict[str, float]:
    """Re-derive the `good` thresholds from clean records. Needs the dataset."""
    from filters import FilterConfig, clean_offline
    from validate import load_record

    cfg = QualityConfig()
    rows: list[tuple[float, float, float]] = []
    for rec in records:
        signal, _, fs = load_record(rec)
        x = clean_offline(signal, FilterConfig())
        for start, stop, _ in assess(x, QualityConfig(fs=fs, hop_s=2.0)):
            freqs, psd = sps.welch(x[start:stop], fs=fs, nperseg=min(int(fs * 2), stop - start))
            inband = _band_power(freqs, psd, 0.5, 40.0)
            to40 = _band_power(freqs, psd, 0.0, 40.0)
            rows.append((
                _band_power(freqs, psd, 5.0, 15.0) / inband if inband else 0.0,
                1.0 - (_band_power(freqs, psd, 0.0, 1.0) / to40) if to40 else 0.0,
                float(stats.kurtosis(x[start:stop], fisher=False)),
            ))
    a = np.array(rows)
    return {
        "n_windows": float(len(rows)),
        "good_psqi": float(np.percentile(a[:, 0], 1)),
        "good_bassqi": float(np.percentile(a[:, 1], 1)),
        "good_ksqi": float(np.percentile(a[:, 2], 1)),
    }


# --------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------


def _self_test() -> None:
    """Run: python dsp/quality.py"""
    import sys

    from synth import SynthConfig, synthetic_ecg

    if "--calibrate" in sys.argv:
        got = calibrate()
        print(f"\nRe-derived from {int(got['n_windows'])} clean-record windows:")
        for k in ("good_psqi", "good_bassqi", "good_ksqi"):
            print(f"  {k:12s} = {got[k]:.3f}   (module constant: {globals()[k.upper()]})")
        return

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    from filters import FilterConfig, clean_offline

    def prep(cfg: SynthConfig) -> np.ndarray:
        sig, _ = synthetic_ecg(cfg)
        return clean_offline(sig, FilterConfig())

    # --- a clean synthetic ECG is trustworthy, though not "good" ---
    # It scores kSQI ~17 against ~29 for real recordings because its Gaussian QRS is
    # broader than a real one, so it lands on "poor". The requirement that matters is
    # SR-07 - a number must not be reported from an untrustworthy signal - and "poor"
    # still reports. Whether real clean records score "good" is checked against the
    # dataset in dsp/validate.py, where the dataset already lives.
    clean = prep(SynthConfig(duration_s=20, hr_bpm=72))
    res = window_quality(clean[: int(4 * DEFAULT_FS)])
    check("a clean ECG is trustworthy", res.trustworthy, f"{res.label}: {res.reason}")
    check("a clean ECG is not called unusable", res.label != "unusable", res.label)

    # --- the dangerous inputs are unusable, not merely poor ---
    dead = window_quality(np.zeros(1440))
    check("a dead lead is unusable", dead.label == "unusable", dead.reason)
    dc = window_quality(np.full(1440, 2.5))
    check("constant DC is unusable", dc.label == "unusable", dc.reason)
    noise = window_quality(np.random.default_rng(0).normal(0, 0.3, 1440))
    check("pure Gaussian noise is unusable", noise.label == "unusable", noise.reason)
    check("...because it is no more peaked than noise", noise.ksqi < UNUSABLE_KSQI, f"kSQI {noise.ksqi:.2f}")
    railed = window_quality(np.concatenate([np.full(720, -4.0), np.full(720, 4.0)]))
    check("a railed amplifier is unusable", railed.label == "unusable", railed.reason)
    nan = window_quality(np.concatenate([clean[:1439], [np.nan]]))
    check("non-finite samples are unusable", nan.label == "unusable", nan.reason)
    tiny = window_quality(clean[: int(4 * DEFAULT_FS)] * 0.02)
    check("an ECG scaled below 0.1 mV is unusable", tiny.label == "unusable", tiny.reason)

    # --- degradation is graded, not binary ---
    labels = []
    for noise_std in (0.0, 0.05, 0.15, 0.4, 1.0):
        w = prep(SynthConfig(duration_s=20, hr_bpm=72, noise_std=noise_std, seed=3))[: int(4 * DEFAULT_FS)]
        labels.append(window_quality(w).label)
    check("quality degrades monotonically with noise", labels == sorted(labels, key=["good", "poor", "unusable"].index),
          " -> ".join(labels))
    check("a clean signal is trustworthy and a swamped one is not",
          labels[0] != "unusable" and labels[-1] == "unusable", f"{labels[0]} ... {labels[-1]}")

    # --- baseline wander is what basSQI is for ---
    wander = prep(SynthConfig(duration_s=20, hr_bpm=72, wander_amp=0.0))[: int(4 * DEFAULT_FS)]
    t = np.arange(wander.size) / DEFAULT_FS
    drifted = wander + 3.0 * np.sin(2 * np.pi * 0.3 * t)
    check("baseline wander is detected by basSQI",
          window_quality(drifted).bassqi < window_quality(wander).bassqi,
          f"{window_quality(drifted).bassqi:.3f} vs {window_quality(wander).bassqi:.3f}")

    # --- windowing ---
    windows = assess(prep(SynthConfig(duration_s=30, hr_bpm=72)))
    check("assess covers the recording in overlapping windows", len(windows) > 20, f"{len(windows)} windows")
    share = summarise(windows)
    check("a clean 30 s recording is entirely trustworthy", share["unusable"] == 0.0,
          f"{100 * share['unusable']:.0f} % unusable")
    check("quality shares sum to 1", abs(sum(share.values()) - 1.0) < 1e-9, str({k: round(v, 3) for k, v in share.items()}))

    # --- a bad patch taints its window, and only its window ---
    mixed = prep(SynthConfig(duration_s=40, hr_bpm=72)).copy()
    mixed[int(20 * DEFAULT_FS) : int(24 * DEFAULT_FS)] = 0.0
    mask = trustworthy_mask(mixed.size, assess(mixed))
    check("a flat patch is marked untrustworthy", not mask[int(22 * DEFAULT_FS)], "sample at 22 s excluded")
    check("clean signal well before it stays trustworthy", bool(mask[int(5 * DEFAULT_FS)]), "sample at 5 s kept")
    check("clean signal well after it stays trustworthy", bool(mask[int(35 * DEFAULT_FS)]), "sample at 35 s kept")
    check("most of the recording is still usable", mask.mean() > 0.7, f"{100 * mask.mean():.0f} % trustworthy")

    # --- a clean fast rhythm is not silenced (D-51) ---
    # The failure this pins: at 130 bpm the QRS bumps crowd together, the concentration
    # statistic collapses, and the gate withheld the rate of a perfectly clean
    # tachycardia - the rhythm the monitor most needs to report. Found by a person
    # following TESTING.md when the tachycardia alarm never fired.
    fast = prep(SynthConfig(duration_s=20, hr_bpm=130))
    res_fast = window_quality(fast[int(2 * DEFAULT_FS) : int(6 * DEFAULT_FS)])
    check("a clean 130 bpm rhythm is not called unusable",
          res_fast.label != "unusable", f"{res_fast.label}: {res_fast.reason}")
    # Above ~145 bpm this OFFLINE mirror still withholds: the offline detector's noise
    # estimate adapts differently from the streaming one's (its npki absorbs rejected
    # T-wave peaks), so its SNR at a clean 150 bpm is ~4 against the streaming
    # detector's ~13. The streaming gate - the one the shipped monitor runs - keeps
    # 150 bpm reportable, pinned by the server test 'a clean tachycardia is reported'.
    # Withholding here is the conservative direction for an offline reporting tool,
    # and it is recorded rather than hidden (D-51).

    # --- config validation ---
    for kwargs, label in ((dict(fs=0), "fs = 0"), (dict(window_s=0), "window_s = 0"),
                          (dict(hop_s=10, window_s=4), "hop longer than window")):
        try:
            QualityConfig(**kwargs)
            check(f"rejects invalid config ({label})", False, "no error raised")
        except ValueError:
            check(f"rejects invalid config ({label})", True, "ValueError")

    width = max(len(n_) for n_, _, _ in checks)
    passed = sum(ok for _, ok, _ in checks)
    print(f"\nquality.py self-test\n" + "-" * (width + 30))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print("-" * (width + 30))
    print(f"{passed}/{len(checks)} checks passed\n")
    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
