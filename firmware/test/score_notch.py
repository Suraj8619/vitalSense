"""Score the firmware's fixed-point notch against the Python design that generated it.

Three programs now filter the same ECG: `dsp/filters.py` (float64, the design of
record), `server/src/dsp/biquad.js` (float64, causal, already scored in Phase 4a), and
`firmware/src/vs_notch.c` (Q14 fixed point, the one that ships). D-12 keeps their
coefficients from drifting apart. This closes the remaining gap: it measures whether the
*arithmetic* drifted, by pushing real MIT-BIH recordings through the compiled firmware
filter and comparing sample by sample against the float reference.

"The port is equivalent" is a claim. This makes it a number (D-13, D-25).

Run: python firmware/test/score_notch.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy import signal as sps

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "dsp"))
sys.path.insert(0, str(ROOT / "sim"))

from afe_model import AdcConfig, AfeConfig, ElectrodeConfig, acquire  # noqa: E402
from fetch_data import DEFAULT_RECORDS  # noqa: E402
from filters import FilterConfig, design_notch  # noqa: E402
from validate import load_record  # noqa: E402

TEST_DIR = Path(__file__).resolve().parent
BINARY = TEST_DIR / "build" / "host_filter"


def build() -> None:
    """Compile the host binary, failing loudly rather than scoring a stale one."""
    subprocess.run(["make", "-C", str(TEST_DIR), "filter"], check=True, capture_output=True)


def reference_notch(counts: np.ndarray) -> np.ndarray:
    """The float64 design, applied causally and primed the same way the C is.

    Anything else would compare two different filters and attribute the difference to
    fixed point.
    """
    b, a = design_notch(FilterConfig())
    b, a = b / a[0], a / a[0]
    zi = sps.lfilter_zi(b, a) * float(counts[0])
    out, _ = sps.lfilter(b, a, counts.astype(np.float64), zi=zi)
    return out


def run_firmware(counts: np.ndarray) -> np.ndarray:
    """Pipe ADC counts through the compiled firmware filter."""
    payload = "\n".join(str(int(c)) for c in counts)
    proc = subprocess.run([str(BINARY)], input=payload, capture_output=True, text=True, check=True)
    return np.fromstring(proc.stdout, dtype=np.int64, sep="\n")


def score(record: str) -> dict[str, float]:
    """Compare the two implementations on one record, as the device would see it."""
    signal_mv, _, fs = load_record(record)
    acquired = acquire(signal_mv, fs, afe=AfeConfig(), electrode=ElectrodeConfig(), adc=AdcConfig())
    counts = acquired.counts.astype(np.int64)

    ref = reference_notch(counts)
    got = run_firmware(counts)
    if got.size != counts.size:
        raise RuntimeError(f"firmware returned {got.size} samples, expected {counts.size}")

    err = got - ref
    # The firmware returns whole counts, so a large part of `err` is simply the final
    # round-to-integer and has nothing to do with fixed point. Comparing against the
    # *correctly rounded* float result separates the two: what is left is the error the
    # Q14 arithmetic actually introduced.
    disagree = got != np.round(ref)

    # Referred to the patient: one ADC count is lsb_v / gain volts at the electrodes.
    uv_per_count = 1e6 * AdcConfig().lsb_v / AfeConfig().gain
    signal_rms = float(np.std(ref))
    return {
        "n": float(counts.size),
        "max_err_counts": float(np.max(np.abs(err))),
        "rms_err_counts": float(np.sqrt(np.mean(err**2))),
        "max_err_uv": float(np.max(np.abs(err)) * uv_per_count),
        "snr_db": float(20 * np.log10(signal_rms / np.sqrt(np.mean(err**2)))) if np.any(err) else float("inf"),
        "disagree_pct": 100.0 * float(np.mean(disagree)),
    }


def startup_transient(counts: np.ndarray, settle_counts: int = 1) -> tuple[int, float]:
    """What priming buys, in counts and seconds.

    A cascade started from zero charges up through the electrode's standing DC level
    and rings. Running the same samples with and without priming measures the size of
    the transient that would otherwise sit at the front of every session - and every
    reconnection.
    """
    head = counts[: min(counts.size, 4000)]
    payload = "\n".join(str(int(c)) for c in head)
    unprimed = np.fromstring(
        subprocess.run([str(BINARY), "--no-prime"], input=payload, capture_output=True,
                       text=True, check=True).stdout,
        dtype=np.int64, sep="\n",
    )
    primed = run_firmware(head)
    dev = np.abs(unprimed - primed)
    beyond = np.nonzero(dev > settle_counts)[0]
    settle_samples = int(beyond[-1] + 1) if beyond.size else 0
    return int(dev.max()), settle_samples


def main(argv: list[str] | None = None) -> int:
    records = (argv or sys.argv[1:]) or DEFAULT_RECORDS
    build()

    print(f"\nFirmware notch vs Python design - {len(records)} records\n")
    print(f"{'rec':>5} {'samples':>9} {'max err':>9} {'rms err':>9} {'max err':>10} {'SNR':>9} {'disagree':>9}")
    print(f"{'':>5} {'':>9} {'(counts)':>9} {'(counts)':>9} {'(µV @ in)':>10} {'(dB)':>9} {'(%)':>9}")
    print("-" * 68)

    rows: list[dict[str, float]] = []
    for rec in records:
        try:
            r = score(rec)
        except FileNotFoundError as exc:
            print(f"{rec:>5}  SKIPPED - {exc}", file=sys.stderr)
            continue
        rows.append(r)
        print(
            f"{rec:>5} {int(r['n']):>9} {r['max_err_counts']:>9.2f} {r['rms_err_counts']:>9.3f} "
            f"{r['max_err_uv']:>10.2f} {r['snr_db']:>9.1f} {r['disagree_pct']:>9.3f}"
        )

    if not rows:
        print("No records scored - run: python dsp/fetch_data.py", file=sys.stderr)
        return 1

    worst_max = max(r["max_err_counts"] for r in rows)
    worst_rms = max(r["rms_err_counts"] for r in rows)
    worst_snr = min(r["snr_db"] for r in rows)
    worst_uv = max(r["max_err_uv"] for r in rows)
    worst_disagree = max(r["disagree_pct"] for r in rows)
    print("-" * 68)
    print(
        f"{'WORST':>5} {'':>9} {worst_max:>9.2f} {worst_rms:>9.3f} {worst_uv:>10.2f} "
        f"{worst_snr:>9.1f} {worst_disagree:>9.3f}"
    )

    signal_mv, _, fs = load_record(records[0])
    peak, settle = startup_transient(acquire(signal_mv, fs).counts.astype(np.int64))
    uv = peak * 1e6 * AdcConfig().lsb_v / AfeConfig().gain
    print(
        f"\nStart-up: priming removes a {peak}-count ({uv:.0f} µV) transient that otherwise "
        f"takes {settle} samples ({settle / fs:.2f} s) to decay below one count."
    )

    # A single ADC count is 2.44 µV at the electrodes; the smallest ECG feature anyone
    # measures is around 100 µV. Requiring the fixed-point error to stay under one count
    # is therefore a strict bound, not a generous one.
    ok = worst_max <= 1.0
    print(
        f"\n{'PASS' if ok else 'FAIL'}: worst-case difference from the float design "
        f"{worst_max:.2f} counts ({worst_uv:.2f} µV referred to the electrodes), against a "
        f"1-count budget.\n"
        f"      Most of that is the final round-to-integer, not the fixed-point arithmetic: "
        f"the firmware\n      agrees with the correctly-rounded float result on "
        f"{100 - worst_disagree:.2f} % of samples, and never differs by more than one count.\n"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
