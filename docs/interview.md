# Interview preparation

Not a script. A map from the questions this project invites to the evidence that answers
them, so nothing has to be recalled under pressure — every claim below has a file behind
it.

Two rules that matter more than any answer here:

1. **Never claim more than the evidence supports.** The strongest thing this project has
   is that its documentation refuses to overstate itself; talking past that would waste it.
2. **When you don't know, say so and say what you'd do.** That is the same behaviour
   SR-07 demands of the monitor, and you can say so out loud — it usually lands.

---

## The thirty-second version

> "It's a multi-parameter patient monitor — ECG, SpO₂, temperature. The DSP is validated
> against the MIT-BIH Arrhythmia Database: 22,459 cardiologist-annotated beats, 98.7 %
> sensitivity. It runs end to end with no hardware, because the acquisition front end and
> the ADC are modelled, so the pipeline is tested on ADC counts rather than idealised
> millivolts.
>
> The part I'd actually want to talk about is the verification. Requirements are data;
> the requirements document is generated, and a requirement whose evidence hasn't been
> produced says 'not yet measured'. That caught a real one — SR-07 was marked verified for
> two phases while the shipped server never called the signal-quality metric."

Then stop. Let them pick the thread.

---

## The five questions that carry the interview

### 1. "Tell me about the hardest bug."

**The 190 bpm.** [BUILD_LOG 006](../BUILD_LOG.md), D-22.

The monitor displayed **190 bpm, tachycardia, signal quality GOOD** — on a patient whose
electrodes had just been reattached. Reattaching is a step change into the amplifier; it
rings through the IIR filters while the adaptive thresholds are still tuned to the flat
line, producing detections about 0.3 s apart.

Why it got through every guard: the RR plausibility filter accepts 0.32 s (190 bpm is fast
but possible), the flat-line gate sees real amplitude, and the trustworthy-fraction check
passed *because nearly all the intervals agreed with each other*. **The artefact was
self-consistent, and consistency checks cannot catch a self-consistent artefact.**

What was missing wasn't a better threshold — it was context. The system knew the
electrodes had just been reconnected and wasn't using that fact. It now discards filter
and threshold state on reconnection and withholds the rate for 3 s.

The follow-through that makes it land: **the same bug reappeared independently in the
SpO₂ channel** (D-32). Motion artefact is periodic, so every beat is perturbed the same
way, and a 95 % patient was reported at 86.9 % marked "good". Same shape, same fix in
kind: stop asking whether the numbers agree with each other and ask a physical question —
do the two wavelengths see the same shape of pulsation, as they must if the thing moving
is arterial blood?

### 2. "You have no hardware. What did you actually verify?"

This will come. Do not get defensive.

> "Everything above the ADC pins, against cardiologist-annotated data — including the
> firmware's own fixed-point filter, compiled for the host and scored on the same
> recordings. What isn't verified is the analog domain and electrode physics."

Then name the list, because precision is the answer: real electrode–skin impedance and
motion morphology, actual AD8232 silicon versus its datasheet model, RF behaviour under
interference, patient isolation and leakage current.

And the turn: **modelling the ADC made one finding a bench test would have missed.** The
front-end gain was the datasheet's ~1100, which clips at ±1.5 mV. MIT-BIH records 203 and
208 exceed 4 mV, and IEC 60601-2-27 expects ±5 mV. Gain is now 330 (D-26). Nobody holding
electrodes in a lab would have produced a 4 mV QRS.

### 3. "How do you verify a requirement, as opposed to testing code?"

The strongest answer in the project. [requirements.md](requirements.md), D-49, D-50.

> "A unit test is written from inside a component and can only see that component. A
> requirement is a claim about the assembled system. Those are different claims, and I
> learned it the hard way."

SR-07 — *a signal too poor to trust is flagged, not reported* — was marked **verified**.
`dsp/quality.py` passed all 22 of its checks. The gated MIT-BIH table showed record 108's
positive predictivity rising 90.95 → 97.37 %. All true. **And the shipped server never
called any of it** — its quality check was still a coarse heuristic. The fault-injection
suite, which drives the real server over the real protocol, found it in one run: 102
consecutive wrong heart rates on a swamped signal.

The structural fix, not the vigilance fix: requirements are now data, status is *computed*
from named evidence, and a requirement whose evidence hasn't been produced renders as "not
yet measured". Under that scheme SR-07 could not have been marked verified by a Python
self-test, because its evidence is a scenario nobody had run.

### 4. "Your detector does badly on record 203. Why didn't you fix it?"

D-10, and it has a second act now.

> "Per-record threshold tuning would have made the table look excellent and the detector
> worse on anything unseen. So I published it — 93.6 % sensitivity on 203 — and named the
> real fix: a signal-quality metric, so the monitor can say 'too poor to trust' instead of
> quietly reporting a wrong number."

Then the second act, which is better than the first: **the obvious signal-quality metric
was dangerous.** The textbook version — QRS-band power share, baseline power, kurtosis —
produced *better* headline numbers: pooled PPV 99.64 %. Grouping the records by *why* they
are difficult showed why:

| | unusable % |
|---|---|
| 105, 108, 203 — genuine artefact | 9.3–11.7 |
| 106, 119 — arrhythmia, clean signal | **39.1–59.9** |

Kurtosis and the QRS-band share both fall for a *wide* QRS, so both read a ventricular
beat as a degraded signal. That monitor would go quiet exactly when a patient started
throwing ventricular beats — and its better numbers came from excluding the hardest,
most clinically important beats. **D-10's mistake in a new disguise.**

The shipped metric measures QRS energy concentration *after* the Pan-Tompkins integration,
where width is normalised away, and its threshold is derived from SR-01 rather than from a
percentile of the database. It scores worse on paper and it is the one I'd put on a
patient.

### 5. "How would you split this across a team?"

[team-plan.md](team-plan.md).

> "Along the interfaces that were already versioned contracts — the wire protocol, the
> generated coefficient files, and the requirement IDs. Three lanes: acquisition,
> algorithms and V&V, platform. The V&V owner is the integration gate, because 'done'
> already has a definition here: an SR row moved and CI is still green."

That's a systems answer, not a software answer, and it's what the role is screening for.

---

## Depth, by area

### Signal processing

| Question | Answer lives in |
|---|---|
| Why 360 Hz? | Matches MIT-BIH, so validation is directly comparable; QRS energy is ≤40 Hz and clinical monitors run 250–500 Hz (D-02) |
| Why bandpass 0.5–40 Hz *and* 5–15 Hz? | Detection wants QRS energy maximised; a clinician wants undistorted morphology. Real monitors separate them (D-07) |
| IIR vs FIR on a microcontroller? | IIR is far cheaper for this rolloff — but **one NaN is permanent in an IIR**, where an FIR flushes it out of the window. That asymmetry is a real argument for FIR in safety-critical acquisition (BUILD_LOG 004) |
| filtfilt vs lfilter? | Zero-phase needs the future; only the offline validation can use it. Group delay is the price of causality, measured at 5.56 ms |
| Pan-Tompkins, stage by stage? | 5–15 Hz band → derivative → square → 150 ms integration → adaptive dual threshold with search-back, refractory period and T-wave discrimination → fiducial snapping |
| Why adaptive thresholds? | A fixed threshold can't survive amplitude variation — **but an adaptive one rescales to whatever it's given, and invented 36 beats on constant DC.** Hence the absolute gate (D-08) |

### Pulse oximetry

- **How it works:** two wavelengths, AC/DC per channel removes everything that isn't
  arterial pulsation — skin tone, finger thickness, LED brightness — which is why no
  per-patient calibration is needed. Two tests pin exactly that: halving DC and dropping
  perfusion fivefold leave the reading unchanged.
- **What you can't validate:** the R→% curve is fitted per sensor against arterial
  blood-gas co-oximetry. No public dataset has raw dual-wavelength PPG with that
  reference, so the module reports **both** SpO₂ and the R it came from, and the docs say
  which half is evidence (D-34).
- **Two real-data bugs:** counting beats is a *biased* rate estimator (respiration-driven
  amplitude modulation drops small beats — 15–23 bpm low on BIDMC), and sub-harmonic lock
  reported exactly half the pulse rate on several records (D-36).

### Firmware and embedded

| Question | Answer |
|---|---|
| Why fixed point on a chip with an FPU? | The sampler runs from a timer ISR at 360 Hz sharing a core with WiFi; integer arithmetic has constant cost and a **bit-exact** result across compilers — which is what makes the host score meaningful |
| Why Q14, not Q15? | \|b1\| = 1.267 for this notch. A Q15 coefficient would wrap, and a wrapped coefficient in a recursive filter isn't a small error, it's a different filter |
| Why not sample in the ISR? | An ADC conversion in a handler holds off everything else, and `analogRead` isn't guaranteed IRAM-resident. The ISR gives a semaphore; a pinned high-priority task reads (D-29) |
| Which core, and why? | Core 1. Core 0 runs the WiFi driver's long interrupt-disabled sections — sharing puts radio scheduling into the sampling jitter of a medical measurement |
| How do you test firmware with no board? | The DSP is portable C99 with the I2C transport injected. Same translation units, compiled for the host, scored on MIT-BIH: **0.52 counts** worst case, agreeing with the correctly-rounded float result on 99.46 % of 6.5 M samples |
| Truncation vs rounding? | `>> 14` rounds toward −∞ — half an LSB every sample. In a *recursive* filter that's fed back and integrates into baseline drift. Test: constant input, 200,000 samples, output must not move |

### Systems, alarms and safety

- **Alarm fatigue:** sustained conditions, not single readings; redundant alarms suppressed
  where a more specific one explains the fault (D-19); asystole can't fire before
  acquisition has settled (D-47).
- **Acknowledging:** a *silence*, not a dismissal. Expires by severity; a recurrence is a
  new unsilenced episode; acknowledging an alarm that isn't sounding is refused (D-44).
- **"Your requirement said 2 s — how did you verify it?"** Measured at 1.51 s. And the
  first time it was measured it failed *by construction*: the debounce was itself 2000 ms,
  so the requirement could never be met. The debounce belongs **inside** the budget (D-18).
- **Safety:** [safety.md](safety.md). Battery operation removes the largest hazard by
  removing mains. Isolation, defibrillation protection and Type CF classification are
  **not** addressed, and the hazard table says so.

---

## Questions to ask them

Better than the generic ones, and they signal what you've actually been doing:

- "How does verification evidence flow here — is the traceability generated, or
  maintained?"
- "Where does the boundary sit between the bedside device and the central station in your
  architecture, and what drove it?"
- "When an algorithm and a clinician disagree about a reading, what does the system do?"
- "How much of your V&V runs without hardware, and what forced that split?"

---

## Things not to say

- ~~"It's IEC 60601 compliant."~~ → "Designed with awareness of; here's what isn't addressed."
- ~~"The latency is 0.5 ms."~~ → "0.5 ms on loopback. That's software only — the WiFi term isn't measured, and it's the term that matters for a 500 ms budget."
- ~~"It's fully tested."~~ → the test counts, then what isn't covered.
- ~~"I built this alone in a week."~~ → let the BUILD_LOG say it. 20 sessions, dated, with the bugs in them.
