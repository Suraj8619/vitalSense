# VitalSense — Multi-Parameter Patient Vitals Monitor

### End-to-end project plan (hardware + firmware + DSP + dashboard + verification)

> Target: Philips healthcare electronics intern/trainee role.
> The JD keywords this project must visibly hit: **requirements → high-level design → integration → verification**, **Model-Based Systems Engineering**, **automation**, patient monitoring domain.
> (Rename the project if you like — pick the name before the repo goes public and keep it consistent.)

---

## 1. System overview

```
 [Patient]
   │  3-lead electrodes          finger clip              contact/IR
   ▼                              ▼                        ▼
 AD8232 ECG AFE ──analog──► ESP32 ADC (360 Hz)      MAX30102 (I2C, 100 Hz)   DS18B20/MLX90614 temp
                                 │
                        Firmware (FreeRTOS tasks)
                        • timer-driven sampling
                        • leads-off detection
                        • 50 Hz IIR notch (on-device)
                        • framing + buffering
                                 │ WiFi / WebSocket (JSON or binary frames)
                                 ▼
                        Node.js ingest server
                        • full DSP chain (bandpass, Pan-Tompkins QRS)
                        • HR, SpO2, temp aggregation
                        • alert rules (tachy/brady/leads-off/SpO2 low)
                        • persistence (PostgreSQL)
                                 │ WebSocket fan-out
                                 ▼
                        Next.js dashboard
                        • live ECG waveform (canvas/uPlot)
                        • numerics tiles (HR, SpO2, Temp) + alarm banner
                        • session history & export
```

Design principle to say out loud in interviews: **thin edge, smart server** — the ESP32 does deterministic sampling and light filtering; heavy DSP lives server-side where it can be validated against reference data. (Real patient monitors make the same split between acquisition front-end and central station — e.g. Philips IntelliVue + PIC iX.)

---

## 2. Bill of materials (all common in college labs / ~₹1,200–1,800 total)

| Item                                           | Qty | Approx ₹ | Notes                                                         |
| ---------------------------------------------- | --- | -------- | ------------------------------------------------------------- |
| ESP32 DevKit v1 (WROOM-32)                     | 1   | 400      | WiFi + 2 usable ADC channels + I2C                            |
| AD8232 ECG module + cable + electrode pads     | 1   | 300      | Single-lead ECG analog front-end; has LO+/LO− leads-off pins  |
| MAX30102 pulse-oximeter module                 | 1   | 200      | HR + SpO2 via I2C                                             |
| DS18B20 (contact) or MLX90614 (IR) temperature | 1   | 100–300  | Either is fine; MLX90614 is more "medical-like" (non-contact) |
| Breadboard + jumpers                           | —   | 200      |                                                               |
| Spare electrode pads                           | 10  | 100      | You WILL burn through pads while testing                      |
| (Optional) 3.7 V LiPo + TP4056                 | 1   | 250      | Battery operation = talking point on patient isolation/safety |

Fallback: if any sensor is unavailable, that channel runs in **simulated mode** (see §6) and the docs say so honestly — "SpO2 channel validated in simulation."

---

## 3. Work breakdown — phased

> Originally scoped as 7 working days with hardware. Re-cut under D-24 when the board
> did not arrive; see the amendment note below.

### Phase 0 — Repo & dataset (Day 1, ~2 h)

- [x] Create repo: `firmware/`, `dsp/`, `server/`, `dashboard/`, `docs/`, `sim/`
- [x] Download MIT-BIH Arrhythmia Database (PhysioNet), install Python `wfdb`
- [x] README skeleton with the architecture diagram

### Phase 1 — Offline DSP pipeline in Python (Day 1–2)

The scientific core. Built and validated BEFORE hardware, so hardware bring-up has a known-good reference.

- [x] Load MIT-BIH records (360 Hz — we sample our own ECG at the same rate on purpose; say this in interviews)
- [x] Filters: Butterworth bandpass 0.5–40 Hz + 50 Hz IIR notch (design in scipy; export coefficients for firmware)
- [x] **Pan-Tompkins QRS detector**: derivative → squaring → moving-window integration → adaptive thresholds + refractory period
- [x] HR from RR intervals; bonus: SDNN/RMSSD (HRV)
- [x] **Validation vs cardiologist annotations**: sensitivity & positive predictivity per record; target >99% Se on clean records (100, 101, 103…), report honestly on noisy ones (105, 108, 203)
- [x] Results auto-generated into `docs/verification.md` (this IS the JD's "automation" + "verification")

> **Plan amendment (2026-08-19, D-24).** Hardware did not arrive. Rather than block on
> it, the acquisition phases below are re-cut so that everything a board would have
> given us is _modelled and verified in simulation_, and the physical bring-up becomes a
> backlog that upgrades the demo instead of gating it. The risk register (§6) always
> named this path; this is it being taken. Phase numbering keeps a prime (2′, 3′) so the
> original intent stays legible.

### Phase 2′ — Simulated acquisition front-end (replaces hardware bring-up)

Model the signal path a board would have provided, so the DSP is validated on what the
ADC would actually produce rather than on idealised millivolts.

- [x] Analog front-end model: AD8232 instrumentation amp + analog band + right-leg drive; Bode plots into `docs/img/`, component-value rationale written down
- [x] `sim/afe_model.py`: MIT-BIH mV → ESP32 12-bit ADC counts — gain, 3.3 V range, quantisation, ADC nonlinearity, sampling jitter
- [x] Electrode model: half-cell DC offset and drift, motion artefact, 50 Hz mains coupling, **leads-off railing**
- [x] Re-run the full MIT-BIH validation _through_ the AFE model; publish the quantisation cost as a measured delta in `docs/verification.md`

### Phase 3′ — Firmware, verified without a board

The firmware is written for the ESP32 but built and scored on the host, so its DSP is
verified against the same annotated data as the Python and JS implementations.

- [x] Portable C core (no Arduino/ESP-IDF headers) with a host build and unit tests — this is what makes the rest verifiable off-target
- [x] Sample ring buffer sized for the 30 s outage requirement, overwrite-oldest with the loss counted (SR-05, D-27)
- [x] FreeRTOS tasks: `ecgSampleTask` (timer-driven, pinned to core 1), `netTask`, `ppgTask`, `tempTask`, `statsTask` — builds for ESP32 (D-29, D-30)
- [x] On-device 50 Hz IIR notch from the generated coefficients, fixed-point (know why fixed-point, and what saturates)
- [x] Host harness scores the firmware notch against the Python design on MIT-BIH — closes the loop Python ↔ JS ↔ C
- [x] Leads-off detection (LO pins + rail cross-check, asymmetric debounce) → status flag in every frame (D-28)
- [x] Protocol-v1 frame construction in C, checked against the server's own validator with no board and no network
- [x] Store-and-forward with backfill on reconnect — 20 s outage verified sample-for-sample lossless (SR-05)
- [x] ESP32 WiFi + WebSocket client, with a serial-transport build for the simulator
- [x] Wokwi ESP32 simulation: image builds, board and stimulus wired (`firmware/diagram.json`) — configuration not yet executed by the author

### Phase 3.5 — SpO₂, the missing parameter

`spo2` is currently passed straight through from the frame — there is no algorithm, so
"multi-parameter" is not yet earned and SR-06 is unverifiable.

- [x] PPG synthesiser: red/IR channels with _known_ SpO₂, perfusion index, motion artefact (same pattern as `dsp/synth.py`)
- [x] SpO₂ by ratio-of-ratios: DC/AC separation, calibration curve, pulse rate from PPG, perfusion index
- [x] Validate against synthetic ground truth — 432 recordings, zero wrong readings reported
- [x] Validate pulse detection against the BIDMC ICU dataset — 1.04 bpm mean error, 98.6 % within ±5 bpm (D-36, D-37)
- [x] Port SpO₂ to JS, score the port, and wire it into the server — protocol v2 carries raw red/IR; port agrees to 0.0000 on every metric (D-38, D-39)
- [x] **Signal-quality metric (SR-07)**: pSQI, basSQI, kurtosis and QRS energy concentration → `good`/`poor`/`unusable`; heart rate withheld when unusable (D-35)
- [x] Re-run MIT-BIH with quality gating — record 108 PPV 90.95 → 97.37 %, arrhythmic records untouched (closes D-10)

### Phase 4 — Server + dashboard

- [x] Node.js ingest: validate frames → streaming DSP → vitals → alarm engine → WebSocket fan-out
- [x] Live dashboard: scrolling ECG with QRS markers, numerics tiles, alarm banner, device selector (canvas page served by the server, D-21)
- [x] PostgreSQL persistence: sessions, 1 Hz vitals, alarm episodes; one docker-compose; runs with no database at all (D-42, D-43)
- [x] Session history API (`/api/sessions`, `/vitals`, `/alarms`) and CSV export with blank fields for unknown values
- [x] Alarm acknowledge and re-alarm policy — a silence expires and belongs to one episode (D-44)
- [x] Central-station view: multiple devices on one screen, alarm-ordered, with HR/SpO₂ trends and inline acknowledge (D-48)
- [x] Keep infra minimal — one docker-compose, two services, no broker and no orchestrator (D-06)

### Phase 5 — Integration & verification

- [x] Scenario harness: 8 fault-injection scenarios against the real server, results auto-written into `docs/verification.md` (D-49)
- [x] End-to-end latency measured: median 0.5 ms, p95 2.1 ms, worst 7.5 ms on loopback — the WiFi term is named, not folded in
- [x] 30 s outage scenario: 787/787 frames delivered, 0 gaps in `seq`
- [x] Requirements and traceability generated from a machine-readable model plus measured evidence (D-50)
- [x] CI: six jobs — DSP self-tests, server tests, firmware host build, generated-file drift gate, MIT-BIH validation, scenario matrix

### Phase 6 — Docs, demo, polish

- [x] MBSE: context, block, sequence and state diagrams in `docs/design.md` (7 diagrams)
- [x] Safety notes: IEC 60601-1/-2-27 awareness, isolation, battery rationale, 11-row hazard analysis in `docs/safety.md`
- [x] Demo script in `docs/demo.md` with screenshots in `docs/img/` — the video itself is not recorded
- [x] README: architecture, results, honest limitations section
- [x] `sim/` instructions so anyone can run the whole system with zero hardware (dataset replay)
- [x] Decision log moved to `docs/decisions.md` — 50 decisions, grouped by theme
- [x] Interview Q&A prep in `docs/interview.md`, mapped to evidence
- [ ] Resume update (outside this repo)

### Hardware backlog (unblocked the day a board arrives — upgrades the demo, does not gate it)

- [ ] AD8232 → ESP32: OUTPUT→GPIO36 (ADC1_CH0), LO+→GPIO25, LO−→GPIO26, 3.3 V/GND. Electrodes RA/LA/RL (right leg = reference/DRL)
- [ ] MAX30102 on I2C (GPIO21/22); DS18B20 on OneWire (GPIO4); verify each on the serial plotter before any networking
- [ ] Own-ECG validation: pipeline HR vs manual 30-s pulse count and/or MAX30102 cross-check
- [ ] Re-run the Phase 5 scenario matrix on real hardware and diff the numbers against the simulated results
- Electrode hygiene: fresh pads, gel, still subject, wires away from mains — 80 % of "bad ECG" is electrodes, not code

### Phase 2 — Hardware bring-up (Day 3)

- [ ] AD8232 → ESP32: OUTPUT→GPIO36 (ADC1_CH0), LO+→GPIO25, LO−→GPIO26, 3.3 V/GND. Electrodes: RA/LA/RL (right leg = reference/DRL)
- [ ] Timer-ISR sampling at 360 Hz into a ring buffer (never sample in `loop()` — jitter ruins DSP; interview talking point)
- [ ] MAX30102 on I2C (GPIO21/22); DS18B20 on OneWire (GPIO4)
- [ ] Verify each sensor on serial plotter before any networking
- Electrode hygiene: fresh pads, gel, still subject, wires away from mains — 80 % of "bad ECG" is electrodes, not code

### Phase 3 — Firmware complete (Day 3–4)

- [ ] FreeRTOS tasks: `ecgSampleTask` (timer-driven), `ppgTask`, `tempTask`, `netTask`
- [ ] On-device 50 Hz IIR notch using Phase-1 coefficients (fixed-point or float — know why you chose)
- [ ] Leads-off detection → status flag in every frame
- [ ] WebSocket client; frames: `{t, ecg[], hr_raw, spo2, temp, leadsOff}`; buffer + reconnect on WiFi drop (no data loss ≤30 s outage)

### Phase 4 — Server + dashboard (Day 4–5, your home turf — timebox it!)

- [x] Node.js ingest: validate frames → run DSP (port of Phase 1 chain) → compute vitals → alert engine (HR >100 / <60 sustained 10 s, SpO2 <92 %, leads-off, signal-quality flag) → fan-out WebSocket → PostgreSQL sessions
- [x] Next.js dashboard: scrolling ECG waveform with QRS markers, numerics tiles, alarm banner with acknowledge, session replay/export CSV
- [ ] Keep infra minimal — no k8s/RabbitMQ here; AsyncForge already proves that skill. One docker-compose is plenty.

### Phase 5 — Integration & verification (Day 6)

- [ ] End-to-end latency test: electrode → dashboard (<500 ms target; measure, don't claim)
- [ ] Own-ECG validation: pipeline HR vs manual 30-s pulse count and/or MAX30102 HR cross-check
- [ ] Fault-injection: pull an electrode (leads-off alarm <2 s), kill WiFi (buffering works), noisy signal (quality flag)
- [ ] Fill traceability matrix (§5) with actual results

### Phase 6 — Docs, demo, polish (Day 7)

- [ ] Demo video (2–3 min): electrodes on → live waveform → pull electrode → alarm
- [ ] README: architecture, screenshots, validation table, honest limitations section
- [x] `sim/` instructions so anyone can run the whole system with zero hardware (dataset replay)
- [ ] Then: resume update + interview Q&A prep

Commits happen as the work happens, in your name — normal, incremental, honest history.

---

## 4. Requirements → verification (the MBSE story — your differentiator)

`docs/requirements.md`, kept small but rigorous:

| ID    | Requirement                                  | Design element                             | Test                           | Result |
| ----- | -------------------------------------------- | ------------------------------------------ | ------------------------------ | ------ |
| SR-01 | HR accuracy ±5 bpm vs reference              | Pan-Tompkins on 360 Hz ECG                 | MIT-BIH validation + self-test | —      |
| SR-02 | End-to-end latency <500 ms                   | WS streaming, no batch                     | timestamp diff test            | —      |
| SR-03 | Leads-off alarm within 2 s                   | AD8232 LO pins → frame flag → alert engine | pull-electrode test            | —      |
| SR-04 | QRS Se >99 % on clean MIT-BIH records        | filter chain + adaptive threshold          | wfdb annotation compare        | —      |
| SR-05 | No data loss on ≤30 s WiFi outage            | ring buffer + reconnect                    | kill-AP test                   | —      |
| SR-06 | SpO2 reading within ±3 % of module reference | MAX30102                                   | cross-check                    | —      |

Plus: system context diagram + one sequence diagram (draw.io/Mermaid), and a **standards-awareness note**: designed _with awareness of_ IEC 60601-1 (electrical safety) and 60601-2-27 (ECG monitoring) — battery operation, no mains connection during measurement, leads-off alarming. _Awareness, not compliance_ — saying it this way shows maturity; claiming compliance shows the opposite.

---

## 5. Simulation strategy (three layers, use 1 and 2 for sure)

1. **Full-system sim without hardware (must-have):** `sim/replay.py` streams MIT-BIH records through the real server + dashboard at 360 Hz, as if from the ESP32. Demo anywhere, any time — including on your laptop in an interview. This is genuine hardware-in-the-loop-style thinking; name it that.
2. **Analog filter design (must-have):** design/verify the notch + bandpass in **LTspice** (free) or **Falstad** (browser); export Bode plots into `docs/`. Interviewers love asking "why these component values / coefficients."
3. **Firmware sim (optional):** **Wokwi** simulates ESP32 + WiFi; feed synthetic ECG via a custom chip/ADC source to test firmware logic without the lab. **Proteus** (college license) can play ECG waveform files into a filter chain if you want schematic-level sim.

---

## 6. Risk register

| Risk                                  | Mitigation                                                                                                                       |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Interview called before hardware done | Phase 1 + sim replay is already a complete, demoable story; hardware becomes "in progress — here's the design" (honest and fine) |
| Noisy/unusable ECG                    | Electrode discipline, DRL electrode placed correctly, notch filter; worst case: quality flag + document it as a finding          |
| Sensor module dead/unavailable        | Channel runs in simulated mode, documented as such                                                                               |
| Scope creep in dashboard (you!)       | Dashboard is timeboxed to 1.5 days; the ECE depth is what gets you hired here, not another React feature                         |

---

## 7. Interview ammunition this project generates

- Why 360 Hz? (MIT-BIH rate → directly comparable validation; Nyquist: QRS energy lives ≤40 Hz, clinical monitors sample 250–500 Hz)
- Why notch + bandpass, and IIR vs FIR trade-offs on a microcontroller
- Pan-Tompkins stages and why adaptive thresholding beats a fixed threshold
- Timer-ISR sampling vs polling; jitter's effect on DSP
- Leads-off detection: how AD8232 does it electrically
- Edge/server processing split and its parallel in real patient monitoring architecture
- Verification methodology: Se/PPV vs annotated ground truth, traceability matrix
- Safety: why battery operation matters, what IEC 60601 covers

---

## 8. How we work (the working agreement)

1. **This file is the source of truth.** Every work session starts by reading PLAN.md, works the next unchecked box, and ends by checking boxes off.
2. **Every session gets a BUILD_LOG.md entry** — what we did, why we did it that way, what broke, what we learned, and the interview question it prepares us for. No entry = session didn't happen.
   2b. **Every completed phase gets a `docs/progress.md` entry** — delivered / verified / decisions / left open, plus the status table at the top of that file. BUILD_LOG records how a session went; progress.md records what is finished and what the evidence is. The two are not interchangeable and a phase is not done until both exist.
3. **Decisions get logged in §10** the moment they're made, with the rejected alternative. "Why did you choose X over Y" is the single most common project interview question — the decision log IS the answer bank.
4. Commits are small and incremental, in Abhijeet's name, made as the work happens.

## 9. Repo structure (create in Phase 0)

```
vitalsense/
├── PLAN.md              ← this file
├── BUILD_LOG.md         ← the journal (see template inside)
├── README.md            ← public-facing: architecture, screenshots, results
├── dsp/                 ← Python: filters, Pan-Tompkins, MIT-BIH validation
│   ├── filters.py       ← Butterworth bandpass + 50 Hz notch (exports coeffs as C header)
│   ├── pan_tompkins.py
│   ├── validate.py      ← runs records, emits docs/verification.md tables
│   └── requirements.txt
├── sim/
│   ├── replay.py        ← streams MIT-BIH → server over WS at 360 Hz (the no-hardware demo)
│   └── afe_model.py     ← analog front-end + ADC model: mV → 12-bit counts (Phase 2′)
├── firmware/            ← PlatformIO project, ESP32 (Arduino framework)
│   ├── src/main.cpp     ← FreeRTOS tasks: sample / ppg / temp / net
│   └── test/            ← host build: the same DSP scored against MIT-BIH (Phase 3′)
├── server/
│   ├── src/             ← Node.js ingest + streaming DSP port + alarm engine
│   └── public/          ← the live dashboard (single canvas page, D-21)
│   └── replay.py        ← streams MIT-BIH → server over WS at 360 Hz (the no-hardware demo)
├── firmware/            ← PlatformIO project, ESP32 (Arduino framework)
│   └── src/main.cpp     ← FreeRTOS tasks: sample / ppg / temp / net
├── server/              ← Node.js ingest + DSP port + alert engine + Postgres
├── dashboard/           ← Next.js live waveform + numerics + alarms
├── docs/
│   ├── requirements.md  ← SR table + traceability matrix (§4)
│   ├── verification.md  ← auto-generated validation results
│   ├── decisions.md     ← mirror of §10 once it outgrows this file
│   └── img/             ← Bode plots, architecture diagram, screenshots
└── data/                ← MIT-BIH records (gitignored; download script instead)
```

## 10. Decision log

Moved to [docs/decisions.md](docs/decisions.md) — 50 decisions, grouped by theme, each with
the alternative that was rejected and why. New decisions go there.
| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| D-01 | Sim-first, hardware-second build order | Hardware first | Algorithm needs annotated ground truth to validate (a live sensor can't say if a beat was missed); hardware availability is the schedule risk, so it goes on the critical path last |
| D-02 | 360 Hz ECG sampling | 250/500 Hz | Matches MIT-BIH rate → identical filter coefficients and directly comparable validation between sim and device |
| D-03 | Thin edge / smart server split | Full DSP on ESP32 | Server-side DSP is testable against dataset, hot-swappable, and mirrors real monitor architecture (bedside unit + central station); ESP32 keeps only deterministic sampling + light notch |
| D-04 | Pan-Tompkins for QRS | ML classifier | Interpretable, defensible stage-by-stage in an interview, no training data pipeline needed, still the industry baseline; ML can be layered on later |
| D-05 | Plain WebSocket streaming | MQTT | One less broker to run; MQTT's QoS matters for fleets of devices, not one — and we handle outages with device-side buffering (SR-05). Know this trade-off cold for interviews |
| D-06 | Postgres, single docker-compose | K8s/RabbitMQ like AsyncForge | Right-sizing infra is the "mindset to simplify" line in the JD; AsyncForge already demonstrates the heavy stack |
| D-07 | Detection path filtered 5-15 Hz, separate from the 0.5-40 Hz display path | One filter chain for both | QRS energy peaks at 5-15 Hz; that band maximises detection but distorts the waveform a clinician reads. Real monitors keep the two paths separate for the same reason |
| D-08 | Absolute flat-line amplitude gate before the adaptive threshold | Adaptive threshold alone | An adaptive threshold rescales to whatever it is given, so on a detached electrode it locks onto microvolt numerical residue and reports a heart rate. Found by a self-test on constant-DC input |
| D-09 | Report heart rate as the median of instantaneous rates | 60 / mean(RR) | One spurious or missed beat shifts a mean far enough to trip an alarm; the median absorbs it. Combined with a physiological RR plausibility filter (0.25-3 s) |
| D-10 | Publish honest per-record results, including the bad ones | Tune thresholds per record, report the best | Per-record tuning inflates these numbers and degrades unseen signals. The useful fix is a signal-quality flag (SR-07), not a better-looking table |
| D-11 | Verification report generated by script, never hand-edited | Hand-written results table | A hand-maintained table drifts from the code silently; `dsp/validate.py` regenerates it and exits non-zero on requirement failure, so it can gate CI |
| D-12 | One filter design in Python, generating C and JS coefficient files | Hand-transcribe coefficients into each language | Three implementations filter the same ECG (validation, firmware, server). Transcribing by hand means the validated pipeline and the deployed pipeline eventually differ, and nothing catches it |
| D-13 | Streaming detector scored against MIT-BIH like the offline one | Trust that the port is equivalent | "The causal port matches" is a measurable claim; `server/tools/validateStreaming.js` measures it on the same records, annotations and +/-150 ms window |
| D-14 | Detector refuses a sampling rate its coefficients were not designed for | Accept any fs and filter anyway | Silently applying 360 Hz coefficients to a 250 Hz stream would corrupt real patient data with no error; failing loudly is the safe behaviour |
| D-15 | Frames are blocks of samples with a sequence number, not single samples | One WebSocket message per sample | At 360 Hz per-sample framing costs more in overhead than payload; `seq` gaps are what makes lost data detectable rather than invisible |
| D-16 | Absent sensor readings are sent as `null` | Repeat the last good value | Holding a stale reading makes a disconnected sensor look healthy - the most dangerous failure mode a monitor has |
| D-17 | Beats are reported as absolute sample indices with a frame anchor | Offsets into the current frame | Detection lags the R peak by more than one frame, so most offsets were negative and were being dropped - only ~8% of beats reached the dashboard |
| D-18 | Alarm debounce sits inside the requirement budget, not on top of it | 2000 ms debounce for a 2 s requirement | A debounce equal to the budget makes the requirement unmeetable by construction; measured 2.1 s, now 1.5 s debounce leaves 500 ms margin |
| D-19 | Asystole and signal-poor suppressed when a more specific alarm explains the fault | Fire every alarm whose condition holds | A detached lead is not a cardiac arrest, and a silent device has no signal to judge. Redundant alarms are the mechanism of alarm fatigue |
| D-20 | Sessions survive a disconnect for 30 s, then are reaped | Delete on disconnect / keep forever | Deleting immediately loses filter state across a WiFi blip (SR-05); keeping forever leaves finished sessions alarming DEVICE*SILENT indefinitely |
| D-21 | Dashboard is a self-contained canvas page served by the existing server | Separate Next.js app (as originally planned) | One process and no build step means the demo starts with a single command and cannot fail on an unfamiliar machine mid-interview. Next.js is already demonstrated by AsyncForge and SonicScribe, so nothing is lost by right-sizing here |
| D-22 | Acquisition restarts and the rate is withheld for 3 s after electrodes reconnect | Keep filtering and detecting straight through | The reconnection step drives a transient through the IIR filters while the adaptive thresholds are still tuned to the flat line; the detection burst was displayed as 190 bpm tachycardia with signal quality "good". Bedside monitors blank the rate across this settling period for the same reason |
| D-23 | Stale values are withdrawn from the display, not left on screen | Keep showing the last received number | A number on a monitor is a claim about \_now*. After 3 s without a frame the dashboard shows "--" and flags NO DATA rather than letting a clinician read a number that is no longer true |
