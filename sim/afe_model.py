"""Analog front-end and ADC model: what the ESP32 would actually have sampled.

Every number in `docs/verification.md` today was measured on clean floating-point
millivolts read straight out of a `.dat` file. A real device never sees that signal. It
sees the output of an instrumentation amplifier and an analog filter, offset by an
electrode half-cell potential, contaminated by mains coupling that survived the
common-mode rejection, clipped by a 3.3 V rail, sampled by an ADC with real
nonlinearity at a moment that is not exactly on the nominal grid, and quantised to 12
bits.

This module is that signal path, so the DSP can be validated against **what the
hardware would produce** rather than against an idealised input. That is the point: a
detector that scores 99.98 % on clean millivolts has not been shown to score 99.98 % on
ADC counts, and the difference is a measurement, not an assumption (D-24).

The chain, in order, and why it is in that order:

    patient (mV differential)
      + electrode half-cell offset and drift        electrochemistry, not signal
      + motion artefact                             low-frequency, large amplitude
      + mains coupling surviving CMRR and the RLD   the reason a notch filter exists
      -> high-pass                                  BEFORE the gain of ~1100, or a
                                                    300 mV offset would saturate the
                                                    amplifier and no gain setting could
                                                    recover it
      -> gain
      -> low-pass                                   anti-alias + EMG rejection
      -> level shift to VS/2                        single supply: the signal is
                                                    bipolar, the ADC is not
      -> rail clipping at 0 V / 3.3 V               saturation is a real failure mode
      -> sample at t + jitter                       timing error, not amplitude error
      -> ADC: gain error, offset, INL, noise
      -> 12-bit quantisation

Component values are synthesised for the target corner frequencies and then snapped to
the E12 preferred series, and the *realised* corner is what the model uses - because
that is what soldering a real resistor gets you, and "why these component values" is a
question with a real answer here rather than a datasheet copy.

Run `python sim/afe_model.py` for the self-test.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal as sps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dsp"))

from filters import DEFAULT_FS  # noqa: E402

#: E12 preferred series - the resistor and capacitor values actually stocked. Designing
#: to a value you cannot buy is how a simulated corner frequency and a built one differ.
E12: tuple[float, ...] = (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)

#: Capacitors are the coarser of the two, so the design fixes C from this list and
#: solves for R, which is available in far finer steps.
PREFERRED_CAPS_F: tuple[float, ...] = (1e-9, 4.7e-9, 10e-9, 47e-9, 100e-9, 470e-9, 1e-6)


def nearest_e12(value: float) -> float:
    """Snap a resistance to the nearest E12 value in its decade (geometrically nearest)."""
    if value <= 0:
        raise ValueError(f"value must be positive, got {value}")
    decade = 10.0 ** np.floor(np.log10(value))
    candidates = np.array([m * decade for m in E12] + [10.0 * decade, decade])
    # Geometric distance, because component tolerance is proportional, not absolute.
    return float(candidates[np.argmin(np.abs(np.log(candidates / value)))])


# --------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class AfeConfig:
    """Analog front-end: an AD8232-class ECG amplifier on a 3.3 V single supply."""

    hp_hz: float = 0.5  # high-pass corner, removes electrode offset and baseline wander
    lp_hz: float = 40.0  # low-pass corner, rejects EMG and anti-aliases
    #: Total V/V. NOT the ~1100 of the AD8232 datasheet's heart-rate-monitor
    #: configuration: that assumes a ~1.5 mV signal, and this model showed it clipping
    #: real MIT-BIH records (203 and 208 peak above 4 mV). IEC 60601-2-27 expects an
    #: ECG monitor to accept +/-5 mV, and 1.65 V of available swing over 5 mV is 330.
    #: The resolution given up is affordable - see `describe()` (D-26).
    gain: float = 330.0
    supply_v: float = 3.3
    vref_v: float = 1.65  # mid-supply reference the output swings about
    cmrr_db: float = 80.0  # amplifier common-mode rejection at mains frequency
    rld_reduction_db: float = 26.0  # right-leg drive reducing the body's common-mode voltage
    oversample: int = 8  # analog stage is evaluated this many times above fs

    def __post_init__(self) -> None:
        if not 0.01 <= self.hp_hz < self.lp_hz:
            raise ValueError(f"need 0.01 <= hp_hz < lp_hz, got {self.hp_hz} and {self.lp_hz}")
        if self.gain <= 0:
            raise ValueError(f"gain must be positive, got {self.gain}")
        if not 0 < self.vref_v < self.supply_v:
            raise ValueError(f"vref_v must lie inside the supply, got {self.vref_v}/{self.supply_v}")
        if self.oversample < 1:
            raise ValueError(f"oversample must be >= 1, got {self.oversample}")


@dataclass(frozen=True)
class ElectrodeConfig:
    """Everything between the heart and the amplifier input that is not the heart."""

    half_cell_mv: float = 120.0  # Ag/AgCl half-cell potential difference between the pair
    drift_mv_per_min: float = 8.0  # slow drift as the gel settles
    motion_amp_mv: float = 0.0  # motion artefact amplitude, referred to the input
    motion_hz: float = 0.4
    mains_cm_v: float = 1.5  # common-mode mains voltage on the body, volts
    mains_hz: float = 50.0
    emg_std_mv: float = 0.0  # muscle noise, mV RMS at the input
    seed: int = 0

    def __post_init__(self) -> None:
        if self.mains_cm_v < 0 or self.emg_std_mv < 0 or self.motion_amp_mv < 0:
            raise ValueError("amplitudes must be non-negative")


@dataclass(frozen=True)
class AdcConfig:
    """ESP32 ADC1. Deliberately unflattering - this converter is known to be poor."""

    bits: int = 12
    vmin_v: float = 0.0
    vmax_v: float = 3.3
    offset_lsb: float = 18.0  # fixed zero-code error
    gain_error: float = 0.012  # full-scale error, fraction
    inl_lsb: float = 6.0  # integral nonlinearity, peak, in LSB
    noise_lsb: float = 1.1  # RMS thermal + supply noise
    jitter_us: float = 0.0  # sampling instant jitter, RMS microseconds
    seed: int = 0

    def __post_init__(self) -> None:
        if not 8 <= self.bits <= 16:
            raise ValueError(f"bits out of range, got {self.bits}")
        if self.vmax_v <= self.vmin_v:
            raise ValueError("vmax_v must exceed vmin_v")
        if self.noise_lsb < 0 or self.inl_lsb < 0 or self.jitter_us < 0:
            raise ValueError("error terms must be non-negative")

    @property
    def codes(self) -> int:
        return 2**self.bits

    @property
    def lsb_v(self) -> float:
        return (self.vmax_v - self.vmin_v) / self.codes


# --------------------------------------------------------------------------------
# Component synthesis - the answer to "why these values"
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    """One synthesised RC pair and the corner it actually realises."""

    stage: str
    target_hz: float
    c_farads: float
    r_ohms: float

    @property
    def realised_hz(self) -> float:
        return 1.0 / (2.0 * np.pi * self.r_ohms * self.c_farads)

    @property
    def error_pct(self) -> float:
        return 100.0 * (self.realised_hz - self.target_hz) / self.target_hz

    def __str__(self) -> str:
        return (
            f"{self.stage:<12} target {self.target_hz:>6.2f} Hz  "
            f"C = {_eng(self.c_farads, 'F')}  R = {_eng(self.r_ohms, 'Ω')}  "
            f"realised {self.realised_hz:>6.2f} Hz ({self.error_pct:+.1f} %)"
        )


def _eng(value: float, unit: str) -> str:
    """Format a value with an engineering prefix, the way a BOM would list it."""
    for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= scale:
            return f"{value / scale:.3g} {prefix}{unit}"
    return f"{value:.3g} {unit}"


def synthesise_components(cfg: AfeConfig | None = None) -> list[Component]:
    """Pick real E12 parts for each corner frequency.

    The capacitor is chosen first, from the values actually stocked, such that the
    resulting resistor lands in a sane range: too low and it loads the previous stage,
    too high and it turns thermal noise and bias current into signal. 10 kΩ to 10 MΩ is
    the window used here.
    """
    cfg = cfg or AfeConfig()
    out: list[Component] = []
    for stage, target in (("high-pass", cfg.hp_hz), ("low-pass", cfg.lp_hz)):
        best: Component | None = None
        for c in PREFERRED_CAPS_F:
            r_ideal = 1.0 / (2.0 * np.pi * target * c)
            if not 1e4 <= r_ideal <= 1e7:
                continue
            cand = Component(stage, target, c, nearest_e12(r_ideal))
            if best is None or abs(cand.error_pct) < abs(best.error_pct):
                best = cand
        if best is None:  # no capacitor put R in range; fall back to the closest one
            c = min(PREFERRED_CAPS_F, key=lambda c_: abs(np.log(1.0 / (2 * np.pi * target * c_) / 1e5)))
            best = Component(stage, target, c, nearest_e12(1.0 / (2.0 * np.pi * target * c)))
        out.append(best)
    return out


def realised_corners(cfg: AfeConfig | None = None) -> tuple[float, float]:
    """The corner frequencies the built circuit has, not the ones it was designed for."""
    hp, lp = synthesise_components(cfg)
    return hp.realised_hz, lp.realised_hz


def afe_response_db(freqs: np.ndarray, cfg: AfeConfig | None = None, *, include_gain: bool = False) -> np.ndarray:
    """Magnitude response of the analog chain, in dB.

    Two real high-pass poles and a 2-pole Butterworth low-pass, evaluated as a
    continuous-time system - this is the Bode plot the LTspice schematic would produce.
    """
    cfg = cfg or AfeConfig()
    hp_hz, lp_hz = realised_corners(cfg)
    w = 2.0 * np.pi * np.asarray(freqs, dtype=float)

    whp = 2.0 * np.pi * hp_hz
    wlp = 2.0 * np.pi * lp_hz
    s = 1j * w
    # Two cascaded single high-pass poles: one in the DC-blocking feedback around the
    # instrumentation amp, one passive between stages.
    h = (s / (s + whp)) ** 2
    # 2-pole Butterworth low-pass (equal-component Sallen-Key, Q = 1/sqrt(2)).
    h = h * wlp**2 / (s**2 + np.sqrt(2.0) * wlp * s + wlp**2)
    if include_gain:
        h = h * cfg.gain
    with np.errstate(divide="ignore"):
        return 20.0 * np.log10(np.abs(h))


def mains_rejection_db(cfg: AfeConfig | None = None, mains_hz: float = 50.0) -> dict[str, float]:
    """How much of the mains interference each mechanism removes, and what is left.

    The residual is the reason the firmware carries a 50 Hz notch: a 2-pole low-pass at
    40 Hz gives only a few dB at 50 Hz, so the analog chain alone does not finish the job.
    """
    cfg = cfg or AfeConfig()
    analog_db = float(afe_response_db(np.array([mains_hz]), cfg)[0])
    return {
        "right_leg_drive_db": -cfg.rld_reduction_db,
        "cmrr_db": -cfg.cmrr_db,
        "analog_filter_db": analog_db,
        "total_db": -cfg.rld_reduction_db - cfg.cmrr_db + analog_db,
    }


# --------------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------------


@dataclass
class AcquisitionResult:
    """What the firmware would read, plus the intermediate signals for analysis."""

    counts: np.ndarray  #: raw ADC codes, integers
    volts: np.ndarray  #: ADC input voltage before quantisation
    saturated: np.ndarray  #: per-sample flag: the amplifier hit a rail
    fs: float
    components: list[Component] = field(default_factory=list)

    @property
    def saturated_fraction(self) -> float:
        return float(np.mean(self.saturated))


def _analog_stage(x_mv: np.ndarray, fs_os: float, cfg: AfeConfig) -> np.ndarray:
    """High-pass, gain, low-pass - evaluated at the oversampled rate.

    Applied causally, because an analog filter is causal. The high-pass is primed to the
    steady state of the incoming DC level instead of starting from zero, which is what a
    real amplifier's fast-restore does; without it the first several seconds of every
    record would be a settling transient rather than a signal.
    """
    hp_hz, lp_hz = realised_corners(cfg)

    hp_sos = np.vstack([sps.butter(1, hp_hz / (fs_os / 2), btype="highpass", output="sos")] * 2).reshape(-1, 6)
    zi = sps.sosfilt_zi(hp_sos) * float(x_mv[0])
    y, _ = sps.sosfilt(hp_sos, x_mv, zi=zi)

    y = y * cfg.gain / 1000.0  # mV in, volts out

    lp_sos = sps.butter(2, lp_hz / (fs_os / 2), btype="lowpass", output="sos")
    zi = sps.sosfilt_zi(lp_sos) * float(y[0])
    y, _ = sps.sosfilt(lp_sos, y, zi=zi)
    return y


def _inl_curve(codes: np.ndarray, cfg: AdcConfig) -> np.ndarray:
    """Integral nonlinearity as a function of code.

    Deterministic, not random: INL is a fixed characteristic of a particular converter,
    so the same code always reads back with the same error. Modelling it as noise would
    understate it, because averaging removes noise and does not remove INL.
    """
    u = codes / cfg.codes
    return cfg.inl_lsb * (0.7 * np.sin(2 * np.pi * 3.0 * u) + 0.3 * np.sin(2 * np.pi * 7.0 * u + 1.1))


def acquire(
    ecg_mv: np.ndarray,
    fs: float = DEFAULT_FS,
    *,
    afe: AfeConfig | None = None,
    electrode: ElectrodeConfig | None = None,
    adc: AdcConfig | None = None,
    leads_off: np.ndarray | None = None,
) -> AcquisitionResult:
    """Run a patient-referred ECG in mV through the full acquisition chain.

    Parameters
    ----------
    ecg_mv : np.ndarray
        The differential signal at the electrodes, in mV - e.g. a MIT-BIH record.
    leads_off : np.ndarray | None
        Optional per-sample boolean. Where true the amplifier input floats and the
        output rails, which is what the AD8232's leads-off condition looks like at the
        ADC and is precisely the input that made the detector invent beats (D-08).
    """
    afe = afe or AfeConfig()
    electrode = electrode or ElectrodeConfig()
    adc = adc or AdcConfig()
    x = np.asarray(ecg_mv, dtype=float)
    if x.ndim != 1 or x.size < 2:
        raise ValueError("ecg_mv must be a 1-D array of at least 2 samples")
    if fs <= 0:
        raise ValueError(f"fs must be positive, got {fs}")

    n = x.size
    os = afe.oversample
    fs_os = fs * os
    rng = np.random.default_rng(electrode.seed)

    # --- oversample, so that sub-sample timing jitter has something to interpolate ---
    x_os = sps.resample_poly(x, os, 1) if os > 1 else x.copy()
    x_os = x_os[: n * os]
    t_os = np.arange(x_os.size) / fs_os

    # --- everything the electrodes add before the amplifier sees it ---
    x_os = x_os + electrode.half_cell_mv
    x_os = x_os + electrode.drift_mv_per_min * (t_os / 60.0)
    if electrode.motion_amp_mv:
        x_os = x_os + electrode.motion_amp_mv * np.sin(2 * np.pi * electrode.motion_hz * t_os)
    if electrode.emg_std_mv:
        x_os = x_os + rng.normal(0.0, electrode.emg_std_mv, x_os.size)
    if electrode.mains_cm_v:
        # Common-mode mains, reduced by the right-leg drive and then by the amplifier's
        # CMRR, appears at the input as a differential error of this size.
        attenuation = 10.0 ** (-(afe.rld_reduction_db + afe.cmrr_db) / 20.0)
        x_os = x_os + electrode.mains_cm_v * 1000.0 * attenuation * np.sin(
            2 * np.pi * electrode.mains_hz * t_os
        )

    # --- amplifier ---
    v_os = _analog_stage(x_os, fs_os, afe) + afe.vref_v

    if leads_off is not None:
        lo = np.asarray(leads_off, dtype=bool)
        if lo.size != n:
            raise ValueError(f"leads_off must have {n} elements, got {lo.size}")
        v_os[np.repeat(lo, os)[: v_os.size]] = afe.supply_v

    # --- rails: saturation is clipping, not wrapping, and it is worth flagging ---
    sat_os = (v_os <= 0.0) | (v_os >= afe.supply_v)
    v_os = np.clip(v_os, 0.0, afe.supply_v)

    # --- sample: nominal grid displaced by timing jitter ---
    rng_adc = np.random.default_rng(adc.seed)
    t_sample = np.arange(n) / fs
    if adc.jitter_us:
        t_sample = t_sample + rng_adc.normal(0.0, adc.jitter_us * 1e-6, n)
        t_sample = np.clip(t_sample, t_os[0], t_os[-1])
    v = np.interp(t_sample, t_os, v_os)
    saturated = np.interp(t_sample, t_os, sat_os.astype(float)) > 0.5

    # --- converter errors, then quantisation ---
    ideal_code = (v - adc.vmin_v) / adc.lsb_v
    code = ideal_code * (1.0 + adc.gain_error) + adc.offset_lsb
    code = code + _inl_curve(np.clip(code, 0, adc.codes - 1), adc)
    if adc.noise_lsb:
        code = code + rng_adc.normal(0.0, adc.noise_lsb, n)
    counts = np.clip(np.round(code), 0, adc.codes - 1).astype(np.int32)

    return AcquisitionResult(
        counts=counts,
        volts=v,
        saturated=saturated,
        fs=fs,
        components=synthesise_components(afe),
    )


def counts_to_mv(
    counts: np.ndarray,
    *,
    afe: AfeConfig | None = None,
    adc: AdcConfig | None = None,
) -> np.ndarray:
    """Convert ADC codes back to patient-referred mV - the firmware's scaling step.

    This undoes the gain and the level shift, which is all a device can undo. It cannot
    undo the analog band-limiting, the quantisation, the INL or the converter's offset
    and gain error, because it does not know them. That asymmetry is the whole reason
    this module exists: the recovered signal is *not* the original, and the cost of the
    difference is measurable rather than assumed.
    """
    afe = afe or AfeConfig()
    adc = adc or AdcConfig()
    volts = adc.vmin_v + np.asarray(counts, dtype=float) * adc.lsb_v
    return (volts - afe.vref_v) * 1000.0 / afe.gain


def describe(cfg: AfeConfig | None = None) -> str:
    """A human-readable design summary - goes into docs/ and answers the BOM question."""
    cfg = cfg or AfeConfig()
    hp, lp = synthesise_components(cfg)
    rej = mains_rejection_db(cfg)
    lines = [
        f"Analog front-end: gain {cfg.gain:.0f} V/V, {cfg.supply_v} V single supply, "
        f"output centred at {cfg.vref_v} V",
        f"  {hp}",
        f"  {lp}",
        f"  input range before saturation: ±{1000.0 * min(cfg.vref_v, cfg.supply_v - cfg.vref_v) / cfg.gain:.2f} mV"
        f"   (IEC 60601-2-27 expects ±5 mV)",
        f"  resolution referred to input:  {1e6 * AdcConfig().lsb_v / cfg.gain:.2f} µV per LSB"
        f"   (a 0.1 mV ECG feature is ~{0.1 / (1e3 * AdcConfig().lsb_v / cfg.gain):.0f} codes)",
        "50 Hz mains, path by path:",
        f"  right-leg drive       {rej['right_leg_drive_db']:>7.1f} dB",
        f"  amplifier CMRR        {rej['cmrr_db']:>7.1f} dB",
        f"  analog low-pass       {rej['analog_filter_db']:>7.1f} dB   <- only a few dB; "
        "this is why the firmware still needs a notch",
        f"  total                 {rej['total_db']:>7.1f} dB",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------


def _self_test() -> None:
    """Run: python sim/afe_model.py"""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    sys.path.insert(0, str(ROOT / "dsp"))
    from synth import SynthConfig, synthetic_ecg  # noqa: E402

    cfg = AfeConfig()

    # --- component synthesis ---
    check("nearest_e12 snaps within its decade", nearest_e12(4.5e3) == 4.7e3, f"{nearest_e12(4.5e3):.0f} Ω")
    check("nearest_e12 handles decade boundaries", nearest_e12(9.7e5) == 1e6, f"{nearest_e12(9.7e5):.3g} Ω")
    hp, lp = synthesise_components(cfg)
    check("high-pass realised within 10 % of target", abs(hp.error_pct) < 10, f"{hp.error_pct:+.1f} %")
    check("low-pass realised within 10 % of target", abs(lp.error_pct) < 10, f"{lp.error_pct:+.1f} %")
    check("resistors are buildable (10 kΩ - 10 MΩ)", all(1e4 <= c.r_ohms <= 1e7 for c in (hp, lp)),
          f"{_eng(hp.r_ohms, 'Ω')}, {_eng(lp.r_ohms, 'Ω')}")

    # --- analog response ---
    f = np.array([0.05, hp.realised_hz, 10.0, lp.realised_hz, 50.0, 150.0])
    db = afe_response_db(f, cfg)
    check("passband (10 Hz) is flat within 1 dB", abs(db[2]) < 1.0, f"{db[2]:+.2f} dB")
    check("two high-pass poles give -6 dB at the corner", abs(db[1] - (-6.0)) < 1.0, f"{db[1]:+.2f} dB")
    check("low-pass corner is -3 dB", abs(db[3] - (-3.0)) < 0.5, f"{db[3]:+.2f} dB")
    check("0.05 Hz baseline wander strongly rejected", db[0] < -35, f"{db[0]:+.1f} dB")
    check("150 Hz EMG rejected by > 20 dB", db[5] < -20, f"{db[5]:+.1f} dB")
    check("50 Hz survives the analog filter (notch still needed)", db[4] > -12, f"{db[4]:+.2f} dB")
    check("response is monotonic across the passband edge", db[3] < db[2], f"{db[3]:.1f} < {db[2]:.1f}")

    # --- the offset problem the high-pass exists to solve ---
    sig, peaks = synthetic_ecg(SynthConfig(duration_s=20, hr_bpm=72, fs=DEFAULT_FS))
    quiet = ElectrodeConfig(mains_cm_v=0.0, drift_mv_per_min=0.0)
    res = acquire(sig, DEFAULT_FS, afe=cfg, electrode=quiet, adc=AdcConfig(noise_lsb=0.0, inl_lsb=0.0, offset_lsb=0.0, gain_error=0.0))
    check("120 mV half-cell offset does not saturate the amplifier", res.saturated_fraction < 0.01,
          f"{100 * res.saturated_fraction:.2f} % of samples clipped")
    check("output stays inside the rails", bool(res.volts.min() >= 0 and res.volts.max() <= cfg.supply_v),
          f"{res.volts.min():.3f} - {res.volts.max():.3f} V")
    check("output sits near mid-supply", abs(float(np.median(res.volts)) - cfg.vref_v) < 0.15,
          f"median {np.median(res.volts):.3f} V")

    # --- a STEP in offset is a different matter, and it is the D-22 mechanism ---
    # The high-pass is primed at session start (fast restore), so a standing offset is
    # rejected immediately. A step - which is what reattaching an electrode is - cannot
    # be. The amplifier rails until the 0.5 Hz high-pass has settled, and this is the
    # analog root of the reconnection transient that produced a fabricated 190 bpm in
    # session 006. It is visible here one layer below where it was originally found.
    long_sig, _ = synthetic_ecg(SynthConfig(duration_s=60, hr_bpm=72, fs=DEFAULT_FS))
    n_half = long_sig.size // 2
    step = np.zeros(long_sig.size)
    step[n_half:] = 400.0  # a fresh electrode brings a new half-cell potential with it
    res_step = acquire(long_sig + step, DEFAULT_FS, afe=cfg,
                       electrode=ElectrodeConfig(half_cell_mv=0.0, mains_cm_v=0.0, drift_mv_per_min=0.0))
    win = lambda sl: float(res_step.saturated[sl].mean())
    before = win(slice(n_half - int(2 * DEFAULT_FS), n_half))
    during = win(slice(n_half, n_half + int(2 * DEFAULT_FS)))
    after = win(slice(n_half + int(5 * DEFAULT_FS), None))
    check("a step change in electrode offset saturates the amplifier", during > 0.5, f"{100 * during:.1f} % clipped")
    check("the amplifier was not saturated before the step", before < 0.05, f"{100 * before:.1f} % clipped")
    check("the high-pass recovers within 5 s of the step", after < 0.01, f"{100 * after:.2f} % clipped")

    # --- quantisation and round-trip ---
    ideal = AdcConfig(noise_lsb=0.0, inl_lsb=0.0, offset_lsb=0.0, gain_error=0.0)
    res_q = acquire(sig, DEFAULT_FS, afe=cfg, electrode=quiet, adc=ideal)
    back = counts_to_mv(res_q.counts, afe=cfg, adc=ideal)
    check("counts are inside the 12-bit range", bool(res_q.counts.min() >= 0 and res_q.counts.max() <= 4095),
          f"{res_q.counts.min()} - {res_q.counts.max()}")
    check("counts use a useful share of the range", int(res_q.counts.max() - res_q.counts.min()) > 500,
          f"{int(np.ptp(res_q.counts))} codes of 4096")
    check("round-trip recovers ECG-scale amplitudes", 0.5 < float(np.ptp(back)) < 5.0, f"{np.ptp(back):.2f} mV peak-to-peak")
    lsb_mv = ideal.lsb_v * 1000.0 / cfg.gain
    check("one LSB is well below ECG feature size", lsb_mv < 0.01, f"{1000 * lsb_mv:.2f} µV per LSB")

    # --- the front end is not a transparent pipe: it delays and it distorts phase ---
    # Comparing sample-for-sample against the source fails, and for a reason that is
    # not a defect: the analog chain is causal, so it has group delay, while the
    # reference chain (`clean_offline`) is zero-phase and has none. Measure the delay
    # instead of asserting it away - it is a real design number that the end-to-end
    # latency budget (SR-02) has to carry.
    from filters import FilterConfig, clean_offline  # noqa: E402

    ref = clean_offline(sig, FilterConfig())[400:-400]
    got = clean_offline(back, FilterConfig())[400:-400]
    ref = ref - ref.mean()
    got = got - got.mean()
    best_lag, best_r = 0, -1.0
    for lag in range(0, 13):  # `got` lags `ref`; slide it back
        a = ref[: ref.size - lag]
        b = got[lag:]
        rr = float(np.corrcoef(a, b)[0, 1])
        if rr > best_r:
            best_lag, best_r = lag, rr
    delay_ms = 1000.0 * best_lag / DEFAULT_FS
    check("analog group delay is a few ms, not tens", 0.0 < delay_ms < 15.0, f"{delay_ms:.2f} ms")
    check("aligned round-trip correlates with the source (r > 0.95)", best_r > 0.95, f"r = {best_r:.4f} at {delay_ms:.2f} ms")

    # --- and the check that actually matters: can the detector still find the beats? ---
    from pan_tompkins import compare_to_reference, detect_qrs  # noqa: E402

    for label, elec, adcc in (
        ("quiet electrodes, ideal ADC", quiet, ideal),
        ("quiet electrodes, real ADC", quiet, AdcConfig()),
        ("mains + drift + 50 us jitter", ElectrodeConfig(), AdcConfig(jitter_us=50)),
    ):
        recovered = counts_to_mv(acquire(sig, DEFAULT_FS, afe=cfg, electrode=elec, adc=adcc).counts,
                                 afe=cfg, adc=adcc)
        m = compare_to_reference(detect_qrs(recovered).r_peaks, peaks, DEFAULT_FS)
        check(f"detection survives acquisition ({label})",
              m["sensitivity"] > 0.99 and m["ppv"] > 0.99,
              f"Se {100 * m['sensitivity']:.2f} %, PPV {100 * m['ppv']:.2f} %, timing {m['mean_error_ms']:.2f} ms")
        check(f"timing bias equals the group delay ({label})", abs(m["mean_error_ms"] - delay_ms) < 3.0,
              f"{m['mean_error_ms']:.2f} ms vs {delay_ms:.2f} ms of analog delay")

    # --- headroom: the reason the default gain is 330 and not the datasheet's 1100 ---
    # A +/-5 mV input is what IEC 60601-2-27 expects a monitor to accept, and several
    # MIT-BIH records genuinely reach 4 mV. The check is run on a synthetic signal
    # scaled to that amplitude so it does not depend on the dataset being present.
    big = sig * (4.5 / float(np.max(np.abs(sig))))
    hot = acquire(big, DEFAULT_FS, afe=AfeConfig(gain=1100.0), electrode=quiet, adc=ideal)
    ok_gain = acquire(big, DEFAULT_FS, afe=cfg, electrode=quiet, adc=ideal)
    check("the datasheet gain of 1100 clips a 4.5 mV ECG", hot.saturated_fraction > 0.001,
          f"{100 * hot.saturated_fraction:.2f} % clipped")
    check("the chosen gain of 330 does not", ok_gain.saturated_fraction < 1e-4,
          f"{100 * ok_gain.saturated_fraction:.3f} % clipped")
    check("+/-5 mV fits inside the rails at the chosen gain",
          1000.0 * min(cfg.vref_v, cfg.supply_v - cfg.vref_v) / cfg.gain >= 5.0,
          f"+/-{1000.0 * min(cfg.vref_v, cfg.supply_v - cfg.vref_v) / cfg.gain:.2f} mV")

    # --- INL is deterministic, noise is not ---
    a = acquire(sig, DEFAULT_FS, electrode=quiet, adc=AdcConfig(noise_lsb=0.0, seed=1))
    b = acquire(sig, DEFAULT_FS, electrode=quiet, adc=AdcConfig(noise_lsb=0.0, seed=2))
    check("INL repeats exactly across seeds", np.array_equal(a.counts, b.counts), "identical")
    c = acquire(sig, DEFAULT_FS, electrode=quiet, adc=AdcConfig(noise_lsb=1.1, seed=1))
    d = acquire(sig, DEFAULT_FS, electrode=quiet, adc=AdcConfig(noise_lsb=1.1, seed=2))
    check("ADC noise differs across seeds", not np.array_equal(c.counts, d.counts),
          f"{int(np.sum(c.counts != d.counts))} samples differ")

    # --- determinism for a fixed seed, which regression tests depend on ---
    e = acquire(sig, DEFAULT_FS, electrode=ElectrodeConfig(seed=7), adc=AdcConfig(seed=7, jitter_us=20))
    f2 = acquire(sig, DEFAULT_FS, electrode=ElectrodeConfig(seed=7), adc=AdcConfig(seed=7, jitter_us=20))
    check("same seed reproduces the acquisition", np.array_equal(e.counts, f2.counts), "identical")

    # --- mains ---
    loud = ElectrodeConfig(mains_cm_v=5.0, drift_mv_per_min=0.0)
    res_m = acquire(sig, DEFAULT_FS, afe=cfg, electrode=loud, adc=ideal)
    back_m = counts_to_mv(res_m.counts, afe=cfg, adc=ideal)
    freqs, psd = sps.welch(back_m, fs=DEFAULT_FS, nperseg=2048)
    band = (freqs > 45) & (freqs < 55)
    check("5 V of common-mode mains leaves a measurable 50 Hz residual",
          float(psd[band].max()) > 10 * float(np.median(psd[(freqs > 60) & (freqs < 120)])),
          f"{10 * np.log10(psd[band].max() / np.median(psd[(freqs > 60) & (freqs < 120)])):.1f} dB above the floor")
    rej = mains_rejection_db(cfg)
    check("mains rejection is dominated by CMRR and the RLD, not the filter",
          abs(rej["analog_filter_db"]) < abs(rej["cmrr_db"] + rej["right_leg_drive_db"]),
          f"filter {rej['analog_filter_db']:.1f} dB vs {rej['cmrr_db'] + rej['right_leg_drive_db']:.1f} dB")

    # --- leads-off rails the output, which is what the flat-line gate must survive ---
    lo = np.zeros(sig.size, dtype=bool)
    lo[int(5 * DEFAULT_FS) : int(10 * DEFAULT_FS)] = True
    res_lo = acquire(sig, DEFAULT_FS, afe=cfg, electrode=quiet, adc=ideal, leads_off=lo)
    off = res_lo.counts[int(6 * DEFAULT_FS) : int(9 * DEFAULT_FS)]
    on = res_lo.counts[: int(4 * DEFAULT_FS)]
    check("leads-off rails the ADC to full scale", int(off.min()) >= 4090, f"min code {int(off.min())}")
    check("leads-off is distinguishable from a real signal", int(np.ptp(off)) < int(np.ptp(on)) / 10,
          f"{int(np.ptp(off))} codes vs {int(np.ptp(on))}")

    # --- timing jitter shifts samples in time, not amplitude ---
    j0 = acquire(sig, DEFAULT_FS, electrode=quiet, adc=AdcConfig(noise_lsb=0.0, jitter_us=0))
    j1 = acquire(sig, DEFAULT_FS, electrode=quiet, adc=AdcConfig(noise_lsb=0.0, jitter_us=200, seed=3))
    err_lsb = float(np.std(j1.counts.astype(float) - j0.counts.astype(float)))
    check("200 µs of jitter perturbs the signal but does not destroy it", 0.0 < err_lsb < 40.0,
          f"{err_lsb:.1f} LSB RMS difference")

    # --- input validation ---
    for maker, label in (
        (lambda: AfeConfig(hp_hz=50, lp_hz=40), "hp above lp"),
        (lambda: AfeConfig(gain=0), "zero gain"),
        (lambda: AfeConfig(vref_v=4.0), "vref outside supply"),
        (lambda: AdcConfig(bits=4), "4-bit ADC"),
        (lambda: ElectrodeConfig(mains_cm_v=-1), "negative mains"),
    ):
        try:
            maker()
            check(f"rejects invalid config ({label})", False, "no error raised")
        except ValueError:
            check(f"rejects invalid config ({label})", True, "ValueError")
    try:
        acquire(np.array([1.0]), DEFAULT_FS)
        check("rejects a one-sample input", False, "no error raised")
    except ValueError:
        check("rejects a one-sample input", True, "ValueError")

    width = max(len(n_) for n_, _, _ in checks)
    passed = sum(ok for _, ok, _ in checks)
    print("\n" + describe(cfg))
    print(f"\nafe_model.py self-test\n" + "-" * (width + 30))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print("-" * (width + 30))
    print(f"{passed}/{len(checks)} checks passed\n")
    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
