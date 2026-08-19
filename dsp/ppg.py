"""Photoplethysmography: pulse oximetry from red and infrared light.

SpO2 is the second parameter this monitor claims, and until now it was not earned - the
value was passed straight through from the frame and no code computed anything. This
module computes it.

**How pulse oximetry works.** Oxygenated and deoxygenated haemoglobin absorb red
(~660 nm) and infrared (~940 nm) light differently: oxygenated blood absorbs less red,
which is why arterial blood looks bright. Shine both wavelengths through a fingertip and
each transmitted signal has two parts - a large steady component from tissue, bone and
venous blood, and a small pulsatile component from the arterial pressure wave. Only the
pulsatile part is arterial, so taking the ratio AC/DC in each channel removes everything
that is not blood being pushed by a heartbeat, including skin tone, finger thickness and
LED brightness. The ratio of those two ratios,

    R = (AC_red / DC_red) / (AC_ir / DC_ir)

depends on oxygen saturation and very little else. That is the whole idea, and it is why
a pulse oximeter needs no calibration against the individual patient.

**What this module can and cannot verify.** The step from R to a saturation percentage
is an *empirical* curve, fitted per sensor design against arterial blood-gas
measurements from human desaturation studies. It cannot be derived, and it cannot be
validated in simulation: nothing here proves the constants are right for any real
sensor. What can be validated in simulation is everything up to that point - that R is
recovered correctly from a waveform whose true R is known, that it survives noise,
motion and poor perfusion, and that the module refuses to answer when it should. So this
module reports SpO2 *and* the R it came from, and the verification report says plainly
which half is verified (SR-06).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal as sps

DEFAULT_FS = 100.0  # MAX30102 default sample rate; ECG runs at 360 Hz separately

#: Empirical calibration, the linear form quoted throughout the pulse-oximetry
#: literature. Constants are sensor-specific in a real product; these are the generic
#: textbook values and are treated as an assumption, not a result.
CAL_INTERCEPT = 110.0
CAL_SLOPE = 25.0

#: Below this perfusion index the pulsatile signal is too small to divide by safely -
#: a cold or poorly placed finger. Real oximeters report "low perfusion" and stop.
MIN_PERFUSION_PCT = 0.15

#: R outside this range is not a saturation the curve was fitted over; reporting a
#: number from it would be extrapolation dressed up as a measurement.
MIN_PLAUSIBLE_R = 0.3
MAX_PLAUSIBLE_R = 3.0

#: Both wavelengths pass through the same arterial bed and see the same volumetric
#: pulsation, so their pulsatile waveforms must be near-identical in shape - only their
#: amplitude differs, and that difference is the measurement. Anything that is not
#: arterial pulsation perturbs the two channels differently, so their correlation is a
#: direct test of whether the signal being measured is the signal of interest.
#:
#: This threshold is not a guess: swept across 432 synthetic recordings spanning
#: saturation, perfusion, noise and motion, a floor of 0.97 rejected every reading that
#: was more than 2 % wrong (worst was 12.1 % wrong) while refusing 7.7 % of accurate
#: ones. Refusing a good reading is safe; reporting a wrong one is not.
MIN_CHANNEL_CORR = 0.97
#: Above this the channels agree well enough to call the reading good rather than poor.
GOOD_CHANNEL_CORR = 0.995

#: A lag counts as the fundamental if its correlation is at least this fraction of the
#: best candidate's. Standard practice in autocorrelation pitch detection, and needed
#: here because a longer lag can outscore the true period when beats vary in amplitude.
#:
#: Swept against the BIDMC recordings: at 0.85 five windows locked to exactly half the
#: patient's pulse rate; at 0.50 none did, mean error fell from 2.93 to 1.57 bpm, and
#: the synthetic sweep from 48 to 180 bpm was unchanged at 1.82 bpm worst case - so
#: nothing was traded away at the fast end to fix the slow one.
SUBHARMONIC_TOLERANCE = 0.50

#: Below this normalised autocorrelation there is no pulse to find.
#:
#: Unlike the red/IR correlation gate, this one does **not** separate cleanly: across
#: the BIDMC windows, readings accurate to within 5 bpm run down to a strength of 0.199
#: while inaccurate ones run up to 0.746. There is no threshold that keeps every good
#: window and refuses every bad one. 0.50 is chosen on the conservative side of that
#: overlap - it cuts the wrong readings from nine to two and still keeps 87 % of windows.
#:
#: The residual is stated rather than hidden: about 1 % of windows can still be wrong by
#: up to 25 bpm. That is tolerable *here specifically* because the ECG is this monitor's
#: primary heart-rate source and the PPG rate is a cross-check. The proper fix is not a
#: better single-signal threshold but the comparison itself - two independent
#: measurements of the same quantity disagreeing is stronger evidence than either signal
#: can produce alone, and it is how bedside monitors decide the same question (D-37).
MIN_PERIODICITY = 0.50

#: Pulse rates outside this are not plausible for a human at rest or exercise.
MIN_PULSE_BPM = 25.0
MAX_PULSE_BPM = 240.0


def spo2_from_ratio(r: float) -> float:
    """Empirical ratio-of-ratios to saturation. See the module docstring's caveat."""
    return CAL_INTERCEPT - CAL_SLOPE * r


def ratio_from_spo2(spo2: float) -> float:
    """Inverse of :func:`spo2_from_ratio`, used to synthesise a signal with known R."""
    return (CAL_INTERCEPT - spo2) / CAL_SLOPE


# --------------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PpgConfig:
    """A synthetic pulse-oximeter recording with known saturation."""

    duration_s: float = 30.0
    fs: float = DEFAULT_FS
    pulse_bpm: float = 72.0
    spo2_pct: float = 97.0
    perfusion_pct: float = 2.0  # AC/DC of the IR channel, in percent
    dc_ir: float = 100000.0     # raw counts; MAX30102 returns ~1e5 for a finger
    dc_red: float = 80000.0
    noise_frac: float = 0.0     # white noise as a fraction of the AC amplitude
    motion_frac: float = 0.0    # motion artefact as a fraction of the AC amplitude
    motion_hz: float = 0.7
    #: How much more the red channel is perturbed by motion than the infrared. Motion
    #: changes the optical path and the arterial/venous mix, and it does not do so
    #: identically at two wavelengths. A model that moved both channels by the same
    #: amount would cancel in the ratio and make motion look harmless, which is the
    #: opposite of clinical experience - motion artefact is the main reason pulse
    #: oximeters report wrong numbers.
    motion_red_gain: float = 1.6
    wander_frac: float = 0.0    # slow baseline drift as a fraction of DC
    hrv_frac: float = 0.0
    seed: int = 0

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError(f"duration_s must be positive, got {self.duration_s}")
        if not MIN_PULSE_BPM <= self.pulse_bpm <= MAX_PULSE_BPM:
            raise ValueError(f"pulse_bpm out of plausible range: {self.pulse_bpm}")
        if not 50.0 <= self.spo2_pct <= 100.0:
            raise ValueError(f"spo2_pct out of range: {self.spo2_pct}")
        if self.perfusion_pct <= 0:
            raise ValueError(f"perfusion_pct must be positive, got {self.perfusion_pct}")
        if self.dc_ir <= 0 or self.dc_red <= 0:
            raise ValueError("DC levels must be positive")


def _pulse_waveform(phase: np.ndarray) -> np.ndarray:
    """One cardiac cycle of a PPG, normalised to unit peak-to-peak.

    Two Gaussians: the systolic upstroke and, after the aortic valve closes, the
    dicrotic notch and its reflected wave. Not a haemodynamic model - but the shape,
    asymmetry and harmonic content are right, which is what the AC/DC measurement and
    the peak finder actually depend on.
    """
    systolic = np.exp(-(((phase - 0.22) / 0.11) ** 2))
    dicrotic = 0.35 * np.exp(-(((phase - 0.52) / 0.10) ** 2))
    w = systolic + dicrotic
    return (w - w.min()) / np.ptp(w)


def synthetic_ppg(cfg: PpgConfig | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Generate red and IR channels with a *known* saturation.

    Returns
    -------
    red, ir : np.ndarray
        Raw sensor counts, DC included - what a MAX30102 hands over.
    peaks : np.ndarray
        Sample indices of the true systolic peaks, the ground truth for pulse rate.
    """
    cfg = cfg or PpgConfig()
    rng = np.random.default_rng(cfg.seed)
    n = int(round(cfg.duration_s * cfg.fs))
    t = np.arange(n) / cfg.fs

    # Beat times, with optional variability.
    mean_rr = 60.0 / cfg.pulse_bpm
    beats: list[float] = []
    clock = 0.0
    while clock < cfg.duration_s + mean_rr:
        beats.append(clock)
        rr = mean_rr * (1.0 + rng.normal(0.0, cfg.hrv_frac)) if cfg.hrv_frac else mean_rr
        clock += max(rr, 60.0 / MAX_PULSE_BPM)

    # Build a unit-amplitude pulsatile waveform on the sample grid.
    ac_shape = np.zeros(n)
    peaks: list[int] = []
    for i, start in enumerate(beats[:-1]):
        rr = beats[i + 1] - start
        lo = int(round(start * cfg.fs))
        hi = int(round((start + rr) * cfg.fs))
        if hi <= 0 or lo >= n:
            continue
        idx = np.arange(max(lo, 0), min(hi, n))
        phase = (idx / cfg.fs - start) / rr
        ac_shape[idx] = _pulse_waveform(phase)
        peak = lo + int(round(0.22 * rr * cfg.fs))
        if 0 <= peak < n:
            peaks.append(peak)

    # The AC/DC ratio of each channel is what encodes saturation.
    ratio_ir = cfg.perfusion_pct / 100.0
    r_true = ratio_from_spo2(cfg.spo2_pct)
    ratio_red = r_true * ratio_ir

    ir = cfg.dc_ir * (1.0 - ratio_ir * ac_shape)
    red = cfg.dc_red * (1.0 - ratio_red * ac_shape)
    # Transmitted light *falls* as blood volume rises, hence the minus sign. Most
    # sensors invert this before reporting; keeping the physics here means the
    # estimator has to handle the real polarity.

    for name, sig, dc, ratio in (("ir", ir, cfg.dc_ir, ratio_ir), ("red", red, cfg.dc_red, ratio_red)):
        ac_amp = dc * ratio
        if cfg.noise_frac:
            sig += rng.normal(0.0, cfg.noise_frac * ac_amp, n)
        if cfg.motion_frac:
            gain = 1.0 if name == "ir" else cfg.motion_red_gain
            sig += cfg.motion_frac * gain * ac_amp * np.sin(
                2 * np.pi * cfg.motion_hz * t + (0.0 if name == "ir" else 0.4)
            )
        if cfg.wander_frac:
            sig += cfg.wander_frac * dc * np.sin(2 * np.pi * 0.05 * t)

    return red, ir, np.array(sorted(peaks), dtype=np.int64)


# --------------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------------


@dataclass
class Spo2Result:
    """What the oximeter concluded, including when it concluded nothing."""

    spo2_pct: float | None
    ratio: float | None
    pulse_bpm: float | None
    perfusion_pct: float
    n_beats: int
    quality: str  # "good" | "poor" | "unusable"
    reason: str
    #: Correlation between the red and infrared pulsatile waveforms. Near 1 when the
    #: thing being measured is a pulse; lower when it is something else.
    channel_corr: float = 0.0
    #: Normalised autocorrelation at the detected pulse period. Low means the signal is
    #: not periodic enough for there to be a pulse in it.
    periodicity: float = 0.0

    @property
    def valid(self) -> bool:
        return self.spo2_pct is not None


def _pulsatile(x: np.ndarray, fs: float) -> np.ndarray:
    """Isolate the cardiac component: 0.5-5 Hz covers 30-300 bpm and its first harmonic."""
    nyq = fs / 2.0
    high = min(5.0, nyq * 0.9)
    sos = sps.butter(2, [0.5 / nyq, high / nyq], btype="bandpass", output="sos")
    return sps.sosfiltfilt(sos, x)


def dominant_period_s(ac: np.ndarray, fs: float) -> float | None:
    """Estimate the pulse period by autocorrelation. See :func:`periodicity`."""
    period, _ = periodicity(ac, fs)
    return period


def periodicity(ac: np.ndarray, fs: float) -> tuple[float | None, float]:
    """Pulse period and how strongly the signal supports it.

    Needed because the refractory period cannot be a constant. A PPG pulse has a second
    hump - the dicrotic wave, from the aortic valve closing - roughly a third of a cycle
    after the systolic peak. A fixed minimum spacing wide enough to reject it at 48 bpm
    would reject real beats at 180 bpm, and one narrow enough for 180 bpm counts every
    dicrotic wave as a beat and doubles the reported rate. Estimating the period first
    makes the spacing scale with the patient.

    Two things this has to get right, both found on real ICU recordings rather than on
    synthetic signals:

    * **Sub-harmonic lock.** A periodic signal correlates with itself at every multiple
      of its period, and with beat-to-beat variation the *longer* lag can edge ahead. On
      record bidmc29 the true period scored 0.678 and double it scored 0.701, so a plain
      argmax reported half the patient's pulse rate. The fix is the standard one from
      pitch detection: among the candidate peaks, take the **shortest** lag whose
      correlation is within `SUBHARMONIC_TOLERANCE` of the best, not the highest.
    * **Signals that are not periodic at all.** The returned strength is the normalised
      correlation at the chosen lag. When it is low there is no pulse to find, and the
      caller should decline rather than report the best of a bad set of lags.

    Returns ``(period_s, strength)``; ``(None, 0.0)`` when no candidate exists.
    """
    x = np.asarray(ac, dtype=np.float64)
    x = x - x.mean()
    if x.size < 4 or not np.any(x):
        return None, 0.0
    corr = np.correlate(x, x, mode="full")[x.size - 1 :]
    if corr[0] <= 0:
        return None, 0.0
    corr = corr / corr[0]

    lo = int(fs * 60.0 / MAX_PULSE_BPM)
    hi = min(int(fs * 60.0 / MIN_PULSE_BPM), corr.size - 1)
    if hi <= lo:
        return None, 0.0

    # Local maxima only: without this, a shoulder of the zero-lag peak can win purely
    # by being close to it.
    window = corr[lo : hi + 1]
    peaks, _ = sps.find_peaks(window)
    if peaks.size == 0:
        lag = lo + int(np.argmax(window))
        return float(lag) / fs, float(corr[lag])

    lags = peaks + lo
    best = float(np.max(corr[lags]))
    if best <= 0:
        return None, 0.0
    # The shortest lag that is nearly as good as the best - the fundamental rather than
    # one of its multiples.
    acceptable = lags[corr[lags] >= SUBHARMONIC_TOLERANCE * best]
    lag = int(acceptable.min()) if acceptable.size else int(lags[int(np.argmax(corr[lags]))])
    return float(lag) / fs, float(corr[lag])


#: A candidate peak must reach this many standard deviations of the pulsatile signal.
PEAK_HEIGHT_FRAC = 0.4


def pick_peaks(x: np.ndarray, min_distance: int, height: float) -> np.ndarray:
    """Local maxima above `height`, thinned so none are closer than `min_distance`.

    Written out rather than delegated to `scipy.signal.find_peaks` because this function
    has to exist twice - here and in `server/src/dsp/ppg.js` - and a cross-language
    comparison is only meaningful if both sides run the same algorithm. Depending on
    scipy's prominence semantics would have meant the JavaScript port approximating
    something it could not reproduce, and any disagreement afterwards would have been
    impossible to attribute (D-38).

    Thinning is greedy by amplitude, tallest first, which is what scipy's `distance`
    does: keeping the strongest candidate in each neighbourhood rather than the earliest.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size < 3:
        return np.array([], dtype=np.int64)

    interior = np.arange(1, x.size - 1)
    is_local_max = (x[interior] > x[interior - 1]) & (x[interior] >= x[interior + 1])
    candidates = interior[is_local_max & (x[interior] >= height)]
    if candidates.size == 0:
        return np.array([], dtype=np.int64)

    order = candidates[np.argsort(-x[candidates], kind="stable")]
    keep: list[int] = []
    for idx in order:
        if all(abs(int(idx) - k) >= min_distance for k in keep):
            keep.append(int(idx))
    return np.array(sorted(keep), dtype=np.int64)


def find_pulses(ir: np.ndarray, fs: float) -> np.ndarray:
    """Systolic peaks on the IR channel, which has the stronger pulsatile signal."""
    ac = -_pulsatile(ir, fs)  # invert: transmitted light dips as volume rises
    std = float(np.std(ac))
    if std <= 0:
        return np.array([], dtype=np.int64)

    period = dominant_period_s(ac, fs)
    # 0.6 of a period rejects the dicrotic wave (which arrives at ~0.3) while leaving
    # room for the beat-to-beat variability a real pulse has.
    min_distance = int(0.6 * period * fs) if period else int(fs * 60.0 / MAX_PULSE_BPM)
    return pick_peaks(ac, max(min_distance, 1), PEAK_HEIGHT_FRAC * std)


def estimate_spo2(red: np.ndarray, ir: np.ndarray, fs: float = DEFAULT_FS) -> Spo2Result:
    """Estimate saturation, pulse rate and perfusion - or explain why it cannot.

    R is computed per beat and combined with a median rather than a mean, for the same
    reason the heart rate is (D-09): one corrupted beat should not move the reported
    number far enough to trip an alarm.
    """
    red = np.asarray(red, dtype=np.float64)
    ir = np.asarray(ir, dtype=np.float64)
    if red.shape != ir.shape:
        raise ValueError(f"red and ir must be the same length, got {red.shape} and {ir.shape}")
    if fs <= 0:
        raise ValueError(f"fs must be positive, got {fs}")

    nothing = lambda reason, pi=0.0, n=0, corr=0.0, per=0.0: Spo2Result(  # noqa: E731
        None, None, None, pi, n, "unusable", reason, channel_corr=corr, periodicity=per
    )

    if red.size < int(fs * 2):
        return nothing("less than 2 s of signal")
    dc_ir_all, dc_red_all = float(np.mean(ir)), float(np.mean(red))
    if dc_ir_all <= 0 or dc_red_all <= 0:
        return nothing("no light reaching the detector - sensor off the finger?")

    ac_ir = _pulsatile(ir, fs)
    ac_red = _pulsatile(red, fs)
    perfusion = 100.0 * float(np.ptp(ac_ir)) / dc_ir_all

    # Do the two wavelengths agree that they are looking at a pulse?
    channel_corr = 0.0
    if np.std(ac_ir) > 0 and np.std(ac_red) > 0:
        channel_corr = float(np.corrcoef(ac_red, ac_ir)[0, 1])

    peaks = find_pulses(ir, fs)
    if peaks.size < 3:
        return nothing("fewer than 3 pulses found", perfusion, int(peaks.size), channel_corr)

    # Per-beat AC (peak-to-peak of the pulsatile component) over local DC (mean of the
    # raw signal in the same window). Measuring both in the same window is what makes
    # the ratio immune to slow drift in either channel.
    ratios: list[float] = []
    for lo, hi in zip(peaks[:-1], peaks[1:]):
        if hi - lo < 2:
            continue
        dc_i, dc_r = float(np.mean(ir[lo:hi])), float(np.mean(red[lo:hi]))
        if dc_i <= 0 or dc_r <= 0:
            continue
        ac_i, ac_r = float(np.ptp(ac_ir[lo:hi])), float(np.ptp(ac_red[lo:hi]))
        if ac_i <= 0:
            continue
        ratios.append((ac_r / dc_r) / (ac_i / dc_i))

    # Pulse rate comes from the autocorrelation period, not from counting peaks.
    # On real ICU recordings the two disagree badly: peak counting and median-RR both
    # ran 15-23 bpm low on several BIDMC records because beat amplitude varies with
    # respiration and a prominence threshold silently drops the small beats. The
    # autocorrelation period is a whole-window measure and does not care how tall any
    # individual beat is - mean error 0.4-5 bpm on the same recordings (D-36).
    period_s, strength = periodicity(ac_ir, fs)
    pulse = 60.0 / period_s if period_s else None

    if perfusion < MIN_PERFUSION_PCT:
        # Dividing by a pulsatile signal this small amplifies noise into the answer.
        return Spo2Result(None, None, pulse, perfusion, int(peaks.size), "unusable",
                          f"perfusion {perfusion:.2f} % below the {MIN_PERFUSION_PCT} % floor",
                          channel_corr=channel_corr, periodicity=strength)
    if strength < MIN_PERIODICITY:
        # Nothing in this window repeats at a pulse-like interval. Record bidmc41
        # scores 0.09 at its true period; a rate read from that is invented, not
        # measured.
        return Spo2Result(None, None, None, perfusion, int(peaks.size), "unusable",
                          f"periodicity {strength:.2f} below {MIN_PERIODICITY} - no pulse present",
                          channel_corr=channel_corr, periodicity=strength)
    if channel_corr < MIN_CHANNEL_CORR:
        # The red and infrared channels disagree about the shape of what they are
        # seeing, so whatever is moving is not (only) arterial blood. The ratio would
        # still produce a confident-looking number, and that number would be wrong -
        # this is the SpO2 channel's version of the artefact that fabricated a heart
        # rate in session 006, and it is caught here because the check is physical
        # rather than a plausibility test.
        return Spo2Result(None, None, pulse, perfusion, int(peaks.size), "unusable",
                          f"red/IR pulsatile correlation {channel_corr:.3f} below "
                          f"{MIN_CHANNEL_CORR} - artefact, not pulsation",
                          channel_corr=channel_corr, periodicity=strength)
    if not ratios:
        return nothing("no usable beat windows", perfusion, int(peaks.size), channel_corr, strength)

    ratio = float(np.median(ratios))
    if not MIN_PLAUSIBLE_R <= ratio <= MAX_PLAUSIBLE_R:
        return Spo2Result(None, ratio, pulse, perfusion, int(peaks.size), "unusable",
                          f"R = {ratio:.2f} outside the calibrated range",
                          channel_corr=channel_corr, periodicity=strength)

    # Beat-to-beat spread is a direct measure of how much to trust the median.
    spread = float(np.percentile(ratios, 75) - np.percentile(ratios, 25)) if len(ratios) > 3 else 0.0
    quality = "good" if (spread < 0.15 and channel_corr >= GOOD_CHANNEL_CORR) else "poor"

    spo2 = spo2_from_ratio(ratio)
    if spo2 > 100.0:
        # Physically impossible. Clamping is right - the curve is approximate near
        # full saturation - but the reading is no longer independent evidence.
        spo2, quality = 100.0, "poor" if quality == "good" else quality

    return Spo2Result(round(spo2, 1), round(ratio, 4), pulse, perfusion, int(peaks.size), quality,
                      f"{len(ratios)} beat windows, IQR {spread:.3f}, red/IR corr {channel_corr:.3f}",
                      channel_corr=channel_corr, periodicity=strength)


# --------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------


def _self_test() -> None:
    """Run: python dsp/ppg.py"""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    # --- the calibration curve is self-consistent ---
    for s in (85.0, 92.0, 97.0, 100.0):
        check(f"ratio/saturation round-trip at {s:.0f} %",
              abs(spo2_from_ratio(ratio_from_spo2(s)) - s) < 1e-9, f"R = {ratio_from_spo2(s):.3f}")

    # --- synthesis produces what it claims ---
    red, ir, peaks = synthetic_ppg(PpgConfig(duration_s=30, pulse_bpm=72, spo2_pct=97))
    check("synthesised length matches duration", red.size == 3000, f"{red.size} samples")
    check("72 bpm over 30 s gives ~36 pulses", 33 <= peaks.size <= 38, f"{peaks.size} pulses")
    check("IR carries more DC than red", float(np.mean(ir)) > float(np.mean(red)),
          f"{np.mean(ir):.0f} vs {np.mean(red):.0f}")
    check("no NaN/Inf", not (np.isnan(red).any() or np.isnan(ir).any()), "clean")

    # --- recovery across the clinically interesting range ---
    worst = 0.0
    for truth in (100.0, 98.0, 95.0, 92.0, 88.0, 85.0, 80.0):
        r, i, _ = synthetic_ppg(PpgConfig(duration_s=30, spo2_pct=truth, seed=1))
        res = estimate_spo2(r, i)
        err = abs((res.spo2_pct or 0) - truth)
        worst = max(worst, err)
        check(f"recovers {truth:.0f} % saturation", res.valid and err < 1.0,
              f"got {res.spo2_pct} % (R = {res.ratio}), {res.quality}")
    check("worst-case error across the range is under 1 %", worst < 1.0, f"{worst:.2f} %")

    # --- pulse rate ---
    for bpm in (48.0, 72.0, 120.0, 180.0):
        r, i, _ = synthetic_ppg(PpgConfig(duration_s=30, pulse_bpm=bpm, seed=2))
        res = estimate_spo2(r, i)
        check(f"pulse rate at {bpm:.0f} bpm", res.pulse_bpm is not None and abs(res.pulse_bpm - bpm) < 2.0,
              f"{res.pulse_bpm:.1f} bpm" if res.pulse_bpm else "none")

    # The bug this replaced: a fixed refractory period counted the dicrotic wave as a
    # beat and doubled the reported rate at low heart rates. Pin it at the rate where it
    # was worst, and check the pulse count directly rather than only the rate.
    r, i, truth = synthetic_ppg(PpgConfig(duration_s=20, pulse_bpm=48, seed=2))
    found = find_pulses(i, DEFAULT_FS)
    check("the dicrotic wave is not counted as a beat at 48 bpm",
          abs(int(found.size) - int(truth.size)) <= 1, f"{found.size} found vs {truth.size} true")

    # --- the ratio is immune to the things it is designed to be immune to ---
    base = estimate_spo2(*synthetic_ppg(PpgConfig(duration_s=30, spo2_pct=95, seed=3))[:2])
    darker, _, _ = synthetic_ppg(PpgConfig(duration_s=30, spo2_pct=95, seed=3, dc_ir=40000, dc_red=32000))
    _, darker_ir, _ = synthetic_ppg(PpgConfig(duration_s=30, spo2_pct=95, seed=3, dc_ir=40000, dc_red=32000))
    dim = estimate_spo2(darker, darker_ir)
    check("halving the DC level does not change the reading",
          dim.valid and abs(dim.spo2_pct - base.spo2_pct) < 0.5,
          f"{base.spo2_pct} % -> {dim.spo2_pct} %")

    weak = estimate_spo2(*synthetic_ppg(PpgConfig(duration_s=30, spo2_pct=95, perfusion_pct=0.5, seed=3))[:2])
    check("a fivefold drop in perfusion does not change the reading",
          weak.valid and abs(weak.spo2_pct - base.spo2_pct) < 1.0,
          f"{weak.spo2_pct} % at PI {weak.perfusion_pct:.2f} %")

    # --- and it degrades honestly on the things it cannot survive ---
    flat = np.full(3000, 50000.0)
    res = estimate_spo2(flat, flat)
    check("a flat signal reports nothing, not a number", not res.valid, res.reason)

    dead = estimate_spo2(np.zeros(3000), np.zeros(3000))
    check("no light at all reports nothing", not dead.valid, dead.reason)

    tiny = estimate_spo2(*synthetic_ppg(PpgConfig(duration_s=30, perfusion_pct=0.05, seed=4))[:2])
    check("perfusion below the floor is refused, not extrapolated", not tiny.valid, tiny.reason)

    noisy = estimate_spo2(*synthetic_ppg(PpgConfig(duration_s=30, spo2_pct=95, noise_frac=0.8, seed=5))[:2])
    check("heavy noise is flagged or refused, never reported as good",
          noisy.quality in ("poor", "unusable"), f"{noisy.quality}: {noisy.reason}")

    # A monitor is not obliged to flag every artefact - it is obliged never to be
    # confidently wrong. So the property to assert is "accurate, or marked untrustworthy",
    # not "flagged". Asserting the stronger version would have forced the detector to
    # cry wolf on artefacts it actually survives, which is how alarm fatigue starts.
    for mf in (0.5, 1.5, 3.0):
        moved = estimate_spo2(*synthetic_ppg(PpgConfig(duration_s=30, spo2_pct=95, motion_frac=mf, seed=6))[:2])
        accurate = moved.valid and abs(moved.spo2_pct - 95.0) < 2.0
        check(f"motion x{mf} is either accurate or refused, never confidently wrong",
              accurate or not moved.valid,
              f"SpO2 {moved.spo2_pct}, corr {moved.channel_corr:.3f} - {moved.reason}")

    short = estimate_spo2(np.full(50, 1e5), np.full(50, 1e5))
    check("too little signal reports nothing", not short.valid, short.reason)

    # --- mild noise should still be usable, or the device is useless ---
    mild = estimate_spo2(*synthetic_ppg(PpgConfig(duration_s=30, spo2_pct=96, noise_frac=0.05,
                                                  wander_frac=0.02, seed=7))[:2])
    check("realistic mild noise still yields a reading",
          mild.valid and abs(mild.spo2_pct - 96.0) < 2.0, f"{mild.spo2_pct} %, {mild.quality}")

    # A noise-free red/IR pair is two scalar multiples of one waveform, so the channels
    # must correlate at 1.0. This is the check that would have caught the field-order
    # bug the JavaScript port exposed: the gate used the right value all along, but the
    # value *reported* was the periodicity, because a later patch inserted a dataclass
    # field ahead of `channel_corr` and the positional arguments silently shifted.
    clean = estimate_spo2(*synthetic_ppg(PpgConfig(duration_s=20, spo2_pct=99, seed=21))[:2])
    check("a noise-free pair correlates at 1.0 across the channels",
          clean.channel_corr > 0.999, f"corr = {clean.channel_corr:.6f}")
    check("channel_corr and periodicity are not the same number",
          abs(clean.channel_corr - clean.periodicity) > 1e-9,
          f"corr {clean.channel_corr:.4f} vs periodicity {clean.periodicity:.4f}")
    check("a clean signal is graded good, which requires the correlation to be right",
          clean.quality == "good", f"quality = {clean.quality}")

    # --- determinism and validation ---
    a = synthetic_ppg(PpgConfig(duration_s=10, noise_frac=0.1, seed=42))[0]
    b = synthetic_ppg(PpgConfig(duration_s=10, noise_frac=0.1, seed=42))[0]
    check("same seed reproduces the signal", np.array_equal(a, b), "identical")

    for kwargs, label in ((dict(spo2_pct=120), "spo2 = 120 %"), (dict(pulse_bpm=5), "pulse = 5 bpm"),
                          (dict(perfusion_pct=0), "perfusion = 0"), (dict(duration_s=0), "duration = 0")):
        try:
            PpgConfig(**kwargs)
            check(f"rejects invalid config ({label})", False, "no error raised")
        except ValueError:
            check(f"rejects invalid config ({label})", True, "ValueError")

    try:
        estimate_spo2(np.zeros(100), np.zeros(50))
        check("rejects mismatched channel lengths", False, "no error raised")
    except ValueError:
        check("rejects mismatched channel lengths", True, "ValueError")

    width = max(len(n_) for n_, _, _ in checks)
    passed = sum(ok for _, ok, _ in checks)
    print(f"\nppg.py self-test\n" + "-" * (width + 30))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print("-" * (width + 30))
    print(f"{passed}/{len(checks)} checks passed\n")
    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
