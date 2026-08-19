"""Replay recorded ECG into the server as if it came from the device.

This is what lets the whole system be demonstrated, and regression-tested, with no
hardware attached: real MIT-BIH patient recordings are streamed to the server's
`/ingest` endpoint in exactly the frame format the ESP32 firmware sends, at exactly
the rate the hardware would send them. Everything downstream - filtering, beat
detection, alarms, dashboard - is the production path.

Validating against recorded clinical data before hardware integration is not a
workaround for the hardware being unavailable; it is how the algorithm gets a ground
truth a live sensor cannot provide.

Usage
-----
    python sim/replay.py                          # record 100, real time, loops
    python sim/replay.py --record 203 --speed 10  # 10x faster than real time
    python sim/replay.py --scenario leads-off     # inject a fault mid-stream
    python sim/replay.py --synthetic --hr 140     # synthetic tachycardia, no dataset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dsp"))

from filters import DEFAULT_FS  # noqa: E402
from ppg import PpgConfig, synthetic_ppg  # noqa: E402
from synth import SynthConfig, synthetic_ecg  # noqa: E402

PROTOCOL_VERSION = 2

#: Shortest fault window any scenario injects, in simulated seconds, and the longest
#: alarm debounce it has to outlast in *wall* time (LEADS_OFF at 1.5 s, plus margin for
#: frame and processing latency). See the warning in `check_scenario_speed`.
SCENARIO_WINDOW_S = 10.0
LONGEST_SUSTAIN_S = 10.0

#: PPG sample rate. The MAX30102's default, and one of the rates coefficients are
#: generated for - the server refuses any other (D-14).
PPG_FS = 100.0
DEFAULT_URL = "ws://127.0.0.1:8080/ingest"
#: Samples per frame. 32 at 360 Hz is ~89 ms of signal - small enough to keep latency
#: low, large enough that framing overhead stays negligible.
FRAME_SAMPLES = 32


def load_signal(record: str, minutes: float | None) -> tuple[np.ndarray, float]:
    """Load a MIT-BIH record, or explain how to fetch it."""
    import wfdb  # imported here so --synthetic works without the dataset installed

    base = ROOT / "data" / "mitdb" / record
    if not base.with_suffix(".dat").exists():
        raise SystemExit(
            f"record {record} not found in data/mitdb - run: python dsp/fetch_data.py {record}"
        )
    rec = wfdb.rdrecord(str(base))
    signal = np.asarray(rec.p_signal[:, 0], dtype=np.float64)
    if minutes is not None:
        signal = signal[: int(minutes * 60 * rec.fs)]
    return signal, float(rec.fs)


def make_synthetic(hr: float, minutes: float, noise: float) -> tuple[np.ndarray, float]:
    """Generate a synthetic recording - useful for demonstrating a specific rhythm."""
    sig, _ = synthetic_ecg(
        SynthConfig(
            duration_s=minutes * 60,
            hr_bpm=hr,
            noise_std=noise,
            mains_amp=0.15,
            wander_amp=0.2,
            dc_offset=0.3,
        )
    )
    return sig, DEFAULT_FS


class Scenario:
    """Fault injection, so alarm paths can be demonstrated on demand.

    Each scenario answers a question a demo should be able to answer live: what
    happens when an electrode falls off, when the patient's rate climbs, when the
    signal degrades. Being able to trigger these on a laptop is what makes the alarm
    logic reviewable without a patient.
    """

    def __init__(self, name: str, fs: float) -> None:
        self.name = name
        self.fs = fs

    def apply(self, block: np.ndarray, elapsed_s: float) -> tuple[np.ndarray, bool, float | None]:
        """Return (samples, leads_off, spo2_override) for this block."""
        if self.name == "none":
            return block, False, None

        if self.name == "leads-off":
            # Electrodes detach 15 s in and stay off for 10 s. The AD8232 rails when
            # a lead opens, so the signal goes flat with a DC offset - not to zero.
            if 15.0 <= elapsed_s < 25.0:
                return np.full_like(block, 1.8), True, None
            return block, False, None

        if self.name == "noise":
            # Muscle artefact burst - tests that poor signal is flagged, not guessed at.
            if 20.0 <= elapsed_s < 30.0:
                rng = np.random.default_rng(int(elapsed_s * 100))
                return block + rng.normal(0, 0.6, block.size), False, None
            return block, False, None

        if self.name == "desat":
            # SpO2 falls below the alarm threshold and recovers.
            if 20.0 <= elapsed_s < 45.0:
                return block, False, 88.0
            return block, False, 97.0

        raise ValueError(f"unknown scenario: {self.name}")


async def replay(args: argparse.Namespace) -> int:
    if args.synthetic:
        signal, fs = make_synthetic(args.hr, args.minutes or 5.0, args.noise)
        source = f"synthetic {args.hr:.0f} bpm"
    else:
        signal, fs = load_signal(args.record, args.minutes)
        source = f"MIT-BIH record {args.record}"

    scenario = Scenario(args.scenario, fs)
    frame_period = FRAME_SAMPLES / fs / args.speed
    total_frames = len(signal) // FRAME_SAMPLES

    print(
        f"replaying {source}: {len(signal) / fs / 60:.1f} min, {fs:.0f} Hz, "
        f"{args.speed}x speed, scenario '{args.scenario}' -> {args.url}"
    )

    # Awaiting connect() yields the connection itself, not a context manager, so the
    # connection is closed explicitly in the finally block below. Connecting up front
    # (rather than inside `async with`) is what lets a refused connection produce a
    # useful message instead of a traceback.
    try:
        ws = await websockets.connect(args.url, max_queue=None)
    except OSError as exc:
        raise SystemExit(f"could not connect to {args.url}: {exc}\nIs the server running? (npm start)")

    seq = 0
    # A PPG stream to accompany the ECG. The desat scenario walks the saturation down,
    # so the value is generated per segment rather than once.
    ppg_minutes = max(total_frames * FRAME_SAMPLES / fs / 60.0, 1.0)
    # Lock the simulated pulse to the record's own heart rate. A finger and a chest
    # belong to the same patient, and a demo where the two disagree by 4 bpm invites
    # exactly the wrong question. It also sets up the cross-check that D-37 names as the
    # real answer to PPG rate outliers: two independent measurements of one quantity.
    ppg_hr = args.hr
    if not args.synthetic:
        from pan_tompkins import detect_qrs  # noqa: E402
        from vitals import heart_rate  # noqa: E402

        measured = heart_rate(detect_qrs(signal[: int(60 * fs)]).r_peaks, fs)
        if measured.valid:
            ppg_hr = measured.bpm
            print(f"  PPG pulse locked to the record's heart rate: {ppg_hr:.1f} bpm")
    ppg_red, ppg_ir = make_ppg(ppg_minutes, args.spo2, ppg_hr)
    ppg_per_frame = max(int(round(FRAME_SAMPLES / fs * PPG_FS)), 1)

    started = time.monotonic()

    try:
        # Drain server messages (rejections, errors) without blocking the send loop.
        async def drain() -> None:
            try:
                async for message in ws:
                    data = json.loads(message)
                    if data.get("type") == "error":
                        print(f"  server rejected frame: {data.get('errors')}", file=sys.stderr)
            except websockets.ConnectionClosed:
                pass

        reader = asyncio.create_task(drain())

        try:
            while True:
                for i in range(total_frames):
                    block = signal[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
                    elapsed = time.monotonic() - started
                    block, leads_off, spo2_override = scenario.apply(block, elapsed * args.speed)

                    # Slice the accompanying PPG. With the electrodes off, the finger
                    # clip is treated as off too: no light, so no block at all rather
                    # than a stale one.
                    ppg_block = None
                    if not leads_off:
                        lo = (seq * ppg_per_frame) % max(ppg_red.size - ppg_per_frame, 1)
                        red_slice = ppg_red[lo : lo + ppg_per_frame]
                        ir_slice = ppg_ir[lo : lo + ppg_per_frame]
                        if spo2_override is not None:
                            # The desat scenario asks for a specific saturation; scale
                            # the red channel's pulsatile depth to produce it, so the
                            # server derives the intended number from the light rather
                            # than being told it.
                            target_r = (110.0 - spo2_override) / 25.0
                            base_r = (110.0 - args.spo2) / 25.0
                            if base_r > 0:
                                dc = float(np.mean(red_slice))
                                red_slice = dc + (red_slice - dc) * (target_r / base_r)
                        if red_slice.size == ppg_per_frame:
                            ppg_block = {
                                "fs": PPG_FS,
                                "red": [round(float(v), 3) for v in red_slice],
                                "ir": [round(float(v), 3) for v in ir_slice],
                            }

                    frame = {
                        "v": PROTOCOL_VERSION,
                        "deviceId": args.device_id,
                        "seq": seq,
                        "tDevice": int(seq * FRAME_SAMPLES / fs * 1000),
                        "fs": fs,
                        "ecg": [round(float(v), 4) for v in block],
                        # Protocol v2: the device sends the *light*, not a saturation.
                        # Computing it on the server is what D-03's thin-edge split
                        # asks for, and it is the only way the calculation can be
                        # regression-tested against recordings. `spo2` is left null:
                        # a real device reports null when a sensor has no valid
                        # reading rather than inventing a plausible number (D-16).
                        "ppg": ppg_block,
                        "spo2": None,
                        "temp": 36.8 if not leads_off else None,
                        "leadsOff": bool(leads_off),
                        "battery": 3.94,
                    }

                    await ws.send(json.dumps(frame))
                    seq += 1

                    if seq % int(fs / FRAME_SAMPLES * 10) == 0:
                        print(f"  {seq} frames sent ({seq * FRAME_SAMPLES / fs / 60:.1f} min of signal)")

                    # Pace against a fixed schedule rather than sleeping a fixed
                    # amount: sleeping frame_period each time accumulates the
                    # processing time as drift, and the stream slowly falls behind.
                    target = started + seq * frame_period
                    delay = target - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)

                if not args.loop:
                    break
                print("  end of recording, looping")
        except websockets.ConnectionClosed:
            print("server closed the connection", file=sys.stderr)
            return 1
        except asyncio.CancelledError:
            raise
        finally:
            reader.cancel()
    finally:
        await ws.close()

    print(f"done: {seq} frames sent")
    return 0


def check_scenario_speed(scenario: str, speed: float) -> None:
    """Warn when replay speed compresses a fault below the alarm that should catch it.

    Scenario windows are measured in *simulated* time; alarm sustain times are measured
    in *wall* time, because that is what a debounce protecting a clinician has to be.
    Replaying at 15x turns a ten-second detached electrode into 0.67 s of real time, and
    a 1.5 s debounce then correctly refuses to fire - so the demo silently shows no alarm
    and looks like a bug in the alarm engine rather than in the clock the demo chose.

    Found by running exactly that and wondering where LEADS_OFF had gone (D-46).
    """
    if scenario == "none" or speed <= 1.0:
        return
    wall_window = SCENARIO_WINDOW_S / speed
    if wall_window < 2.0:
        print(
            f"  WARNING: at {speed:g}x, the '{scenario}' fault lasts {wall_window:.2f} s of real time.\n"
            f"           Alarm debounces are wall-clock (leads-off needs 1.5 s, rate alarms 10 s),\n"
            f"           so alarms will NOT fire. Use --speed 1 to demonstrate alarm paths.",
            file=sys.stderr,
        )
    elif wall_window < LONGEST_SUSTAIN_S:
        print(
            f"  note: at {speed:g}x the '{scenario}' fault lasts {wall_window:.2f} s of real time;\n"
            f"        rate alarms need 10 s sustained and will not fire. Leads-off still will.",
            file=sys.stderr,
        )


def make_ppg(minutes: float, spo2_pct: float, hr: float) -> tuple[np.ndarray, np.ndarray]:
    """A red/IR pair to accompany the replayed ECG.

    The dataset carries no photoplethysmogram, so this channel is synthetic and the
    docs say so. It exists to exercise the real server-side saturation path - the same
    `estimateSpo2` a device would drive - rather than to claim a validated SpO2.
    """
    cfg = PpgConfig(duration_s=max(minutes, 1.0) * 60.0, fs=PPG_FS, pulse_bpm=hr, spo2_pct=spo2_pct)
    red, ir, _ = synthetic_ppg(cfg)
    return red, ir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="server ingest endpoint")
    parser.add_argument("--record", default="100", help="MIT-BIH record to replay")
    parser.add_argument("--device-id", default="sim-01", help="device identity to report")
    parser.add_argument("--speed", type=float, default=1.0, help="playback rate multiplier")
    parser.add_argument("--minutes", type=float, default=None, help="limit replay length")
    parser.add_argument("--loop", action="store_true", help="restart at the end of the recording")
    parser.add_argument(
        "--scenario",
        default="none",
        choices=["none", "leads-off", "noise", "desat"],
        help="inject a fault to demonstrate an alarm path",
    )
    parser.add_argument("--synthetic", action="store_true", help="generate a signal instead of using the dataset")
    parser.add_argument("--hr", type=float, default=72.0, help="synthetic heart rate, bpm")
    parser.add_argument("--noise", type=float, default=0.03, help="synthetic noise level, mV RMS")
    parser.add_argument("--spo2", type=float, default=97.0, help="baseline SpO2 to encode in the PPG, %%")
    args = parser.parse_args(argv)

    if args.speed <= 0:
        parser.error("--speed must be positive")

    check_scenario_speed(args.scenario, args.speed)

    try:
        return asyncio.run(replay(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
