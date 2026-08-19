"""Drive the real system end to end, so requirements can be measured rather than claimed.

This is the machinery Phase 5 needs: start the actual ingest server, attach a device that
speaks the real wire protocol and a dashboard that consumes the real vitals stream, inject
a fault, and measure what happens. Nothing here reimplements the system - if the harness
says the leads-off alarm arrived in 1.6 s, that is the shipped alarm engine's answer,
reached through the shipped protocol and the shipped DSP.

The pieces:

* :class:`ServerProcess` - the Node server on an ephemeral port, torn down afterwards.
* :class:`Device`   - a WebSocket client emitting protocol-v2 frames, with the
                      acquisition model in the path so the samples are ADC-derived
                      millivolts rather than idealised ones (D-24).
* :class:`Dashboard` - a WebSocket client recording every vitals frame with the local
                      time it arrived, which is what makes latency measurable.

Everything is timestamped with `time.monotonic()`. Wall clocks can step; a latency
measurement taken across an NTP correction is not a latency measurement.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dsp"))
sys.path.insert(0, str(ROOT / "sim"))

from afe_model import AdcConfig, AfeConfig, ElectrodeConfig, acquire, counts_to_mv  # noqa: E402
from ppg import PpgConfig, synthetic_ppg  # noqa: E402

FRAME_SAMPLES = 32
PPG_FS = 100.0
PROTOCOL_VERSION = 2


class ServerProcess:
    """The ingest server, on its own port, for the duration of one scenario."""

    def __init__(self, port: int = 0, env: dict | None = None) -> None:
        self.port = port
        self.env = env or {}
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "ServerProcess":
        import os
        import socket

        if self.port == 0:
            # Bind, read the port, release. A fixed port makes scenarios collide with
            # each other and with whatever the developer already has running.
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]

        env = {**os.environ, "PORT": str(self.port), "HOST": "127.0.0.1", **self.env}
        # No DATABASE_URL: scenarios measure the monitor, not the history, and a
        # scenario that needs a database running is a scenario nobody runs.
        env.pop("DATABASE_URL", None)
        self.proc = subprocess.Popen(
            ["node", "src/index.js"],
            cwd=ROOT / "server",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._wait_until_listening()
        return self

    def _wait_until_listening(self, timeout_s: float = 15.0) -> None:
        import socket

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{self.proc.stdout.read()}")
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.1)
        raise TimeoutError(f"server did not listen on {self.port} within {timeout_s}s")

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@dataclass
class SentFrame:
    seq: int
    first_sample: int
    sent_at: float


class Device:
    """A device speaking protocol v2, with the acquisition model in the signal path."""

    def __init__(self, url: str, device_id: str = "sim-hw", fs: float = 360.0) -> None:
        self.url = f"{url}/ingest"
        self.device_id = device_id
        self.fs = fs
        self.ws = None
        self.seq = 0
        self.sample_index = 0
        self.sent: list[SentFrame] = []
        self.errors: list[list[str]] = []
        self._reader: asyncio.Task | None = None

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.url, max_size=2**22)
        self._reader = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            async for message in self.ws:
                data = json.loads(message)
                if data.get("type") == "error":
                    self.errors.append(data.get("errors", []))
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    async def send_block(
        self,
        ecg_mv: np.ndarray,
        *,
        leads_off: bool = False,
        ppg: tuple[np.ndarray, np.ndarray] | None = None,
        temp: float | None = 36.8,
    ) -> SentFrame:
        frame = {
            "v": PROTOCOL_VERSION,
            "deviceId": self.device_id,
            "seq": self.seq,
            "tDevice": int(self.sample_index / self.fs * 1000),
            "fs": self.fs,
            "ecg": [round(float(v), 4) for v in ecg_mv],
            "ppg": None
            if ppg is None
            else {
                "fs": PPG_FS,
                "red": [round(float(v), 3) for v in ppg[0]],
                "ir": [round(float(v), 3) for v in ppg[1]],
            },
            "spo2": None,
            "temp": temp,
            "leadsOff": bool(leads_off),
            "battery": 3.94,
        }
        record = SentFrame(self.seq, self.sample_index, time.monotonic())
        await self.ws.send(json.dumps(frame))
        self.sent.append(record)
        self.seq += 1
        self.sample_index += len(ecg_mv)
        return record

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()


class Dashboard:
    """A dashboard client that records every vitals frame and when it arrived."""

    def __init__(self, url: str) -> None:
        self.url = f"{url}/stream"
        self.ws = None
        self.frames: list[tuple[float, dict]] = []
        self._reader: asyncio.Task | None = None

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.url, max_size=2**22)
        self._reader = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            async for message in self.ws:
                data = json.loads(message)
                if data.get("type") == "vitals":
                    self.frames.append((time.monotonic(), data))
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    async def acknowledge(self, device_id: str, code: str, by: str = "harness") -> None:
        await self.ws.send(json.dumps({"type": "acknowledge", "deviceId": device_id, "code": code, "by": by}))

    def alarms_of(self, code: str) -> list[tuple[float, dict]]:
        """Every arrival at which `code` was active, with its arrival time."""
        out = []
        for at, frame in self.frames:
            for alarm in frame.get("alarms", []):
                if alarm["code"] == code:
                    out.append((at, alarm))
        return out

    def first_alarm_at(self, code: str) -> float | None:
        hits = self.alarms_of(code)
        return hits[0][0] if hits else None

    def latest(self) -> dict | None:
        return self.frames[-1][1] if self.frames else None

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()


# --------------------------------------------------------------------------------
# Signal sources
# --------------------------------------------------------------------------------


def acquired_ecg(signal_mv: np.ndarray, fs: float) -> np.ndarray:
    """Push a signal through the acquisition model and back to millivolts.

    Scenarios run on what the device would actually sample - amplified, band-limited by
    real components, quantised by a 12-bit ADC - not on the recording's clean floats.
    A requirement verified on an idealised signal is verified on a system nobody ships.
    """
    result = acquire(signal_mv, fs, afe=AfeConfig(), electrode=ElectrodeConfig(), adc=AdcConfig())
    return counts_to_mv(result.counts, afe=AfeConfig(), adc=AdcConfig())


def ppg_stream(minutes: float, spo2_pct: float, pulse_bpm: float) -> tuple[np.ndarray, np.ndarray]:
    red, ir, _ = synthetic_ppg(
        PpgConfig(duration_s=max(minutes, 0.5) * 60.0, fs=PPG_FS, pulse_bpm=pulse_bpm, spo2_pct=spo2_pct)
    )
    return red, ir


@dataclass
class Timeline:
    """Bookkeeping for a scenario that streams at real time."""

    fs: float
    started: float = field(default_factory=time.monotonic)
    frames_sent: int = 0

    async def pace(self) -> None:
        """Sleep until the next frame is due, against a fixed schedule.

        Sleeping a fixed interval each time accumulates processing cost as drift, and a
        scenario that quietly runs at 0.9x real time measures the wrong debounce.
        """
        self.frames_sent += 1
        target = self.started + self.frames_sent * FRAME_SAMPLES / self.fs
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
