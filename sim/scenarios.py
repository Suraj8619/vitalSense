"""Fault-injection scenarios: one per requirement, each producing a measurement.

Every scenario here starts the real ingest server, attaches a device speaking the real
wire protocol with the acquisition model in the signal path, injects one fault, and
measures one number against one requirement. Nothing is stubbed. When a scenario reports
that leads-off alarmed in 1.6 s, that is the shipped alarm engine answering through the
shipped protocol.

The results feed `docs/verification.md` and the traceability matrix, so the documentation
cannot drift from the behaviour (D-11).

Run: python sim/run_scenarios.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dsp"))
sys.path.insert(0, str(ROOT / "sim"))

from harness import (  # noqa: E402
    FRAME_SAMPLES,
    PPG_FS,
    Dashboard,
    Device,
    ServerProcess,
    Timeline,
    acquired_ecg,
    ppg_stream,
)
from synth import SynthConfig, synthetic_ecg  # noqa: E402

FS = 360.0


@dataclass
class ScenarioResult:
    """What one scenario measured, and whether the requirement was met."""

    name: str
    requirement: str
    question: str
    measured: str
    passed: bool
    detail: str = ""
    numbers: dict = field(default_factory=dict)


def _blocks(signal: np.ndarray, n: int = FRAME_SAMPLES):
    for i in range(0, len(signal) - n + 1, n):
        yield signal[i : i + n]


async def _ppg_slice(red, ir, index, count):
    lo = (index * count) % max(len(red) - count, 1)
    return red[lo : lo + count], ir[lo : lo + count]


# --------------------------------------------------------------------------------


async def scenario_latency() -> ScenarioResult:
    """SR-02: how long from a sample leaving the device to reaching a dashboard."""
    ecg, _ = synthetic_ecg(SynthConfig(duration_s=25, hr_bpm=72, fs=FS))
    ecg = acquired_ecg(ecg, FS)

    with ServerProcess() as server:
        device, dash = Device(server.url), Dashboard(server.url)
        await dash.connect()
        await device.connect()
        clock = Timeline(FS)

        for block in _blocks(ecg):
            await device.send_block(block)
            await clock.pace()
        await asyncio.sleep(0.4)

        # Match each vitals frame to the device frame that produced it, by absolute
        # sample index. The dashboard frame carrying sample N is the response to the
        # device frame that contained sample N.
        sent_by_index = {f.first_sample: f.sent_at for f in device.sent}
        latencies = [
            (arrived - sent_by_index[frame["sampleIndex"]]) * 1000.0
            for arrived, frame in dash.frames
            if frame.get("sampleIndex") in sent_by_index
        ]
        await device.close()
        await dash.close()

    if len(latencies) < 50:
        return ScenarioResult("End-to-end latency", "SR-02", "How long does a sample take to reach the display?",
                              "insufficient data", False, f"only {len(latencies)} matched frames")

    p50 = statistics.median(latencies)
    p95 = float(np.percentile(latencies, 95))
    worst = max(latencies)
    return ScenarioResult(
        "End-to-end latency", "SR-02",
        "How long does a sample take to reach the display?",
        f"median {p50:.1f} ms, p95 {p95:.1f} ms, worst {worst:.1f} ms",
        worst < 500.0,
        "Loopback transport: this covers framing, validation, streaming DSP, alarm "
        "evaluation and fan-out, but **not** WiFi. A real 2.4 GHz link on a busy ward "
        "adds roughly 10-50 ms typical with occasional retransmission spikes into the "
        "hundreds, so the deployed figure is this plus that term.",
        {"p50_ms": p50, "p95_ms": p95, "worst_ms": worst, "n": len(latencies)},
    )


async def scenario_leads_off() -> ScenarioResult:
    """SR-03: how quickly a detached electrode is alarmed."""
    ecg, _ = synthetic_ecg(SynthConfig(duration_s=30, hr_bpm=72, fs=FS))
    ecg = acquired_ecg(ecg, FS)

    with ServerProcess() as server:
        device, dash = Device(server.url), Dashboard(server.url)
        await dash.connect()
        await device.connect()
        clock = Timeline(FS)

        detached_at = None
        for i, block in enumerate(_blocks(ecg)):
            elapsed = i * FRAME_SAMPLES / FS
            off = elapsed >= 8.0
            if off and detached_at is None:
                detached_at = asyncio.get_running_loop().time()
                import time as _t

                detached_at = _t.monotonic()
            # A detached lead rails the amplifier; it does not go to zero.
            await device.send_block(np.full_like(block, 1.8) if off else block, leads_off=off)
            await clock.pace()
            if off and dash.first_alarm_at("LEADS_OFF"):
                break
        await asyncio.sleep(0.2)

        alarmed_at = dash.first_alarm_at("LEADS_OFF")
        await device.close()
        await dash.close()

    if alarmed_at is None or detached_at is None:
        return ScenarioResult("Leads-off alarm", "SR-03", "How fast is a detached electrode alarmed?",
                              "no alarm", False, "LEADS_OFF never fired")
    seconds = alarmed_at - detached_at
    return ScenarioResult(
        "Leads-off alarm", "SR-03", "How fast is a detached electrode alarmed?",
        f"{seconds:.2f} s", seconds < 2.0,
        "Budget: 1.5 s device+server debounce (D-18, D-28) leaves 500 ms for transport "
        "and processing.",
        {"seconds": seconds},
    )


async def scenario_outage() -> ScenarioResult:
    """SR-05: a 30 s network outage must not lose samples."""
    ecg, _ = synthetic_ecg(SynthConfig(duration_s=70, hr_bpm=72, fs=FS))
    ecg = acquired_ecg(ecg, FS)
    blocks = list(_blocks(ecg))
    # 30 s of outage at real time is 30 s of test. Frames are buffered by the device and
    # replayed on reconnect, exactly as the firmware's ring buffer does (SR-05, D-27).
    outage_from, outage_to = 10.0, 40.0

    with ServerProcess() as server:
        device, dash = Device(server.url, device_id="outage-1"), Dashboard(server.url)
        await dash.connect()
        await device.connect()
        clock = Timeline(FS)

        buffered: list[np.ndarray] = []
        disconnected = False
        for i, block in enumerate(blocks):
            elapsed = i * FRAME_SAMPLES / FS
            if outage_from <= elapsed < outage_to:
                if not disconnected:
                    await device.close()
                    disconnected = True
                buffered.append(block)
            else:
                if disconnected:
                    await device.connect()
                    disconnected = False
                    for held in buffered:
                        await device.send_block(held)
                    buffered.clear()
                await device.send_block(block)
            await clock.pace()
        await asyncio.sleep(0.5)

        stats = dash.latest()["stats"] if dash.latest() else {}
        await device.close()
        await dash.close()

    expected = len(blocks)
    received = stats.get("framesReceived", 0)
    dropped = stats.get("framesDropped", 0)
    return ScenarioResult(
        "30 s network outage", "SR-05", "Does a network outage lose patient data?",
        f"{received}/{expected} frames delivered, {dropped} gaps in seq",
        received >= expected and dropped == 0,
        "The device buffers during the outage and replays on reconnect; `seq` gaps are "
        "what makes any loss detectable rather than invisible (D-15).",
        {"expected": expected, "received": received, "dropped": dropped},
    )


async def scenario_spo2() -> ScenarioResult:
    """SR-06: a known saturation must be recovered from raw red/IR."""
    truth = 94.0
    ecg, _ = synthetic_ecg(SynthConfig(duration_s=25, hr_bpm=72, fs=FS))
    ecg = acquired_ecg(ecg, FS)
    red, ir = ppg_stream(0.6, truth, 72.0)
    per_frame = int(round(FRAME_SAMPLES / FS * PPG_FS))

    with ServerProcess() as server:
        device, dash = Device(server.url), Dashboard(server.url)
        await dash.connect()
        await device.connect()
        clock = Timeline(FS)
        for i, block in enumerate(_blocks(ecg)):
            await device.send_block(block, ppg=await _ppg_slice(red, ir, i, per_frame))
            await clock.pace()
        await asyncio.sleep(0.3)
        reported = [f["spo2"] for _, f in dash.frames if f.get("spo2") is not None]
        sources = {f.get("spo2Source") for _, f in dash.frames if f.get("spo2") is not None}
        await device.close()
        await dash.close()

    if not reported:
        return ScenarioResult("SpO2 from raw light", "SR-06", "Is saturation recovered from red/IR?",
                              "no reading", False, "no SpO2 was reported")
    measured = statistics.median(reported)
    error = abs(measured - truth)
    return ScenarioResult(
        "SpO2 from raw light", "SR-06", "Is saturation recovered from red/IR?",
        f"{measured:.1f} % against a true {truth:.0f} % (error {error:.1f})",
        error <= 3.0 and sources == {"server"},
        "The device sends light; the server derives saturation (protocol v2). The "
        "measurement chain is verified here; the R-to-percentage calibration curve is "
        "an assumption (D-34).",
        {"truth": truth, "measured": measured, "error": error},
    )


async def scenario_poor_signal() -> ScenarioResult:
    """SR-07: a signal too poor to trust must be flagged, not reported as a number."""
    clean, _ = synthetic_ecg(SynthConfig(duration_s=12, hr_bpm=72, fs=FS, seed=3))
    noisy, _ = synthetic_ecg(SynthConfig(duration_s=18, hr_bpm=72, fs=FS, noise_std=0.9, seed=3))
    signal = acquired_ecg(np.concatenate([clean, noisy]), FS)

    with ServerProcess() as server:
        device, dash = Device(server.url), Dashboard(server.url)
        await dash.connect()
        await device.connect()
        clock = Timeline(FS)
        for block in _blocks(signal):
            await device.send_block(block)
            await clock.pace()
        await asyncio.sleep(0.3)
        frames = [f for _, f in dash.frames]
        await device.close()
        await dash.close()

    # Only the noisy stretch matters, once the rate window has filled with noise.
    late = frames[int(len(frames) * 0.7) :]
    reported = [f["hr"] for f in late if f["hr"] is not None]
    wrong = [h for h in reported if abs(h - 72.0) > 10.0]
    withheld = sum(1 for f in late if f["hr"] is None)
    return ScenarioResult(
        "Unusable signal", "SR-07", "Is a bad signal flagged rather than guessed at?",
        f"{withheld}/{len(late)} frames withheld a rate; {len(wrong)} wrong rates reported",
        len(wrong) == 0,
        "The test is not whether the detector copes with noise - it is whether the "
        "monitor declines to answer instead of answering wrongly.",
        {"withheld": withheld, "of": len(late), "wrong": len(wrong)},
    )


async def scenario_reconnect_settling() -> ScenarioResult:
    """D-22: reconnecting an electrode must not fabricate a heart rate."""
    ecg, _ = synthetic_ecg(SynthConfig(duration_s=40, hr_bpm=72, fs=FS))
    ecg = acquired_ecg(ecg, FS)

    with ServerProcess() as server:
        device, dash = Device(server.url), Dashboard(server.url)
        await dash.connect()
        await device.connect()
        clock = Timeline(FS)
        import time as _t

        reattached_at = None
        for i, block in enumerate(_blocks(ecg)):
            elapsed = i * FRAME_SAMPLES / FS
            off = 8.0 <= elapsed < 18.0
            if not off and elapsed >= 18.0 and reattached_at is None:
                reattached_at = _t.monotonic()
            await device.send_block(np.full_like(block, 1.8) if off else block, leads_off=off)
            await clock.pace()
        await asyncio.sleep(0.3)
        after = [(at, f) for at, f in dash.frames if reattached_at and at >= reattached_at]
        await device.close()
        await dash.close()

    rates = [f["hr"] for _, f in after if f["hr"] is not None]
    fabricated = [r for r in rates if r > 110.0]
    settled = [r for _, f in after[-30:] if (r := f["hr"]) is not None]
    return ScenarioResult(
        "Electrode reconnection", "D-22", "Does reattaching an electrode invent a heart rate?",
        f"{len(fabricated)} fabricated rates above 110 bpm; settled at "
        f"{statistics.median(settled):.0f} bpm" if settled else "no rate after settling",
        len(fabricated) == 0 and bool(settled) and abs(statistics.median(settled) - 72) < 8,
        "The failure this guards: a burst of detections from the reconnection transient "
        "was once displayed as 190 bpm tachycardia with signal quality GOOD.",
        {"fabricated": len(fabricated), "settled_bpm": statistics.median(settled) if settled else None},
    )


async def scenario_alarm_acknowledge() -> ScenarioResult:
    """D-44: acknowledging silences an alarm without dismissing it."""
    ecg, _ = synthetic_ecg(SynthConfig(duration_s=30, hr_bpm=72, fs=FS))
    ecg = acquired_ecg(ecg, FS)

    with ServerProcess() as server:
        device, dash = Device(server.url, device_id="ack-1"), Dashboard(server.url)
        await dash.connect()
        await device.connect()
        clock = Timeline(FS)

        acked = False
        for i, block in enumerate(_blocks(ecg)):
            elapsed = i * FRAME_SAMPLES / FS
            off = elapsed >= 5.0
            await device.send_block(np.full_like(block, 1.8) if off else block, leads_off=off)
            await clock.pace()
            if not acked and dash.first_alarm_at("LEADS_OFF") and elapsed > 9.0:
                await dash.acknowledge("ack-1", "LEADS_OFF")
                acked = True
        await asyncio.sleep(0.3)

        states = [a for _, a in dash.alarms_of("LEADS_OFF")]
        await device.close()
        await dash.close()

    silenced = [a for a in states if a.get("acknowledged")]
    still_present = bool(states) and states[-1]["code"] == "LEADS_OFF"
    return ScenarioResult(
        "Alarm acknowledgement", "D-44", "Does acknowledging silence an alarm without hiding it?",
        f"{len(silenced)} frames silenced; alarm still listed: {still_present}",
        bool(silenced) and still_present,
        "Acknowledging is a silence, not a dismissal: the alarm stays on the display and "
        "sounds again when the silence expires.",
        {"silenced_frames": len(silenced), "still_listed": still_present},
    )


async def scenario_device_silent() -> ScenarioResult:
    """A device that stops sending must be reported, not forgotten."""
    ecg, _ = synthetic_ecg(SynthConfig(duration_s=8, hr_bpm=72, fs=FS))
    ecg = acquired_ecg(ecg, FS)

    with ServerProcess() as server:
        device, dash = Device(server.url), Dashboard(server.url)
        await dash.connect()
        await device.connect()
        clock = Timeline(FS)
        for block in _blocks(ecg):
            await device.send_block(block)
            await clock.pace()
        import time as _t

        stopped = _t.monotonic()
        await device.close()
        await asyncio.sleep(9.0)
        alarmed = dash.first_alarm_at("DEVICE_SILENT")
        last = dash.latest()
        await dash.close()

    if alarmed is None:
        return ScenarioResult("Silent device", "UN-3", "Is a device that stops sending noticed?",
                              "no alarm", False, "DEVICE_SILENT never fired")
    seconds = alarmed - stopped
    return ScenarioResult(
        "Silent device", "UN-3", "Is a device that stops sending noticed?",
        f"alarmed after {seconds:.1f} s; rate withdrawn: {last['hr'] is None}",
        seconds < 8.0 and last is not None and last["hr"] is None,
        "A monitor that keeps showing the last number it received is claiming something "
        "about now that it cannot support (D-23).",
        {"seconds": seconds},
    )


ALL_SCENARIOS = (
    scenario_latency,
    scenario_leads_off,
    scenario_outage,
    scenario_spo2,
    scenario_poor_signal,
    scenario_reconnect_settling,
    scenario_alarm_acknowledge,
    scenario_device_silent,
)
