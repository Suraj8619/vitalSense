# Build progress

One entry per **completed phase**, written the moment its boxes are all ticked. Three
documents track this project and they answer different questions:

| Document | Question it answers |
|---|---|
| [PLAN.md](../PLAN.md) | What is *going* to be built, and what is left |
| [BUILD_LOG.md](../BUILD_LOG.md) | How a *session* went — the reasoning, the bugs, the lessons |
| **this file** | What is *finished*, what it measures, and where the evidence is |

If a phase is not listed here, it is not done.

---

## Status at a glance

| Phase | Scope | State | Headline evidence |
|---|---|---|---|
| 0 | Repo, dataset, scaffold | ✅ done | 10 MIT-BIH records, reproducible fetch |
| 1 | Offline DSP in Python | ✅ done | Se 98.73 % / PPV 98.64 % on 22,459 beats |
| 4a | Wire protocol + streaming DSP port (JS) | ✅ done | causal port scored: Se 98.69 % / PPV 98.63 % |
| 4b | Ingest server, alarms, replay simulator | ✅ done | end-to-end without hardware; leads-off at 1.5 s |
| 4c | Live dashboard | ✅ done | 75 bpm on screen vs 75.3 bpm reference |
| 2′ | Analog front-end + ADC model | ✅ done | detector loses 0.05 pts PPV through the chain |
| 3′ | Firmware, verified on a host build | ✅ done | frames pass the server's own validator; SR-05 lossless |
| 3.5 | SpO₂ and signal quality | ✅ done | 0 wrong saturations of 432; SR-07 verified |
| 4′ | Persistence, history, central station | ✅ done | verified against real PostgreSQL |
| 5 | Integration, fault injection, CI | ✅ done | 8/8 scenarios; all 8 requirements verified |
| 6 | MBSE diagrams, safety notes, demo | ✅ done | 7 diagrams, 11-row hazard analysis, demo script |

**Test count across the repo:** 191 Python self-checks + 74 C unit tests + 17 frame-contract
checks + 101 Node tests + 8 end-to-end fault-injection scenarios, all passing, plus three cross-language scoring tools (streaming QRS,
firmware notch, SpO₂). The ESP32 image builds (RAM 21.4 %, flash 69.6 %).

---

## Phase 0 — Repo and dataset

**Delivered:** repository scaffold (`dsp/`, `sim/`, `firmware/`, `server/`, `docs/`),
Python venv and pinned requirements, `dsp/fetch_data.py` — a reproducible PhysioNet
downloader rather than committed data.

**Verified:** 10 MIT-BIH records fetched, chosen before any results were seen to span
clean (100, 101, 103, 115), arrhythmic (106, 119, 208) and famously noisy (105, 108, 203)
signals — so the verification table cannot be cherry-picked after the fact.

**Decisions:** D-01 sim-first build order · D-02 360 Hz sampling.

---

## Phase 1 — Offline DSP pipeline (Python)

**Delivered:** `dsp/filters.py` (0.5–40 Hz Butterworth bandpass in SOS form + 50 Hz IIR
notch, in both zero-phase offline and stateful streaming modes, exporting C coefficients),
`dsp/synth.py` (synthetic ECG with exact known R-peak locations), `dsp/pan_tompkins.py`
(the 1985 algorithm end to end), `dsp/vitals.py` (RR, heart rate, SDNN/RMSSD, rhythm
classification), `dsp/validate.py` (scores MIT-BIH and generates the verification report),
`docs/requirements.md` (UN-1..4, SR-01..08 with traceability).

**Verified — 22,459 annotated beats, 301 minutes of real patient ECG:**

| | Se % | PPV % |
|---|---|---|
| Pooled, all 10 records | 98.73 | 98.64 |
| Clean records only (100, 101, 103, 115) | 99.98 | — |

Worst-case heart-rate error 2.18 bpm against a ±5 bpm requirement. **SR-01 and SR-04
PASS.** Pipeline runs ~7,200× real time. Evidence: [verification.md](verification.md).

**Decisions:** D-07 separate detection and display filter paths · D-08 flat-line
amplitude gate before the adaptive threshold · D-09 heart rate as median of instantaneous
rates · D-10 publish the bad records honestly · D-11 verification report generated, never
hand-edited.

**Left open:** the signal-quality metric that D-10 names as the real fix for records 108
and 203 (SR-07 is `partial`) — deferred to Phase 3.5.

---

## Phase 4a — Wire protocol and streaming DSP port

**Delivered:** `docs/protocol.md` (the device↔server contract), `dsp/export_coefficients.py`
(one filter design generating **both** the firmware C header and the server's JS module),
`server/src/dsp/` — `biquad.js` (transposed-DF2 sections, SOS cascade, ring buffer, O(1)
moving average), `qrsDetector.js` (causal streaming Pan-Tompkins), `vitals.js` —
and `server/tools/validateStreaming.js`, which scores the port on the same records,
annotations and ±150 ms window as the Python original.

**Verified — same records, same annotations:**

| | Se % | PPV % | Mean timing error |
|---|---|---|---|
| Offline (Python, zero-phase) | 97.41 | 99.22 | ≈3 ms |
| Streaming (JS, causal) | **98.69** | **98.63** | ≈10 ms |

The causal port is not worse overall — it trades slightly more false positives for better
sensitivity on the hard record. Throughput ≈30,000× real time. 23 JS tests.

**Decisions:** D-12 one design generating every implementation's coefficients ·
D-13 score the port rather than assume equivalence · D-14 detector refuses an unexpected
sampling rate · D-15 framed sample blocks with a sequence number · D-16 absent readings
sent as `null`.

---

## Phase 4b — Ingest server, alarm engine, replay simulator

**Delivered:** `server/src/protocol.js` (total frame validation), `alarms.js` (declarative
rules with sustain times), `session.js` (per-device DSP, rate window, alarm latching,
frame accounting), `index.js` (WebSocket server: `/ingest`, `/stream`, health endpoint,
silent-device watchdog), `sim/replay.py` (MIT-BIH streamed at true 360 Hz in the firmware's
frame format, with fault-injection scenarios).

**Verified end to end, replaying record 100:** heart rate 71.8–75.3 bpm against the
record's 75.3 bpm reference; 445 beats delivered over 6 minutes at 75 bpm (~450 expected);
leads-off alarm at **1.5 s** against a 2 s requirement; SpO₂ alarm at exactly its 10 s
sustain. 19 new tests.

**Decisions:** D-17 beats reported as absolute sample indices · D-18 alarm debounce inside
the requirement budget, not on top of it · D-19 suppress alarms a more specific alarm
already explains · D-20 sessions survive a 30 s disconnect, then are reaped.

---

## Phase 4c — Live dashboard

**Delivered:** `server/public/index.html` — scrolling ECG on canvas with a conventional
ECG-paper grid (200 ms major squares at 25 mm/s), per-beat detection carets, HR / SpO₂ /
temperature tiles, severity-ranked alarm banner, device selector, frame statistics. Served
as a static page by the existing server, with path-traversal rejection.

**Verified on screen:** record 100 rendering at **75 bpm** against its 75.3 bpm reference,
a caret over every detected beat, SIGNAL GOOD flag, live counters. After a simulated
disconnect: red DEVICE SILENT banner and all three numerics withdrawn to `--`.

**Decisions:** D-21 self-contained canvas page instead of a separate Next.js app ·
D-22 discard filter and threshold state on electrode reconnection and withhold the rate
for 3 s · D-23 withdraw stale values from the display rather than leaving them on screen.

**The bug that matters:** the monitor displayed **190 bpm, "tachycardia", signal quality
GOOD**, on a patient whose electrodes had just been reattached. Every guard in the
pipeline was a plausibility check, and the artefact was self-consistent — which is exactly
what let it through. Full account in BUILD_LOG 006.

---

## Phase 2′ — Analog front-end and ADC model

**Delivered:** `sim/afe_model.py` — the acquisition chain from electrode to ADC code:
half-cell offset and drift, motion artefact, mains coupling surviving CMRR and the
right-leg drive, an instrumentation amplifier whose component values are synthesised for
the target corners and snapped to the **E12 preferred series**, causal analog filtering,
3.3 V rails with per-sample saturation flags, sampling jitter interpolated from an 8×
oversampled signal, and a 12-bit ESP32 ADC with offset, gain error, deterministic INL and
noise. Plus `sim/plot_afe.py` → `docs/img/afe_bode.png` and `afe_chain.png`, and a second
scoring pass in `dsp/validate.py`.

**Verified — 22,459 beats, clean input vs acquired input:**

| | Se % | PPV % | Rail saturation |
|---|---|---|---|
| As stored (floating-point mV) | 98.73 | 98.64 | — |
| Through the acquisition chain | **98.78** | **98.59** | 0.00 % |

Detections arrive **7.1 ms** later on average — the group delay of the causal analog
filter, a fixed bias rather than scatter, and a term SR-02's latency budget now carries.
47 self-checks. Evidence: [verification.md](verification.md) § *Through the acquisition
chain*.

**Decisions:** D-24 acquisition phases re-cut as simulation · D-25 firmware builds for the
host as well as the target · D-26 front-end gain 330, sized from the ±5 mV input range
IEC 60601-2-27 expects.

**What the model changed in the design:** the gain started at the AD8232 datasheet's
~1100, which clipped records 203 and 208 — they peak above 4 mV. Clipping a QRS is losing
the one feature the device exists to find. No bench session would have caught this,
because the person holding the electrodes would not have produced a 4 mV QRS.

**What the model still cannot cover:** real electrode–skin impedance and motion-artefact
morphology, actual AD8232 silicon behaviour versus its datasheet, RF behaviour under
interference, and anything to do with patient isolation or leakage current.

---

## Phase 3′ — Firmware, verified on a host build

**Delivered:** the acquisition firmware, split so that everything except the target shell
is verifiable on a laptop.

*Portable C99, no Arduino dependency — the same translation units the ESP32 image links:*
`vs_notch.{h,c}` (Q14 fixed-point 50 Hz notch with steady-state priming),
`vs_ringbuf.{h,c}` (store-and-forward, 30 s at 360 Hz, overwrite-oldest with the loss
counted), `vs_leadsoff.{h,c}` (LO pins *or* a railed ADC reading, asymmetric debounce),
`vs_frame.{h,c}` (protocol-v1 JSON, no malloc, no floating-point printf).

*Generated, never hand-edited:* `notch_coeffs.h` now carries fixed-point coefficients
alongside the float ones and refuses to emit a header whose quantised poles leave the
unit circle; `vs_calibration.h` carries the counts→millivolts scaling, generated from
`sim/afe_model.py` so the device's millivolt and the pipeline's millivolt are the same
millivolt.

*Target shell:* `main.cpp` — timer ISR, five FreeRTOS tasks, WiFi, WebSocket client,
plus a serial-transport build for the simulator. `platformio.ini` with a pinned platform,
`wokwi.toml` and `diagram.json`.

*Host verification:* `firmware/test/` — Makefile, 47 unit tests, a filter harness, a
device emulator, `score_notch.py` and `validate_frames.mjs`.

**Verified:**

| Check | Result |
|---|---|
| Fixed-point notch vs the Python design, 6.5 M samples | **0.52 counts** worst case (1.28 µV at the electrodes) |
| Agreement with the *correctly rounded* float result | **99.46 %**, never off by more than 1 count |
| Firmware frames through the server's own `validateSampleFrame()` | **225 of 225 accepted**, zero warnings |
| SR-05, 20 s simulated outage | **zero samples lost**; 14,400 samples identical to the no-outage run, `seq` contiguous |
| Outage beyond the buffer | loss reported (3,616 samples), not silent |
| Leads-off assert / clear | 50.0 ms / 500.0 ms; 50 + 1500 ms server debounce = 1550 ms of SR-03's 2000 ms |
| ESP32 build | RAM 21.4 %, flash 69.6 % (Wokwi build 13.9 % / 21.7 %) |
| DC drift over 200,000 samples (9 minutes) | **zero counts** |

Three implementations of one filter design — Python float64, JavaScript float64 causal,
C Q14 fixed point — are now all scored on the same recordings.

**Decisions:** D-25 firmware builds for the host as well as the target · D-27 device
buffer overwrites oldest and counts the loss · D-28 asymmetric leads-off debounce, sized
to fit inside SR-03 alongside the server's · D-29 sampling ISR only gives a semaphore ·
D-30 pinned Espressif platform version · D-31 two transports behind one frame builder.

**Left open:**
- **The Wokwi simulation has not been run.** The image builds and the diagram loads in
  the VS Code extension with every part and connection resolving correctly; running it
  additionally needs a Wokwi licence (free, via the command palette). So the wiring is
  confirmed and the behaviour is not.
- **No PPG or temperature driver.** The tasks exist and do nothing; frames report those
  readings as `null` rather than as a plausible constant (D-16). The drivers wait on
  Phase 3.5, which is where the SpO₂ algorithm that would validate them lives.
- **Never run on physical hardware.** Nothing above involved a board.

---

## Phase 3.5 — SpO₂ and signal quality

**Delivered:** the second parameter, and the metric that decides when not to answer.

`dsp/ppg.py` — red/IR synthesiser with known saturation, ratio-of-ratios, pulse rate by
autocorrelation, perfusion index, and four refusal gates. `dsp/quality.py` — pSQI,
basSQI, kurtosis and QRS energy concentration → `good`/`poor`/`unusable`.
`dsp/fetch_ppg.py` and `dsp/validate_ppg.py` — the BIDMC ICU dataset and its scoring.
`server/src/dsp/ppg.js` — the port, with its coefficients and thresholds generated from
Python. `server/tools/validatePpg.js` — cross-language scoring. Protocol **v2**: the
device sends raw red/IR light and the server derives saturation, with `spo2Source` on
every vitals frame. Wired through `session.js` and shown on the dashboard.

**Verified:**

| Check | Result |
|---|---|
| Saturation, 432 synthetic recordings | **0 wrong readings reported** (216 accurate, 216 refused) |
| Saturation recovery, 80–100 % clean | worst error **0.00 %** |
| Pulse rate on 10 BIDMC ICU recordings | **1.04 bpm** mean error, 98.6 % within ±5 bpm |
| JS port vs Python design, 25 cases | **0.0000** on SpO₂, pulse, R and channel correlation; every accept/refuse decision matches |
| SR-07 across an SNR sweep | no wrong heart rate reported at any noise level |
| Quality gating, record 108 | PPV **90.95 → 97.37 %** |
| Quality gating, arrhythmic records 106/119 | **0.0 % unusable** — the gate does not silence ventricular beats |

**Decisions:** D-32 refuse SpO₂ when red and IR disagree · D-33 derive the PPG refractory
period · D-34 the calibration curve is an assumption, and R is reported beside it ·
D-35 withhold on QRS energy concentration, threshold derived from SR-01 · D-36 pulse rate
from the autocorrelation period, not a peak count · D-37 publish the periodicity gate's
residual · D-38 peak picking written out in both languages · D-39 the server speaks v1
and v2.

**Closed after the first pass** (see BUILD_LOG 016):
- **The MAX30102 driver now exists** — `firmware/src/vs_max30102.{h,c}`, portable C with
  the I2C transport injected, 23 host checks against a simulated part covering register
  sequencing, part-ID rejection, 18-bit decoding, FIFO wrap-around and overflow
  accounting. Wired into `ppgTask`, emitting raw red/IR in v2 frames. Never run on
  silicon.
- **The ECG/PPG cross-check is implemented** (D-37's named fix). It is asymmetric because
  the physiology is: a pulse rate *below* the heart rate is a real clinical finding
  (pulse deficit, reported not hidden); *above* it is impossible and the pulse rate is
  withheld; near exactly *half* is the signature of sub-harmonic lock.
- **The "~1 % wrong by up to 25 bpm" residual was a scoring artefact.** Those windows were
  scored against a bedside reference that was itself moving 20 bpm inside the comparison
  window. Excluding windows where the reference is unstable (D-40): mean error **0.73 bpm**
  and **100 % of accepted windows within ±5 bpm**, with 28 of 160 windows excluded and
  reported.

**Still open — genuinely needs hardware:**
- **The R → saturation calibration curve is unvalidated.** Verifying it needs raw
  dual-wavelength PPG recorded simultaneously with arterial blood-gas co-oximetry across
  a controlled desaturation — an IRB-approved human study, not a download. Every public
  PPG dataset the author is aware of (BIDMC, MIMIC, PPG-DaLiA, Pulse Transit Time)
  provides a single already-processed channel, which cannot form a ratio at all. SR-06
  therefore stays **partial**: the measurement chain up to R is verified, the constants
  that turn R into a percentage are assumed (D-34).
- **The MAX30102 driver has never met a MAX30102.** Its logic is verified against a
  simulated part; bus timing, pull-ups, LED currents for a real finger and ambient-light
  rejection are not.
- **The PPG in the simulator is synthetic**, generated at the replayed record's own heart
  rate. MIT-BIH carries no photoplethysmogram.

---

## Phase 4 — Server, persistence, history and central station

**Delivered:** `server/src/storage/` — one contract with three implementations (`NullStore`,
`MemoryStore`, `PostgresStore`), the schema, and `docker-compose.yml`. `server/src/history.js`
— session listing, detail, vitals and CSV export. Alarm acknowledgement with a
severity-dependent re-alarm timer. `server/public/central.html` — a multi-bed view
ordered by alarm severity, with HR and SpO₂ trends and inline acknowledge.

**Verified against a running PostgreSQL:**

| Check | Result |
|---|---|
| Storage contract, both implementations | **28/28**, and stable across repeated runs on an accumulating database |
| Whole server suite, with and without a database | **101/101** |
| Replayed patient → recorded session | 1 Hz vitals, `LEADS_OFF` raised and cleared after exactly **10.0 s** — the injected window |
| CSV export | unknown measurements are blank fields, never 0 |
| Acknowledge, dashboard → engine → database | `acknowledged_by = central` persisted; chip shows `silenced 56s` counting down to re-alarm |
| Three simultaneous beds | alarming bed sorts to the top, numerics withdrawn to `--`, trends break across the gap rather than drawing through it |

**Decisions:** D-42 store 1 Hz numerics and alarm episodes, not the waveform · D-43 the
database is optional and its failure degrades to "no history" · D-44 acknowledgement is a
bounded, per-episode silence · D-45 storage tests must not assume an empty store ·
D-46 the simulator warns when replay speed defeats a wall-clock debounce · D-47 the silence
during an interruption is not charged to the patient · D-48 a bed stays visible when its
device disappears.

**Left open:**
- **Trends are accumulated in the browser from the live stream**, so a freshly opened
  central station starts with an empty five-minute window rather than back-filling from
  `/api/sessions/:id/vitals`. The endpoint exists; the page does not call it yet.
- **No authentication.** Anyone who can reach the port can view every bed and acknowledge
  any alarm. Acceptable for a laptop demo, not for a ward.
- **The waveform is not on the central station** — trends only. Real central stations show
  a compressed strip per bed.

---

## Phase 5 — Integration, fault injection and CI

**Delivered:** `sim/harness.py` (the real server as a subprocess, a device speaking
protocol v2 with the acquisition model in the signal path, a dashboard recording arrival
times), `sim/scenarios.py` (eight fault-injection scenarios), `sim/run_scenarios.py`.
`dsp/requirements.py` — the requirements as data, with each naming the evidence that
decides its status — and `dsp/generate_requirements.py`, which renders
`docs/requirements.md` from that model plus measured results. `.github/workflows/ci.yml`,
six jobs.

**Verified — every requirement, measured against the running system:**

| Requirement | Measured |
|---|---|
| SR-01 heart rate ±5 bpm | worst 2.18 bpm over 22,459 beats |
| SR-02 latency < 500 ms | median **0.5 ms**, p95 2.1 ms, worst 7.5 ms (loopback; WiFi named separately) |
| SR-03 leads-off < 2 s | **1.51 s** |
| SR-04 clean-record sensitivity > 99 % | **99.98 %** |
| SR-05 no loss across a 30 s outage | **787/787 frames**, 0 gaps in `seq` |
| SR-06 SpO₂ ±3 % | 94.0 % against a true 94 % |
| SR-07 poor signal flagged, not guessed | **102/102 frames withheld**, 0 wrong rates |
| SR-08 thresholds defined once | boundary self-tests, plus the CI drift gate |

Plus three scenarios covering decisions rather than requirements: electrode reconnection
does not fabricate a rate (D-22), acknowledgement silences without hiding (D-44), and a
silent device is noticed and its numbers withdrawn.

**Decisions:** D-49 requirements are verified by fault injection against the running
system · D-50 `docs/requirements.md` is generated from a model plus evidence.

**What the matrix caught on its first run:** the shipped server reported **102 wrong heart
rates** on a swamped signal and never withheld one — an outright SR-07 violation. The
signal-quality metric existed only in Python; the server's own check was a coarse
heuristic and never consulted it. Every component test passed throughout. Fixed by
exposing the QRS energy concentration from the streaming detector, where the integrator
already runs, and gating on it in `session.js`.

**Left open:**
- **Latency is loopback.** It covers framing, validation, streaming DSP, alarm evaluation
  and fan-out, but not WiFi — a real 2.4 GHz link on a busy ward adds roughly 10–50 ms
  typical with retransmission spikes into the hundreds. The report says so rather than
  folding an unmeasured term into a measured number.
- **The `UNUSABLE_CONCENTRATION` threshold is defined twice**, in `dsp/quality.py` and
  `server/src/dsp/qrsDetector.js`, rather than being generated into `coefficients.js` like
  the filter constants. It is the one surviving hand-copied constant (D-12's exception).
- **CI has never run.** The workflow is written and its jobs are all runnable locally, but
  no push has exercised GitHub Actions.

---

## Phase 6 — Documentation, design and demo

**Delivered:** `docs/design.md` — seven Mermaid diagrams (system context, work-split block
diagram, three sequence diagrams, alarm state machine, verification structure), each
verified to render. `docs/safety.md` — IEC 60601-1/-2-27 awareness, the battery/isolation
argument, and an 11-row hazard analysis in the shape ISO 14971 asks for. `docs/decisions.md`
— all 50 decisions moved out of PLAN §10 and grouped by theme. `docs/demo.md` — a
three-minute walkthrough script. `docs/interview.md` — questions mapped to evidence.
Screenshots in `docs/img/`, and a README that now shows the system rather than describing it.

**Verified:** all seven diagrams render through `mermaid-cli`; the state machine was
checked visually because its syntax was the riskiest.

**The thing worth keeping from the hazard analysis:** of eleven hazards, the one this
project is actually about is **H-2, a wrong vital sign is believed**. Seven of the fifty
decisions exist because of it, and every one was forced by an observed failure rather than
anticipated — which says something about how this class of hazard is found.

**Left open:**
- **No demo video.** The script and screenshots exist; recording needs screen capture. The
  GIF recorder in the browser tooling captures per-action and is built for click-flows,
  not a continuously scrolling waveform — it produced two usable frames and was the wrong
  instrument.
- **Résumé update** — outside this repository.
- Everything previously recorded as open still is: no hardware, the SpO₂ calibration curve
  unvalidated, no authentication, CI never actually executed on GitHub.

---

## Post-completion fix — the gate that silenced a tachycardia (D-51)

A person following TESTING.md found that simulating a 130 bpm patient never fired the
TACHYCARDIA alarm. The SR-07 quality gate's concentration statistic is rate-confounded:
above ~125 bpm the QRS bumps crowd together and a clean fast rhythm scores as
"unreadable", so the monitor withheld the rate of exactly the rhythm it exists to alarm
on. Every automated layer had missed it because everything was validated at ≤ 104 bpm.

Fix: withhold only when concentration AND the detector's own signal-to-noise estimate
(spki/npki, already tracked) are both low. Verified: clean 130/150 bpm now report,
TACHYCARDIA fires end to end, all 8 scenarios still pass, and record 108's gated PPV
stays a large improvement (90.95 → 96.52 %). Known residual: a clean rhythm near 180 bpm
is still withheld, and the offline Python mirror is conservative above ~145 bpm — both
recorded, not hidden.
