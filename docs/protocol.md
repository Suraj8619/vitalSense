# Wire protocol

The contract between the acquisition device and the server. The ESP32 firmware and
`sim/replay.py` both speak it, which is what lets the whole system run — and be
tested — with no hardware attached.

## Transport

WebSocket, one connection per device, device connects outward to the server.

- **Device → server:** `/ingest` — sample frames
- **Server → dashboard:** `/stream` — processed vitals and waveform for display

The device dials out rather than listening, so it works behind a hospital NAT
without inbound firewall rules — and it is how the ESP32 will behave on a campus
network. Reconnection is the device's responsibility (SR-05).

## Device → server: sample frame

One JSON message per block of samples. A block, not a sample, because one WebSocket
message per sample at 360 Hz would spend more effort on framing than on data.

```json
{
  "v": 2,
  "deviceId": "esp32-01",
  "seq": 1043,
  "tDevice": 289733,
  "fs": 360,
  "ecg": [-0.118, -0.121, -0.117, ...],
  "ppg": {
    "fs": 100,
    "red": [79812.4, 79790.1, ...],
    "ir":  [99486.7, 99451.2, ...]
  },
  "spo2": null,
  "temp": 36.8,
  "leadsOff": false,
  "battery": 3.94
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | integer | yes | Protocol version. The server rejects versions it does not know rather than guessing. |
| `deviceId` | string | yes | Stable device identity; scopes sessions and alarms. |
| `seq` | integer | yes | Monotonic frame counter from boot. **Gaps in `seq` are how lost data is detected** — without it, a dropped frame is invisible and the waveform silently lies. |
| `tDevice` | integer | yes | Milliseconds since device boot at the **first sample** of this frame. Device clocks are not wall clocks; the server timestamps arrival separately. |
| `fs` | number | yes | Sampling rate of `ecg`. Sent explicitly so a mismatch is caught rather than assumed. |
| `ecg` | number[] | yes | ECG samples in mV, oldest first. Typically 32 samples ≈ 89 ms at 360 Hz. |
| `spo2` | number \| null | no | Blood oxygen saturation, %. `null` when the sensor has no valid reading — never a stale value. |
| `temp` | number \| null | no | Body temperature, °C. |
| `leadsOff` | boolean | yes | Electrode contact lost, from the AD8232 LO± pins. Drives SR-03. |
| `battery` | number | no | Battery voltage, V. |

### Why these choices

- **`spo2` and `temp` are nullable, `ecg` and `leadsOff` are not.** A monitor may
  legitimately have no SpO₂ reading; it must always know whether its electrodes are
  attached. Nullability encodes which readings are allowed to be absent.
- **`null` rather than a stale reading.** Repeating the last good value would make a
  disconnected sensor look healthy — the single most dangerous failure mode in a
  monitor, and the reason SR-07 exists.
- **Timestamps are device-relative.** The ESP32 has no real-time clock. The server
  maps `tDevice` onto wall time at session start, so a device reboot cannot rewrite
  history.

## Server → dashboard: vitals frame

```json
{
  "v": 1,
  "type": "vitals",
  "deviceId": "esp32-01",
  "tServer": 1755530400123,
  "sampleIndex": 371680,
  "ecg": [-0.02, -0.01, ...],
  "beats": [371654, 371798],
  "hr": 72.4,
  "hrClass": "normal",
  "spo2": 97.4,
  "temp": 36.8,
  "signalQuality": "good",
  "alarms": [
    { "code": "LEADS_OFF", "severity": "high", "since": 1755530391000, "message": "ECG electrodes detached" }
  ],
  "stats": { "framesReceived": 1043, "framesDropped": 0, "uptimeS": 289.7 }
}
```

| Field | Meaning |
|---|---|
| `ecg` | Filtered waveform for display (0.5–40 Hz path, not the 5–15 Hz detection path). |
| `sampleIndex` | Absolute index of the **first** sample in this frame's `ecg`, counted from session start. |
| `beats` | **Absolute** sample indices of detected R peaks — not offsets into this frame. Detection lags the R peak (the trailing 150 ms integration window delays the decision by ~75 ms, and R-peak snapping then searches backwards), so a beat is routinely confirmed one or two frames *after* the waveform carrying it was sent. Frame-relative offsets would be negative for most beats; absolute indices let the dashboard mark a beat on waveform it already holds. |
| `hr` / `hrClass` | Heart rate and its classification from `dsp/vitals.py`'s thresholds. `null` when unknown. |
| `signalQuality` | `good` \| `poor` \| `unusable`. When `unusable`, `hr` is `null` — the system says "I don't know" instead of guessing (SR-07). |
| `alarms` | Currently active alarms, highest severity first. Empty array means no alarms — the field is always present, so a dashboard bug cannot make alarms disappear by omission. |
| `stats` | Frame accounting, including `framesDropped` derived from `seq` gaps. |

## Alarm codes

| Code | Severity | Trigger |
|---|---|---|
| `LEADS_OFF` | high | `leadsOff` true for ≥ 1.5 s. The debounce sits inside SR-03's 2 s end-to-end budget, leaving margin for frame and processing latency — a 2 s debounce would make the 2 s requirement unmeetable by construction. |
| `ASYSTOLE` | high | No beat detected for ≥ 4 s while leads are on |
| `TACHYCARDIA` | medium | HR > 100 bpm sustained ≥ 10 s |
| `BRADYCARDIA` | medium | HR < 60 bpm sustained ≥ 10 s |
| `SPO2_LOW` | medium | SpO₂ < 92 % sustained ≥ 10 s |
| `SIGNAL_POOR` | low | Signal quality `unusable` for ≥ 5 s |
| `DEVICE_SILENT` | high | No frame received for ≥ 5 s |

Rate alarms require a sustained condition rather than firing on a single reading:
a monitor that alarms on one noisy beat trains its users to ignore it, which is a
well-documented patient-safety problem (alarm fatigue), not just an annoyance.

## Versioning

`v` is checked on every frame. An unknown version is rejected with an error message
and the connection closed, rather than being parsed on a best-effort basis — a
partially understood medical data frame is worse than a refused one.
