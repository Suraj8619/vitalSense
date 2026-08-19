"""Render the analog front-end's response and the acquisition chain into docs/img/.

These are the figures a schematic review would ask for: what the analog band actually
does, and what a real ECG looks like by the time the firmware reads it. Generated from
the same model that `dsp/validate.py` scores the detector through, so the plots cannot
drift away from the numbers in `docs/verification.md` (the same argument as D-11).

Run: python sim/plot_afe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dsp"))
sys.path.insert(0, str(ROOT / "sim"))

from afe_model import (  # noqa: E402
    AdcConfig,
    AfeConfig,
    ElectrodeConfig,
    acquire,
    afe_response_db,
    counts_to_mv,
    realised_corners,
    synthesise_components,
)
from filters import DEFAULT_FS, FilterConfig, design_notch, response_db  # noqa: E402
from synth import SynthConfig, synthetic_ecg  # noqa: E402

IMG_DIR = ROOT / "docs" / "img"


def plot_bode(cfg: AfeConfig | None = None) -> Path:
    """Magnitude response of the analog chain, and what the digital notch adds to it."""
    cfg = cfg or AfeConfig()
    hp_hz, lp_hz = realised_corners(cfg)
    f = np.logspace(-2, 2.4, 2000)

    analog = afe_response_db(f, cfg)
    nb, na = design_notch(FilterConfig())
    notch = response_db(nb, na, f, DEFAULT_FS)

    fig, ax = plt.subplots(figsize=(9, 5.2))
    ax.semilogx(f, analog, lw=2.0, label="analog front end (2-pole HP + 2-pole LP)")
    ax.semilogx(f, analog + notch, lw=1.4, ls="--", label="analog + firmware 50 Hz notch")

    for freq, note in ((hp_hz, f"HP {hp_hz:.2f} Hz"), (lp_hz, f"LP {lp_hz:.1f} Hz")):
        ax.axvline(freq, color="0.6", lw=0.8, ls=":")
        ax.annotate(note, (freq, 3), fontsize=8, ha="center", color="0.35")

    ax.axvline(50.0, color="#c0392b", lw=0.9, ls=":")
    ax.annotate(
        f"50 Hz: analog gives only {afe_response_db(np.array([50.0]), cfg)[0]:.1f} dB\n"
        "→ the notch is not optional",
        (50.0, -46),
        fontsize=8,
        ha="right",
        color="#c0392b",
    )
    ax.axhspan(-3, 3, color="#2e86c1", alpha=0.07)
    ax.annotate("±3 dB", (0.012, 0), fontsize=8, color="#2e86c1", va="center")

    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("magnitude (dB)")
    ax.set_title("VitalSense analog front end — measured from the synthesised E12 components")
    ax.set_ylim(-70, 8)
    ax.set_xlim(f[0], f[-1])
    ax.grid(True, which="both", alpha=0.25)
    ax.legend(loc="lower center", fontsize=9)

    parts = "   ".join(f"{c.stage}: C = {c.c_farads * 1e9:.0f} nF, R = {c.r_ohms / 1e6:.1f} MΩ" for c in synthesise_components(cfg))
    fig.text(0.5, 0.008, parts, ha="center", fontsize=8, color="0.35")

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    out = IMG_DIR / "afe_bode.png"
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def plot_chain(cfg: AfeConfig | None = None) -> Path:
    """One beat, at four points in the chain: patient, amplifier, ADC, recovered."""
    cfg = cfg or AfeConfig()
    adc = AdcConfig(jitter_us=20)
    electrode = ElectrodeConfig()

    sig, peaks = synthetic_ecg(SynthConfig(duration_s=12.0, hr_bpm=72, fs=DEFAULT_FS))
    res = acquire(sig, DEFAULT_FS, afe=cfg, electrode=electrode, adc=adc)
    back = counts_to_mv(res.counts, afe=cfg, adc=adc)

    # A window around a beat well clear of the record ends.
    centre = int(peaks[len(peaks) // 2])
    lo, hi = centre - int(0.35 * DEFAULT_FS), centre + int(0.55 * DEFAULT_FS)
    t = (np.arange(lo, hi) - centre) / DEFAULT_FS * 1000.0

    fig, axes = plt.subplots(4, 1, figsize=(9, 8.2), sharex=True)

    axes[0].plot(t, sig[lo:hi], lw=1.4, color="#2e86c1")
    axes[0].set_ylabel("mV")
    axes[0].set_title(
        "At the electrodes — the signal the recording holds", fontsize=10, loc="left"
    )

    axes[1].plot(t, sig[lo:hi] + electrode.half_cell_mv, lw=1.4, color="#8e44ad")
    axes[1].set_ylabel("mV")
    axes[1].set_title(
        f"Plus a {electrode.half_cell_mv:.0f} mV electrode half-cell offset — "
        f"{electrode.half_cell_mv / np.ptp(sig):.0f}× the signal, "
        "and the reason the high-pass sits before the gain",
        fontsize=10,
        loc="left",
    )

    axes[2].step(t, res.counts[lo:hi], lw=1.2, where="mid", color="#d35400")
    axes[2].set_ylabel("ADC code")
    axes[2].set_title(
        f"At the ADC — 12 bits, {1e6 * adc.lsb_v / cfg.gain:.2f} µV per LSB, "
        f"{adc.jitter_us:.0f} µs sampling jitter, INL and noise",
        fontsize=10,
        loc="left",
    )

    axes[3].plot(t, sig[lo:hi], lw=2.2, color="#2e86c1", alpha=0.35, label="original")
    axes[3].plot(t, back[lo:hi], lw=1.3, color="#c0392b", label="recovered by the firmware")
    axes[3].set_ylabel("mV")
    axes[3].set_xlabel("time relative to the R peak (ms)")
    axes[3].set_title(
        "Recovered — the gain and offset undo cleanly; the analog band-limiting and its "
        "group delay do not",
        fontsize=10,
        loc="left",
    )
    axes[3].legend(fontsize=8, loc="upper right")

    for ax in axes:
        ax.grid(True, alpha=0.2)

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    out = IMG_DIR / "afe_chain.png"
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def main() -> int:
    for path in (plot_bode(), plot_chain()):
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
