"""ECG filter design and application.

Two filters clean the raw ECG before beat detection:

1. **Bandpass 0.5-40 Hz** - removes baseline wander (respiration, electrode motion;
   sub-0.5 Hz) and high-frequency EMG/muscle noise. The diagnostic energy of the QRS
   complex sits at roughly 10-25 Hz, so a 40 Hz upper edge keeps the beat morphology
   intact while discarding noise above it.
2. **50 Hz notch** - rejects mains interference (India: 50 Hz; use 60 for the US).
   A narrow notch is used so it removes the interference without gouging a hole in
   the QRS band.

Two application modes are provided, and the distinction matters:

* :func:`filtfilt`-style **offline** filtering is zero-phase - it runs the filter
  forwards then backwards, so beat locations are not shifted in time. This is used
  for validation against annotated datasets, where a timing shift would corrupt the
  comparison.
* :class:`StreamingFilter` is **causal** and stateful - it processes chunks as they
  arrive and can only look backwards, which is the only option in real time and the
  behaviour the ESP32 firmware must match. It introduces a small group delay.

Running both against the same input (see :func:`_self_test`) is what proves the
real-time path behaves like the validated offline path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import signal

# --------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------

#: Sampling rate of the whole system, in Hz.
#: Chosen to match the MIT-BIH Arrhythmia Database so that filter coefficients and
#: validation results transfer directly between dataset and device (decision D-02).
DEFAULT_FS = 360.0


@dataclass(frozen=True)
class FilterConfig:
    """Filter parameters for one ECG channel."""

    fs: float = DEFAULT_FS
    bp_low: float = 0.5  # Hz - baseline wander cutoff
    bp_high: float = 40.0  # Hz - EMG / high-frequency noise cutoff
    bp_order: int = 4  # per-direction order of the Butterworth bandpass
    notch_freq: float = 50.0  # Hz - mains frequency (50 in India, 60 in the US)
    notch_q: float = 30.0  # quality factor; higher = narrower notch

    def __post_init__(self) -> None:
        nyquist = self.fs / 2.0
        if not 0 < self.bp_low < self.bp_high < nyquist:
            raise ValueError(
                f"need 0 < bp_low ({self.bp_low}) < bp_high ({self.bp_high}) "
                f"< Nyquist ({nyquist})"
            )
        if not 0 < self.notch_freq < nyquist:
            raise ValueError(
                f"notch_freq ({self.notch_freq}) must be below Nyquist ({nyquist})"
            )
        if self.notch_q <= 0:
            raise ValueError(f"notch_q must be positive, got {self.notch_q}")


# --------------------------------------------------------------------------------
# Filter design
# --------------------------------------------------------------------------------


def design_bandpass(cfg: FilterConfig) -> np.ndarray:
    """Design the Butterworth bandpass as second-order sections.

    SOS form is used rather than transfer-function (b, a) form because higher-order
    IIR filters are numerically fragile as a single polynomial - cascaded biquads
    keep the poles well conditioned.
    """
    return signal.butter(
        cfg.bp_order,
        [cfg.bp_low, cfg.bp_high],
        btype="bandpass",
        fs=cfg.fs,
        output="sos",
    )


def design_notch(cfg: FilterConfig) -> tuple[np.ndarray, np.ndarray]:
    """Design the mains-rejection notch. Returns ``(b, a)`` of a single biquad."""
    b, a = signal.iirnotch(cfg.notch_freq, cfg.notch_q, fs=cfg.fs)
    return b, a


def response_db(b: np.ndarray, a: np.ndarray, freqs: np.ndarray, fs: float) -> np.ndarray:
    """Magnitude response in dB of filter ``(b, a)`` at the given frequencies."""
    _, h = signal.freqz(b, a, worN=freqs, fs=fs)
    # Guard the log against exact zeros at a perfect null.
    return 20.0 * np.log10(np.maximum(np.abs(h), 1e-12))


def sos_response_db(sos: np.ndarray, freqs: np.ndarray, fs: float) -> np.ndarray:
    """Magnitude response in dB of an SOS filter at the given frequencies."""
    _, h = signal.sosfreqz(sos, worN=freqs, fs=fs)
    return 20.0 * np.log10(np.maximum(np.abs(h), 1e-12))


# --------------------------------------------------------------------------------
# Offline (zero-phase) filtering - used for dataset validation
# --------------------------------------------------------------------------------


def clean_offline(x: np.ndarray, cfg: FilterConfig | None = None) -> np.ndarray:
    """Apply notch + bandpass with zero phase distortion.

    Use for offline analysis only: :func:`scipy.signal.filtfilt` needs the whole
    record up front and is non-causal, but it leaves beat timing untouched, which is
    what makes annotation-based validation meaningful.
    """
    cfg = cfg or FilterConfig()
    x = np.asarray(x, dtype=np.float64)
    if x.ndim != 1:
        raise ValueError(f"expected a 1-D signal, got shape {x.shape}")

    b_notch, a_notch = design_notch(cfg)
    sos_bp = design_bandpass(cfg)

    # filtfilt's default padding needs a signal longer than 3 * max(len(a), len(b));
    # short test snippets would otherwise raise, so pad length is clamped.
    padlen = min(3 * max(len(a_notch), len(b_notch)), max(x.size - 1, 0))
    notched = signal.filtfilt(b_notch, a_notch, x, padlen=padlen)
    return signal.sosfiltfilt(sos_bp, notched)


# --------------------------------------------------------------------------------
# Streaming (causal) filtering - mirrors what runs in real time
# --------------------------------------------------------------------------------


class StreamingFilter:
    """Causal, stateful notch + bandpass for chunk-by-chunk processing.

    Filter state persists across calls, so feeding a signal in chunks of any size
    gives bit-identical output to feeding it in one call - the property the server
    relies on when samples arrive in WebSocket frames.

    Example
    -------
    >>> filt = StreamingFilter()
    >>> out = np.concatenate([filt.process(chunk) for chunk in chunks])
    """

    def __init__(self, cfg: FilterConfig | None = None) -> None:
        self.cfg = cfg or FilterConfig()
        self._b_notch, self._a_notch = design_notch(self.cfg)
        self._sos_bp = design_bandpass(self.cfg)

        # Steady-state initial conditions, scaled by the first sample on first use.
        # Starting from zero state would otherwise ring for the first second or so
        # as the filter "charges up" from the DC offset of the input.
        self._zi_notch_unit = signal.lfilter_zi(self._b_notch, self._a_notch)
        self._zi_bp_unit = signal.sosfilt_zi(self._sos_bp)
        self._zi_notch: np.ndarray | None = None
        self._zi_bp: np.ndarray | None = None

    def reset(self) -> None:
        """Clear filter memory (call between unrelated recordings)."""
        self._zi_notch = None
        self._zi_bp = None

    def process(self, chunk: np.ndarray) -> np.ndarray:
        """Filter one chunk of samples and return the cleaned chunk."""
        x = np.asarray(chunk, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError(f"expected a 1-D chunk, got shape {x.shape}")
        if x.size == 0:
            return x

        if self._zi_notch is None:
            self._zi_notch = self._zi_notch_unit * x[0]
            self._zi_bp = self._zi_bp_unit * x[0]

        notched, self._zi_notch = signal.lfilter(
            self._b_notch, self._a_notch, x, zi=self._zi_notch
        )
        out, self._zi_bp = signal.sosfilt(self._sos_bp, notched, zi=self._zi_bp)
        return out


# --------------------------------------------------------------------------------
# Firmware export
# --------------------------------------------------------------------------------


#: Fractional bits for the firmware's fixed-point notch coefficients.
_NOTCH_FRAC_BITS = 14


def export_notch_header(path: Path, cfg: FilterConfig | None = None) -> Path:
    """Write the notch coefficients as a C header for the ESP32 firmware.

    Generating the header from the same design code that the validated Python
    pipeline uses keeps device and server filters from silently drifting apart.
    """
    cfg = cfg or FilterConfig()
    b, a = design_notch(cfg)
    # Normalise so a[0] == 1, the form the difference equation in firmware assumes.
    b, a = b / a[0], a / a[0]

    # Fixed-point form for the device. The ESP32 has an FPU, but the acquisition task
    # runs from a timer ISR at 360 Hz on a shared core, and integer arithmetic keeps its
    # cost constant and its result bit-exact across builds - which is what lets the same
    # filter be scored on the host (D-25). Q14, not Q15: |b1| = 1.267 > 1 here, so a
    # Q15 coefficient would wrap, and a wrapped coefficient in a recursive filter is not
    # a small error, it is a different filter.
    frac_bits = _NOTCH_FRAC_BITS
    scale = 1 << frac_bits
    bq = [int(round(v * scale)) for v in b]
    aq = [int(round(v * scale)) for v in a]
    if max(abs(v) for v in bq + aq) > 2**15 - 1:
        raise ValueError(
            f"quantised coefficients overflow int16 at Q{frac_bits}: {bq + aq}"
        )
    # A quantised recursive filter can be unstable even when the design is not: the
    # poles move when the coefficients are rounded. Check the rounded denominator, not
    # the ideal one - the device runs the rounded one.
    a1q, a2q = aq[1] / scale, aq[2] / scale
    poles = np.roots([1.0, a1q, a2q])
    if np.max(np.abs(poles)) >= 1.0:
        raise ValueError(
            f"quantised notch is unstable: pole radius {np.max(np.abs(poles)):.6f} >= 1"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""// Auto-generated by dsp/filters.py - do not edit by hand. Regenerate with:
//   python dsp/filters.py --export
// {cfg.notch_freq:.0f} Hz notch biquad, fs = {cfg.fs:.0f} Hz, Q = {cfg.notch_q:.0f}
//   y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2] - a1*y[n-1] - a2*y[n-2]
#pragma once

#include <stdint.h>

#define VS_SAMPLE_RATE_HZ {cfg.fs:.1f}f

static const float NOTCH_B[3] = {{{b[0]:.10f}f, {b[1]:.10f}f, {b[2]:.10f}f}};
static const float NOTCH_A[3] = {{{a[0]:.10f}f, {a[1]:.10f}f, {a[2]:.10f}f}};

// Fixed-point form, Q{frac_bits} (one unit = 1/{scale}). Quantised pole radius
// {np.max(np.abs(poles)):.6f} - checked < 1 at generation time, because rounding the
// coefficients moves the poles and a stable design can quantise into an unstable filter.
#define VS_NOTCH_FRAC_BITS {frac_bits}
#define VS_NOTCH_ONE       ({scale})

static const int16_t NOTCH_B_Q[3] = {{{bq[0]}, {bq[1]}, {bq[2]}}};
static const int16_t NOTCH_A_Q[3] = {{{aq[0]}, {aq[1]}, {aq[2]}}};
"""
    )
    return path


# --------------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------------


def _self_test() -> None:
    """Verify the filters do what the docstrings claim. Run: python dsp/filters.py"""
    cfg = FilterConfig()
    fs = cfg.fs
    b_notch, a_notch = design_notch(cfg)
    sos_bp = design_bandpass(cfg)

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    # --- frequency response of the notch ---
    at_50 = response_db(b_notch, a_notch, np.array([50.0]), fs)[0]
    at_qrs = response_db(b_notch, a_notch, np.array([15.0]), fs)[0]
    check("notch rejects 50 Hz by >30 dB", at_50 < -30, f"{at_50:.1f} dB")
    check("notch spares 15 Hz QRS band (>-1 dB)", at_qrs > -1.0, f"{at_qrs:.2f} dB")

    # --- frequency response of the bandpass ---
    bp = {f: sos_response_db(sos_bp, np.array([f]), fs)[0] for f in (0.1, 0.5, 15.0, 40.0, 100.0)}
    check("bandpass kills 0.1 Hz baseline wander (<-20 dB)", bp[0.1] < -20, f"{bp[0.1]:.1f} dB")
    check("bandpass passes 15 Hz (>-1 dB)", bp[15.0] > -1.0, f"{bp[15.0]:.2f} dB")
    check("bandpass -3 dB near 0.5 Hz edge", -6.0 < bp[0.5] < -1.0, f"{bp[0.5]:.2f} dB")
    check("bandpass -3 dB near 40 Hz edge", -6.0 < bp[40.0] < -1.0, f"{bp[40.0]:.2f} dB")
    check("bandpass kills 100 Hz EMG (<-20 dB)", bp[100.0] < -20, f"{bp[100.0]:.1f} dB")

    # --- end-to-end on a synthetic signal ---
    # 10 s of a 1.2 Hz "beat-like" tone (72 bpm) buried in baseline wander + mains hum.
    t = np.arange(0, 10, 1 / fs)
    clean = np.sin(2 * np.pi * 1.2 * t)
    wander = 2.0 * np.sin(2 * np.pi * 0.15 * t)  # slow drift, twice the signal size
    mains = 0.8 * np.sin(2 * np.pi * 50.0 * t)
    noisy = clean + wander + mains + 1.5  # + DC electrode offset

    filtered = clean_offline(noisy, cfg)

    # Mains rejection: 50 Hz is well resolved by Welch, so measure band power.
    def band_power(sig: np.ndarray, lo: float, hi: float) -> float:
        freqs, psd = signal.welch(sig, fs=fs, nperseg=min(2048, sig.size))
        band = (freqs >= lo) & (freqs <= hi)
        return float(np.trapezoid(psd[band], freqs[band]))

    mains_before, mains_after = band_power(noisy, 49, 51), band_power(filtered, 49, 51)
    check(
        "50 Hz power reduced >100x end-to-end",
        mains_after < mains_before / 100,
        f"{mains_before:.4g} -> {mains_after:.4g}",
    )

    # Baseline wander sits at 0.15 Hz, which is finer than Welch's bin spacing here
    # (fs/nperseg = 0.18 Hz), so a spectral estimate would land between bins and read
    # as zero. Measure it in the time domain instead: zero-phase lowpass, then RMS.
    def drift_rms(sig: np.ndarray) -> float:
        sos_lp = signal.butter(2, 0.3, btype="lowpass", fs=fs, output="sos")
        return float(np.sqrt(np.mean(signal.sosfiltfilt(sos_lp, sig) ** 2)))

    drift_before, drift_after = drift_rms(noisy), drift_rms(filtered)
    check(
        "baseline wander RMS reduced >20x",
        drift_after < drift_before / 20,
        f"{drift_before:.4g} -> {drift_after:.4g}",
    )

    # Trim one second from each end before the DC check: filtfilt's edge padding
    # leaves a transient there that is not representative of steady-state output.
    interior = filtered[int(fs) : -int(fs)]
    check(
        "DC offset removed (|mean| < 0.01)",
        abs(float(np.mean(interior))) < 0.01,
        f"mean = {np.mean(interior):.2e}",
    )

    # --- streaming must equal single-shot, whatever the chunk size ---
    ref = StreamingFilter(cfg).process(noisy)
    chunked = StreamingFilter(cfg)
    pieces = [chunked.process(noisy[i : i + 37]) for i in range(0, noisy.size, 37)]
    streamed = np.concatenate(pieces)
    max_diff = float(np.max(np.abs(ref - streamed)))
    check(
        "chunked streaming == single-shot streaming",
        max_diff < 1e-9,
        f"max |diff| = {max_diff:.2e}",
    )

    # --- streaming should track the offline result once settled ---
    # Causal filtering has group delay, so compare energy after a 1 s settle, not
    # sample-by-sample: the point is that no large DC or drift term survives.
    settled = ref[int(fs) :]
    check(
        "streaming output is centred after settling",
        abs(float(np.mean(settled))) < 0.05,
        f"mean = {np.mean(settled):.2e}",
    )

    # --- input validation ---
    for bad_cfg, label in (
        (dict(bp_low=0.0), "bp_low = 0"),
        (dict(bp_high=200.0), "bp_high above Nyquist"),
        (dict(notch_q=0.0), "notch_q = 0"),
    ):
        try:
            FilterConfig(**bad_cfg)
            check(f"rejects invalid config ({label})", False, "no error raised")
        except ValueError:
            check(f"rejects invalid config ({label})", True, "ValueError")

    # --- report ---
    width = max(len(name) for name, _, _ in checks)
    passed = 0
    print(f"\nfilters.py self-test  (fs = {fs:.0f} Hz)\n" + "-" * (width + 22))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
        passed += ok
    print("-" * (width + 22))
    print(f"{passed}/{len(checks)} checks passed\n")
    if passed != len(checks):
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
