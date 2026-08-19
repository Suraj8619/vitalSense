# VitalSense — team decomposition, remaining build plan, and interview mapping

> Written 2026-08-19, against commit `5858a1f` (Phase 4c complete).
> Companion to [PLAN.md](../PLAN.md) (the source of truth for *what* gets built) and
> [BUILD_LOG.md](../BUILD_LOG.md) (the journal of *how* it got built).
>
> This document answers three questions: what state the project is actually in, how
> the remaining work would be split across a three-person team and why the seams sit
> where they do, and what the finished commit history should look like. The team split
> is a decomposition exercise and an interview artefact — the repository stays
> single-author (see §4, "One honesty note").

---

## 1. What VitalSense actually is right now

A multi-parameter patient monitor built **sim-first** for a Philips healthcare-electronics
campus role. ~5,000 lines across Python DSP, a JS streaming port, a Node ingest server,
and a canvas dashboard. Six commits, one per work session, each paired with a BUILD_LOG
entry and decision-log rows (D-01…D-23). Server suite: 43/43 passing.

### Built and verified

| Layer | State | Evidence |
|---|---|---|
| Python DSP (filters, Pan-Tompkins, vitals) | done, 81 self-checks | Se 98.73 / PPV 98.64 on 22,459 beats |
| Coefficient generation → C header + JS module | done, single source of truth | `dsp/export_coefficients.py` (D-12) |
| JS streaming DSP (causal port) | done, scored on same records | Se 98.69 / PPV 98.63 (D-13) |
| Wire protocol v1 | specified + total validation | `docs/protocol.md`, `server/src/protocol.js` |
| Ingest server, sessions, alarm engine | done, 43 tests | leads-off 1.5 s, SpO₂ at 10 s sustain |
| Dashboard (canvas, ECG paper grid, alarms) | done | verified against live replay |
| MIT-BIH replay simulator + fault scenarios | done | `sim/replay.py` |

### Not built — the raw material for the team split

- **`firmware/src/main.cpp` does not exist.** The firmware directory holds one generated
  header. Phases 2 and 3 are entirely open.
- **SpO₂ has no algorithm at all.** `spo2` is passed through from the frame untouched.
  For a "multi-parameter" monitor this is the biggest gap — SR-06 is unverifiable today.
- **No signal-quality metric.** SR-07 is `partial`. This is the fix `docs/verification.md`
  itself names as the right answer for records 108 and 203.
- No persistence, no docker-compose, no CI, no session history/export, no alarm acknowledge.
- No MBSE diagrams, no `docs/img/` content, no `docs/decisions.md`.
- **README drift:** it still promises a Next.js dashboard and Postgres, and says
  "(server + replay instructions added in Phase 4)". An interviewer opens README first.

### Blocking issue for the working agreement

The next unchecked box in PLAN.md is *Phase 2 — Hardware bring-up*, which cannot be done
without hardware. The plan needs an amendment before any more work, or every session
violates rule 1 of the working agreement.

---

## 2. The three-developer split

Split along **the seams that are already versioned contracts** — because those seams
already exist in this repo, and no lane can silently break another without a contract
file appearing in the diff.

The three lanes also map onto how a real medical-device team is organised: acquisition /
AFE, algorithms & V&V, platform & clinical application.

### Dev A — Edge & Acquisition
*Analog front-end, firmware, device-side reliability*

| | |
|---|---|
| **Owns** | `firmware/`, `sim/spice/`, `sim/afe_model.py`, the device half of `docs/protocol.md` |
| **Requirements** | SR-03 (device half), SR-05, SR-06 (sensor half) |
| **Deliverables** | LTspice AD8232 model + Bode plots; ADC/electrode model (mV → 12-bit counts); FreeRTOS firmware (timer-ISR sampler, ring buffer, fixed-point notch, leads-off, WebSocket client, store-and-forward with backfill); Wokwi ESP32 image; **host-native build of the firmware DSP** |
| **No-hardware substitute** | Firmware C compiles for host and is fed MIT-BIH through the ADC model; Wokwi runs the full image with WiFi |
| **Doesn't touch** | Server internals, dashboard, Python DSP |

The load-bearing idea in this lane: **compile the firmware's fixed-point notch for the
host and score it against the Python design on the same records.** That closes the loop
three ways — Python float64 ↔ JS float64 causal ↔ C fixed-point — all scored on the same
data. No board required, and it is a stronger claim than most people with hardware can make.

### Dev B — Algorithms & Verification
*DSP, V&V, MBSE, automation*

| | |
|---|---|
| **Owns** | `dsp/`, `docs/requirements.md`, `docs/verification.md`, CI, `server/tools/` |
| **Requirements** | SR-01, SR-04, SR-07, SR-08 — and the traceability matrix as a whole |
| **Deliverables** | PPG simulator + **SpO₂ ratio-of-ratios** (the missing parameter); **signal-quality metric (SR-07)**; cross-language equivalence harness (now 3-way); scenario / fault-injection harness; traceability matrix *generated*, not written; CI that gates on requirement failure |
| **No-hardware substitute** | Synthetic PPG with known SpO₂ + BIDMC/MIMIC PPG for realism — same pattern as `synth.py` + MIT-BIH for ECG |
| **Doesn't touch** | Firmware, UI |

Dev B is also the **integration gate** — they own the traceability matrix, so nothing is
"done" until an SR row moves and CI stays green. That is a cleaner rule than rotating an
integration owner.

### Dev C — Platform & Clinical Application
*Server, data, UI, ops, demo*

| | |
|---|---|
| **Owns** | `server/src/` (except `server/src/dsp/`), `server/public/`, docker-compose, migrations |
| **Requirements** | SR-02, alarm behaviour and UX, SR-05 (server half) |
| **Deliverables** | Postgres persistence (sessions / frames / alarm events); session history + CSV export; alarm acknowledge and re-alarm policy; **central-station multi-patient view**; trend charts; latency instrumentation; docker-compose; the recorded demo |
| **No-hardware substitute** | Everything already runs off `sim/replay.py`; extend it to N simultaneous emulated devices |
| **Doesn't touch** | `server/src/dsp/` (generated + B's port), `dsp/`, `firmware/` |

### The contract files — changes here need a second reviewer

```
docs/protocol.md              A ↔ C     frame format, alarm codes
dsp/export_coefficients.py    B → A,C   generated: C header + JS module (never hand-edit)
docs/requirements.md          B owns    SR IDs are the shared vocabulary
data/fixtures/*.json          B → A,C   the records everyone scores against
```

The interview line: *"three people, three languages, one filter design — and the only way
they can drift is if someone hand-edits a generated file, which CI rejects."*

### Sequencing — where the parallelism actually is

```
Week 1  A: LTspice AFE + ADC model ──┐
        B: PPG synth + SpO2 algo     │  all three independent
        C: Postgres + history/export ┘
                                     │
Week 2  A: FreeRTOS + fixed-pt notch │  ◄── sync 1: B scores A's C notch
        B: signal-quality (SR-07)    │      vs the Python design
        C: central station + ack     │
                                     │
Week 3  A: WS client + backfill ─────┼── sync 2: A↔C reconnect/backfill
        B: scenario harness + CI     │      protocol change
        C: latency instrumentation   │
        ── integration week: fault-injection matrix, traceability, demo ──
```

Two real sync points, both on contract files. Everything else is genuinely parallel
because the seams are already there.

---

## 3. The no-hardware simulation strategy

Five layers, each replacing a specific thing a board would have given us:

| Layer | Replaces | Artifact | Verifies |
|---|---|---|---|
| **L0 — Analog** | AD8232 bench measurement | LTspice model, MIT-BIH as PWL source, Bode plots → `docs/img/` | Instrumentation-amp gain, analog band edges, RLD, component-value rationale |
| **L1 — Sensor/ADC** | The ESP32's actual ADC | `sim/afe_model.py`: 12-bit quantisation, 3.3 V range, ADC nonlinearity, sampling jitter, 50 Hz mains coupling, electrode half-cell DC drift, motion artefact, **leads-off railing** | The DSP is validated on ADC counts, not idealised mV |
| **L2 — Firmware-in-the-loop** | Board bring-up | Host build of `firmware/`, plus Wokwi for the full image | Fixed-point notch vs Python design; ring buffer; framing; FreeRTOS task timing |
| **L3 — System-in-the-loop** | Patient + cable + WiFi | `sim/replay.py` as a full device emulator, N devices | End-to-end vitals, alarms, latency |
| **L4 — Fault injection** | Pulling electrodes, killing the AP | Scenario matrix, one scenario per SR, results auto-written into `verification.md` | SR-02, SR-03, SR-05, SR-07 |

**L1 is the one that matters most.** Today every number in `verification.md` comes from
clean float mV. Re-running the whole validation *through* the ADC model and publishing the
delta ("quantisation cost 0.0x % sensitivity") is a result nobody expects from a student
project, and it is the honest version of "I don't have hardware."

### What still cannot be verified — say this unprompted

- Real electrode–skin impedance, motion-artefact morphology, sweat/gel degradation
- Actual AD8232 silicon behaviour vs its datasheet model
- WiFi RF behaviour under real interference (L3 is loopback — measure it, state the
  missing term, give it a budget)
- Patient isolation / leakage current — pure IEC 60601 territory, design-level awareness only

Naming that list precisely is worth more in the interview than a shaky live demo.

---

## 4. Commit history — what went into each commit, file by file

> This section originally *projected* ~45 commits forward from the six that existed. The
> project has since been built; the projection is replaced here by the **actual history**
> — 26 commits — each with its file manifest and what part of each file changed. Lane
> tags `[A]` acquisition / `[B]` algorithms & V&V / `[C]` platform show which lane the
> commit would have belonged to under the three-developer split (the history itself is
> single-author; see the honesty note at the end).
>
> Two files recur in almost every commit and are listed once here rather than repeated:
> **`BUILD_LOG.md`** (the session's journal entry is appended — same commit as the work,
> so the journal cannot be backfilled) and **`PLAN.md`** (boxes ticked, decision-log rows
> D-xx appended the moment the decision is made).

### Phase 0–1 — foundation and the validated DSP core

**`c384c76` — Phase 0: project scaffold, plan, build log, MIT-BIH fetch script**

| File | What went in |
|---|---|
| `PLAN.md`, `BUILD_LOG.md`, `CLAUDE.md`, `README.md` | New: the full phased plan, journal template, working agreement, README skeleton |
| `dsp/fetch_data.py` | New: reproducible PhysioNet downloader (data itself is gitignored) |
| `dsp/requirements.txt`, `.gitignore` | New: pinned deps; ignore data/, venv, node_modules |

**`6cb2ade` `[B]` — Phase 1: ECG filter chain**

| File | What went in |
|---|---|
| `dsp/filters.py` | New: `FilterConfig`, bandpass + notch design, zero-phase `clean_offline()`, causal `StreamingFilter`, `export_notch_header()`, 15-check self-test |
| `firmware/src/notch_coeffs.h` | New: **generated** C header (never hand-edited from here on) |

**`9a67331` `[B]` — Phase 1 complete: Pan-Tompkins, vitals, MIT-BIH validation**

| File | What went in |
|---|---|
| `dsp/pan_tompkins.py` | New: all detector stages, `QrsConfig`, flat-line gate (D-08), `compare_to_reference()` scorer |
| `dsp/synth.py` | New: synthetic ECG with known R peaks |
| `dsp/vitals.py` | New: RR/HR/HRV, median-of-rates (D-09), `RateLimits` |
| `dsp/validate.py` | New: runs the records, renders the report |
| `docs/requirements.md`, `docs/verification.md` | New: SR table; first **generated** results (D-11) |

### Phase 4a–4c — server and dashboard (built before the firmware, per the risk register)

**`cdeaa94` `[B→C]` — Phase 4a: wire protocol + streaming JS port, scored**

| File | What went in |
|---|---|
| `docs/protocol.md` | New: the device↔server contract — the seam the team split relies on |
| `dsp/export_coefficients.py` | New: ONE design now generates the C header **and** the JS module (D-12) |
| `server/src/dsp/biquad.js`, `qrsDetector.js`, `vitals.js` | New: the causal port |
| `server/src/dsp/coefficients.js` | New: **generated** |
| `server/tools/validateStreaming.js`, `dsp/export_fixture.py` | New: the port is scored, not trusted (D-13) |
| `server/test/dsp.test.js`, `vitals.test.js`, `helpers/syntheticEcg.js` | New: 23 unit tests |
| `dsp/filters.py` | Edited: export path only — design untouched |

**`0fed024` `[C]` — Phase 4b: ingest server, alarms, replay simulator**

| File | What went in |
|---|---|
| `server/src/protocol.js` | New: total frame validation |
| `server/src/alarms.js` | New: declarative rules + `AlarmEngine` (D-18, D-19) |
| `server/src/session.js` | New: per-device DSP state, beat anchoring (D-17), reap grace (D-20) |
| `server/src/index.js` | New: `/ingest`, `/stream`, health, watchdog |
| `sim/replay.py` | New: MIT-BIH → server at true 360 Hz, fault scenarios |
| `docs/protocol.md` | Edited: beats become absolute indices (the D-17 fix) |

**`5858a1f` `[C]` — Phase 4c: dashboard; fix fabricated heart rate**

| File | What went in |
|---|---|
| `server/public/index.html` | New: canvas ECG, numerics, alarm banner (D-21, D-23) |
| `server/src/dsp/qrsDetector.js` | Edited: `restart()` added — the 190 bpm fix (D-22) |
| `server/src/session.js` | Edited: 3 s settling window after reconnection |
| `server/src/index.js` | Edited: static serving with path-traversal rejection |
| `server/test/server.test.js` | Edited: regression test reproducing the exact 190 bpm scenario |

### The pivot — no hardware arrived

**`558a0cf` — Plan: team decomposition; phases re-cut as simulation (D-24, D-25)**

| File | What went in |
|---|---|
| `docs/team-plan.md` | New: this document |
| `PLAN.md` | Edited: §3 re-cut — Phases 2′/3′/3.5, hardware demoted to backlog |
| `README.md` | Rewritten: drift removed (promised Next.js/Postgres deleted, real run instructions added) |

**`e88462b` `[A+B]` — Phase 2′: AFE + ADC model; gain fix (D-26)**

| File | What went in |
|---|---|
| `sim/afe_model.py` | New: electrode → amplifier (E12 components) → rails → 12-bit ADC, 47 checks |
| `sim/plot_afe.py`, `docs/img/afe_bode.png`, `afe_chain.png` | New: the Bode evidence |
| `dsp/validate.py` | Edited: second scoring pass *through* the model; new report section |
| `docs/verification.md` | Regenerated: the "Through the acquisition chain" table |
| `README.md` | Edited: acquired-chain row added to the results table |

**`c559870` — docs: per-phase progress ledger**

| File | What went in |
|---|---|
| `docs/progress.md` | New: the per-phase record, backfilled for all done phases |
| `CLAUDE.md`, `PLAN.md` | Edited: rule 3b added — a phase isn't done until it has an entry |

### Phase 3′ — firmware, verified with no board

**`a71e690` `[A]` — Phases 3a–3c: portable core + host scoring (D-27)**

| File | What went in |
|---|---|
| `firmware/src/vs_notch.{h,c}` | New: Q14 fixed-point notch, priming, rounding accumulator |
| `firmware/src/vs_ringbuf.{h,c}` | New: 30 s store-and-forward, overwrite-oldest counted (D-27) |
| `firmware/test/Makefile`, `test_firmware.c`, `host_filter.c` | New: host build + 24 unit tests + stdin/stdout harness |
| `firmware/test/score_notch.py` | New: the C notch scored on MIT-BIH vs the Python design |
| `dsp/filters.py` | Edited: `export_notch_header()` now emits Q14 ints + refuses unstable quantised poles |
| `firmware/src/notch_coeffs.h` | Regenerated: gains `NOTCH_B_Q/NOTCH_A_Q` |
| `.gitignore` | Edited: `firmware/test/build/` (compiled binaries had slipped into the first draft of this commit — amended out) |

**`bf7480a` `[A]` — Phases 3d–3e: leads-off, frame builder, contract check (D-28)**

| File | What went in |
|---|---|
| `firmware/src/vs_leadsoff.{h,c}` | New: LO pins + rail cross-check, asymmetric 50/500 ms debounce |
| `firmware/src/vs_frame.{h,c}` | New: protocol JSON, no malloc, no float printf |
| `firmware/src/vs_calibration.h` | New: **generated** counts→mV scaling from the AFE model |
| `dsp/export_coefficients.py` | Edited: `export_calibration()` added |
| `firmware/test/host_device.c`, `validate_frames.mjs` | New: real frames piped through the server's own validator |
| `firmware/test/test_firmware.c`, `Makefile` | Edited: +23 checks; new binaries wired in |
| `server/package.json` | Edited: `validate:frames` script |

**`a302ccd` `[A]` — Phase 3f: ESP32 shell, Wokwi (D-29–31)**

| File | What went in |
|---|---|
| `firmware/src/main.cpp` | New: timer ISR, five FreeRTOS tasks, WiFi/WS client, serial transport variant |
| `firmware/platformio.ini` | New: pinned platform (D-30) + `wokwi` env (D-31) |
| `firmware/wokwi.toml`, `diagram.json` | New: simulator wiring |
| `firmware/include/vs_secrets.example.h` | New: template only — real secrets gitignored |
| `firmware/src/vs_*.h` | Edited: `extern "C"` guards (found by compiling for the target) |
| `firmware/README.md` | New: layout, host-verification commands, honest Wokwi status |

**`5b61f36` — docs: Wokwi diagram verified, run blocked on licence** — `firmware/README.md`, `docs/progress.md` edited: status corrected to exactly what was and wasn't executed.

### Phase 3.5 — SpO₂ and signal quality

**`572fbc8` `[B]` — Phase 3.5a: PPG + ratio-of-ratios (D-32–34)** — `dsp/ppg.py` new: synthesiser, estimator, red/IR agreement gate, 38 checks.

**`47ad732` `[B]` — Phase 3.5b: signal-quality metric (D-35)**

| File | What went in |
|---|---|
| `dsp/quality.py` | New: pSQI/basSQI/kurtosis + the concentration gate |
| `dsp/pan_tompkins.py` | Edited: `energy_concentration()` added |
| `dsp/validate.py` | Edited: gated third scoring pass + report section |
| `docs/requirements.md` | Edited: SR-07 → verified |

**`4921152` `[B]` — Phase 3.5c: BIDMC validation; sub-harmonic fix (D-36, D-37)**

| File | What went in |
|---|---|
| `dsp/fetch_ppg.py`, `dsp/validate_ppg.py` | New: dataset + scoring |
| `dsp/ppg.py` | Edited: `periodicity()` with shortest-lag rule; rate from autocorrelation, not peak count |

**`b8117b8` `[B→C]` — Phase 3.5d: JS port, protocol v2 (D-38, D-39)**

| File | What went in |
|---|---|
| `server/src/dsp/ppg.js` | New: the port |
| `server/tools/validatePpg.js`, `dsp/export_ppg_fixture.py` | New: scored — this exposed swapped fields in the *Python original* |
| `dsp/ppg.py` | Fixed: field order + keyword-only construction + regression check |
| `server/src/protocol.js` | Edited: v1 **and** v2 accepted; raw `ppg` block validated; `spo2Source` on vitals |
| `server/src/session.js` | Edited: PPG buffering + `#updateSpo2()` |
| `sim/replay.py` | Edited: speaks v2, pulse locked to the record's rate |
| `server/public/index.html` | Edited: provenance line under the SpO₂ tile |
| `dsp/export_coefficients.py`, `server/src/dsp/coefficients.js` | Edited/regenerated: PPG band + thresholds |

**`690d94a` `[A+B+C]` — follow-up: MAX30102 driver, cross-check (D-40, D-41)**

| File | What went in |
|---|---|
| `firmware/src/vs_max30102.{h,c}` | New: driver with injected I2C; part-ID refusal; 18-bit masking |
| `firmware/src/main.cpp` | Edited: `ppgTask` reads the sensor; frames carry raw red/IR |
| `firmware/src/vs_frame.{h,c}` | Edited: v2 `ppg` block emission |
| `server/src/session.js` | Edited: `#crossCheckRates()` — asymmetric (pulse deficit vs impossible) |
| `dsp/validate_ppg.py` | Edited: unstable-reference windows excluded and counted (D-40) |
| `firmware/test/test_firmware.c`, `Makefile` | Edited: +23 driver checks against a simulated part |

### Phase 4 — persistence, history, central station

**`a11c6b8` `[C]` — Phase 4d: PostgreSQL (D-42, D-43)**

| File | What went in |
|---|---|
| `server/src/storage/store.js` | New: the contract + `NullStore` + `MemoryStore` |
| `server/src/storage/postgresStore.js`, `schema.sql`, `index.js` | New: SQL binding, degrade-don't-die pool, factory |
| `server/src/session.js` | Edited: `#persist()` — 1 Hz rows, alarm-episode diffing |
| `server/src/index.js` | Edited: session create/end wiring, reap closes open episodes |
| `docker-compose.yml`, `server/Dockerfile`, `.dockerignore` | New: the entire infrastructure |
| `server/test/storage.test.js` | New: one contract suite, both implementations |

**`c886d94` `[C]` — Phases 4e–4f: history API, acknowledge (D-44)**

| File | What went in |
|---|---|
| `server/src/history.js` | New: `/api/sessions*`, CSV with blank-not-zero fields |
| `server/src/alarms.js` | Edited: `acknowledge()`, `REALARM_MS`, per-episode silences |
| `server/src/index.js` | Edited: `/api/` route; dashboard socket accepts exactly one message type |
| `server/test/history.test.js`, `acknowledge.test.js` | New |

**`436f066` `[C]` — Phase 4 verify vs real Postgres (D-45–47)**

| File | What went in |
|---|---|
| `server/src/storage/postgresStore.js`, `store.js` | Fixed: total ordering (`started_at DESC, id DESC`) |
| `server/src/session.js` | Fixed: the watchdog crash (`derived` in `tick()`) |
| `server/src/dsp/vitals.js` | Fixed: `RollingHeartRate.restart()` — interruption silence not charged to the patient |
| `sim/replay.py` | Edited: warns when `--speed` defeats a wall-clock debounce |
| `server/test/*.test.js` | Edited: tests stop assuming an empty store; watchdog regression test |

**`47f35dd` `[C]` — Phase 4g: central station (D-48)** — `server/public/central.html` new (alarm-ordered beds, gap-drawing trends, ACK with countdown, offline persistence); `index.html` edited (link).

### Phase 5–6 — verification machinery and documentation

**`e391d49` `[B]` — Phase 5: scenarios, generated requirements, CI (D-49, D-50)**

| File | What went in |
|---|---|
| `sim/harness.py` | New: real server subprocess + protocol-v2 device + timing dashboard |
| `sim/scenarios.py`, `run_scenarios.py` | New: 8 scenarios, JSON results, non-zero exit on failure |
| `dsp/requirements.py` | New: requirements as data, evidence named per requirement |
| `dsp/generate_requirements.py` | New: renders `docs/requirements.md`; status **computed** |
| `server/src/dsp/qrsDetector.js` | Edited: `energyConcentration` exposed — **the SR-07 fix the matrix forced** |
| `server/src/session.js` | Edited: the gate actually consulted |
| `.github/workflows/ci.yml` | New: six jobs incl. the generated-file drift gate |
| `dsp/validate.py` | Edited: emits `validation_results.json`; scenario section in the report |
| `docs/requirements.md` | Now **generated** — hand-written version replaced |

**`2bee535` — Phase 6: diagrams, safety, decision log, interview prep** — all new: `docs/design.md` (7 verified Mermaid diagrams), `docs/safety.md` (11-row hazard analysis), `docs/decisions.md` (50 decisions, themed), `docs/demo.md`, `docs/interview.md`, three screenshots in `docs/img/`; `README.md` edited (screenshots + doc index).

**`6bc3ecf` — docs: TESTING.md** — new, every command verified as written.

**`aa97ab3` — Fix D-51: the gate that silenced a tachycardia**

| File | What went in |
|---|---|
| `server/src/dsp/qrsDetector.js` | Edited: `snr` getter (spki/npki); `UNUSABLE_SNR` |
| `server/src/session.js` | Edited: withhold only on concentration **AND** SNR |
| `dsp/pan_tompkins.py`, `dsp/quality.py` | Edited: mirror in the reference; residuals documented |
| `server/test/ppg.test.js` | Edited: "a clean tachycardia is reported" regression test |
| `TESTING.md` | Fixed: Test 5 rebuilt around a persistent alarm; Part 4 triage table |
| `docs/walkthrough.md` | New: the plain-language project story |

### What makes this history read as real rather than manufactured

- **~30 % of commit messages carry a `fix:` clause naming what was actually wrong** — the fabricated 190 bpm, the swapped fields the port exposed, the watchdog crash, the gate that silenced a tachycardia. A history of nothing but clean feature commits is the tell that reality was squashed out of it.
- **`BUILD_LOG.md` changes in the same commit as the work**, so the journal cannot be backfilled.
- **Generated files only ever change in the same commit as their generator** (`notch_coeffs.h` with `filters.py`, `coefficients.js` with `export_coefficients.py`, `requirements.md` with its model). CI's drift gate enforces this going forward.
- Cross-lane commits exist where integration actually happened (`b8117b8` touches DSP, server and simulator because protocol v2 is one change with three ends).
- Docs commits land continuously, not in a heap at the end.

**The honesty note stands:** the history is single-author. The `[A]/[B]/[C]` tags show how the work *decomposes* along the contract seams of §2 — they are an interview answer about team structure, not a claim that a team existed.

## 5. Interview mapping

The delegation question is itself an interview question, and one campus candidates
usually fumble.

**"How would you split this across a team?"** → *"Along the interfaces that were already
versioned contracts — the wire protocol, the generated coefficient files, and the
requirement IDs. Three lanes: acquisition, algorithms and V&V, platform. The V&V owner is
the integration gate, because 'done' means an SR row moved and CI is still green."* That
is a systems-engineering answer, not a software answer, and it is what the JD screens for.

| Question | Lane | Evidence in the repo |
|---|---|---|
| "Why 360 Hz? IIR vs FIR on an MCU?" | A/B | D-02, `dsp/filters.py`, BUILD_LOG 002 |
| "How do you keep three implementations of one filter consistent?" | A/B | D-12, `dsp/export_coefficients.py`, the 3-way score |
| "What happens when an electrode falls off — and when it comes back?" | all | **D-22, BUILD_LOG 006** — the strongest answer |
| "Your detector does badly on record 203. Why didn't you fix it?" | B | D-10 → after Phase 4n: "we did — by flagging, not tuning" |
| "How do you design against alarm fatigue?" | B/C | D-18, D-19, sustain times in `server/src/alarms.js` |
| "Your requirement said 2 s. How did you verify it?" | B | BUILD_LOG 005 #3 — debounce inside the budget |
| "You have no hardware. What did you actually verify?" | all | §3 L0–L4, plus the explicit unverified list |
| "Tell me about the hardest bug you found." | all | 190 bpm fabricated tachycardia with signal quality GOOD |

**Rehearse the no-hardware question hardest**, because it will come and it is where
candidates get defensive. The winning shape: *"Everything above the ADC pins is verified
against cardiologist-annotated data, including the firmware's own fixed-point filter
running on the host. What isn't verified is the analog domain and electrode physics —
here is the specific list, and here is what a board would change."* Confident about what
is known, precise about what is not. That is the same instinct as SR-07, and it can be
said out loud: **the project's thesis is that a system should know when not to make a
claim, and that applies to the engineer presenting it too.**

---

## 6. Immediate next actions

1. **Fix README drift** — it currently promises things that do not exist.
2. **Amend PLAN.md**: re-cut Phases 2/3 as simulated acquisition, add D-24 recording *why*
   (hardware unavailable → sim path promoted per the risk register). This unblocks rule 1
   of the working agreement.
3. Start Phase 2a/2b — the AFE + ADC model, since everything else in lane A depends on it.
