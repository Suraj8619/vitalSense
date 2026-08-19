# VitalSense

A multi-parameter patient vitals monitor: ECG, SpO₂ and temperature streamed over
WebSocket, processed with a DSP pipeline validated against cardiologist-annotated
clinical data, and displayed on a live dashboard with clinical alarming.

> **Status:** in development, and **currently runs entirely without hardware** — real
> MIT-BIH recordings are replayed into the server in the same frame format the ESP32
> firmware will emit. See [PLAN.md](PLAN.md) for the phased build plan,
> [docs/progress.md](docs/progress.md) for what is finished and what it measures,
> [BUILD_LOG.md](BUILD_LOG.md) for the engineering journal, and
> [docs/team-plan.md](docs/team-plan.md) for how the remaining work is decomposed.

## Why this design

The system uses a **thin edge / smart server** split: the ESP32 performs deterministic,
timer-driven acquisition and light on-device filtering, while the heavier DSP (bandpass
filtering, QRS detection, vitals derivation, alarming) runs server-side where it can be
regression-tested against annotated clinical data. This mirrors how bedside monitors and
central monitoring stations divide work in practice.

## Architecture

```
AD8232 ECG AFE ──analog──► ESP32 (360 Hz, notch, leads-off)     [not yet built]
MAX30102 SpO2  ──I2C────►    │
Temp sensor    ──1-Wire──►   │
                             │  WebSocket  /ingest
                             ▼
              Node.js ingest server                              [built]
              • frame validation (protocol v1)
              • streaming DSP: bandpass + notch + Pan-Tompkins
              • vitals, signal quality, alarm engine
                             │  WebSocket  /stream
                             ▼
              Canvas dashboard, served by the same process       [built]
              • scrolling ECG with per-beat markers
              • HR / SpO₂ / temp tiles, alarm banner

  sim/replay.py ──────────────┘  replays MIT-BIH as if it were the device
```

The dashboard is a self-contained page served by the ingest server rather than a
separate front-end app — one process, no build step, so the demo starts with a single
command (decision D-21). Persistence is planned, not built.

## What it looks like

![Bedside monitor](docs/img/dashboard-bedside.jpg)

*Bedside view: MIT-BIH record 100 replayed at true 360 Hz through the acquisition model.
75 bpm against the record's 75.3 bpm reference, a caret over every detected beat, and SpO₂
derived server-side from raw red/IR.*

![Central station](docs/img/central-station.jpg)

*Central station: beds ordered by worst active alarm, not by bed number, with HR and SpO₂
trends. A bed whose device disappears stays on screen marked offline — a tile vanishing
looks exactly like a patient being fine.*

![Device silent](docs/img/dashboard-device-silent.jpg)

*The behaviour the project is really about: when data stops arriving, the numbers are
**withdrawn** rather than held. A number on a monitor is a claim about now.*

## Verification

The DSP pipeline is validated against the
[MIT-BIH Arrhythmia Database](https://physionet.org/content/mitdb/) using cardiologist
beat annotations as ground truth. Results are generated automatically into
[docs/verification.md](docs/verification.md) by `dsp/validate.py`, which exits non-zero
if a requirement fails.

| | Beats | Se % | PPV % |
|---|---|---|---|
| Offline pipeline (Python, zero-phase) | 22,459 | 98.73 | 98.64 |
| Streaming pipeline (JS, causal — what the server runs) | 22,459 | 98.69 | 98.63 |
| Clean records only (100, 101, 103, 115) | 8,175 | 99.98 | — |
| Offline pipeline, **through the modelled acquisition chain** | 22,459 | 98.78 | 98.59 |

That third row is the same recordings pushed through `sim/afe_model.py` first — an
instrumentation amplifier built from E12 component values, an electrode half-cell offset,
mains coupling surviving the common-mode rejection, 3.3 V rails and a 12-bit ESP32 ADC
with its own offset, gain error, nonlinearity and sampling jitter. The detector loses
0.05 points of positive predictivity and gains 0.04 of sensitivity, so *"the pipeline
works on ADC counts"* is a measurement here rather than an assumption.

Worst-case heart-rate error is 2.18 bpm against a ±5 bpm requirement. The difficult
records (105, 108, 203) are reported honestly rather than tuned away — see
[docs/verification.md](docs/verification.md) for why.

Beyond the recordings, eight **fault-injection scenarios** drive the real server over the
real protocol and measure each requirement end to end — leads-off alarms at 1.51 s, a 30 s
outage loses 0 of 787 frames, a swamped signal withholds 102 of 102 rates rather than
guessing. `sim/run_scenarios.py`, results in [verification.md](docs/verification.md).

## Documentation

| Document | What it is |
|---|---|
| [requirements.md](docs/requirements.md) | Requirements and traceability — **generated**; status computed from evidence, never asserted |
| [verification.md](docs/verification.md) | All measured results — **generated** by `dsp/validate.py` |
| [design.md](docs/design.md) | Context, block, sequence and state diagrams |
| [safety.md](docs/safety.md) | IEC 60601 awareness and an 11-row hazard analysis |
| [protocol.md](docs/protocol.md) | The device↔server contract |
| [decisions.md](docs/decisions.md) | 50 decisions, each with the alternative rejected |
| [progress.md](docs/progress.md) | What is finished, what it measures, what is left open |
| [demo.md](docs/demo.md) | A three-minute walkthrough script |
| [BUILD_LOG.md](BUILD_LOG.md) | The engineering journal — 20 sessions, with the bugs |

## Running the whole system, with no hardware

```bash
# 1. Python environment + the MIT-BIH records
python3 -m venv .venv
.venv/bin/pip install -r dsp/requirements.txt
.venv/bin/python dsp/fetch_data.py          # downloads 10 records from PhysioNet

# 2. Start the ingest server + dashboard
cd server && npm install && npm start        # ws://127.0.0.1:8080, dashboard on :8080

# 3. In another terminal, replay a real recording into it
.venv/bin/python sim/replay.py --record 100
```

Then open <http://127.0.0.1:8080> — record 100 renders at ~75 bpm against its 75.3 bpm
reference, with a caret over every detected beat.

### Demonstrating the alarm paths

```bash
.venv/bin/python sim/replay.py --record 100 --scenario leads-off   # electrode detaches
.venv/bin/python sim/replay.py --record 100 --scenario desat       # SpO2 falls below 92 %
.venv/bin/python sim/replay.py --record 105 --scenario noise       # signal quality degrades
.venv/bin/python sim/replay.py --synthetic --hr 130                # sustained tachycardia
```

### With history (optional)

Session history, trends and CSV export need a database. Without one the monitor runs
exactly as above — persistence is optional by design (D-43).

```bash
docker compose up -d db                       # PostgreSQL on :5432
cd server && DATABASE_URL=postgres://vitalsense:vitalsense@127.0.0.1:5432/vitalsense npm start
```

Then, after a session:

```bash
curl -s localhost:8080/api/sessions                      # what has been recorded
curl -s localhost:8080/api/sessions/1/alarms             # the alarm timeline
curl -sO localhost:8080/api/sessions/1/vitals.csv        # export
```

> Fault-injection scenarios must be replayed at `--speed 1`. Alarm debounces are
> wall-clock, so a faster replay compresses a ten-second fault below the time the alarm
> needs to fire; the simulator warns when this would happen (D-46).

### Reproducing the verification numbers

```bash
.venv/bin/python dsp/validate.py            # regenerates docs/verification.md
.venv/bin/python dsp/filters.py             # filter self-test  (15 checks)
.venv/bin/python dsp/pan_tompkins.py        # detector self-test (23 checks)
.venv/bin/python dsp/vitals.py              # vitals self-test  (28 checks)
.venv/bin/python sim/afe_model.py           # acquisition-chain self-test (47 checks)
.venv/bin/python sim/plot_afe.py            # regenerate the figures in docs/img/
cd server && npm test                       # 43 tests
cd server && npm run validate:streaming     # scores the JS port on the same records
```

## Repository layout

| Path | Contents |
|---|---|
| `dsp/` | Python DSP: filters, Pan-Tompkins QRS detection, MIT-BIH validation, coefficient generation |
| `sim/` | Dataset replay, plus the analog front-end and ADC model — runs the whole system without hardware |
| `server/src/` | Node.js ingest, streaming DSP port, session state, alarm engine |
| `server/public/` | The live dashboard (single canvas page) |
| `firmware/` | ESP32 firmware — generated filter coefficients only so far |
| `docs/` | Requirements, verification results, wire protocol, team/build plan |
| `data/` | MIT-BIH records (gitignored; `dsp/fetch_data.py` downloads them) |

## Honest limitations

- **No hardware yet.** The analog front end and ADC are *modelled* (`sim/afe_model.py`,
  figures in `docs/img/`), not built; the ESP32 firmware and physical sensors do not
  exist yet. SR-02, SR-03, SR-05 and SR-06 are verified in simulation or not at all.
  What the model cannot cover: real electrode–skin impedance, actual AD8232 silicon,
  RF behaviour, and anything to do with patient isolation.
- **SpO₂ has no algorithm.** The value is currently passed through from the frame; the
  ratio-of-ratios pipeline is not written, so SR-06 is unverified.
- **Signal quality is partial.** The flat-line gate is implemented and tested; the full
  quality metric that SR-07 calls for is not.
- **No persistence.** Sessions live in memory only; history and export are planned.
- This is a student project and makes **no claim of IEC 60601 compliance** — see the
  standards-awareness note in [docs/requirements.md](docs/requirements.md).
