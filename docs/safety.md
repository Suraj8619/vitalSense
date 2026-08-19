# Safety notes

**This is a student project. It is not a medical device, has undergone no formal risk
management process, and must not be connected to a patient.** What follows is what a
person building this class of system should be able to say about it — the hazards it
would have to answer for, and which of them this design actually addresses.

Saying "designed with awareness of IEC 60601" is honest. Saying "compliant" would not be:
compliance is a process with an audit trail, a quality system and testing this project has
not done. The distinction matters more than the standard.

---

## 1. Electrical safety — the hazard that is not in the software

An ECG amplifier is a deliberate low-impedance path to the inside of a person. The
patient's chest is connected, through gel and metal, to electronics; the only thing
between them and mains is whatever isolation the design provides.

**IEC 60601-1** governs this. The parts that bear on a build like this one:

| Concern | What the standard is protecting against | This design |
|---|---|---|
| Patient leakage current | Current flowing through the patient to earth. Limits are tens of microamps — well below perception, because an electrode bypasses skin resistance and a current that would be harmless on a fingertip can be lethal near the heart | **Battery operation only.** No mains-referenced circuit exists while measuring |
| Mains isolation | A fault in a power supply appearing on the patient connection | Not applicable while on battery; a mains-powered version would need a medical-grade isolated supply and an isolation barrier in the signal path, neither of which this has |
| Applied-part classification | How much protection the patient-contacting parts need. An ECG is **Type CF** — the most stringent, because it is considered to have a direct cardiac connection | Not met. A real design needs an isolation amplifier or opto/transformer barrier between the front end and everything else |
| Defibrillation protection | Surviving a 5 kV defibrillator discharge across the electrodes without destroying the amplifier or injuring anyone | Not addressed. Requires clamp diodes, series resistors and a rated input network |

**The honest summary:** battery operation removes the single largest hazard by removing
mains from the system entirely. Everything else on that list is unaddressed, and a
mains-powered or clinically deployed version of this would be a substantially different
electrical design — not the same design with a different power supply.

This is also why the BOM's optional LiPo entry is not a convenience item. Running from a
battery is the safety argument.

---

## 2. ECG-specific requirements — IEC 60601-2-27

The particular standard for ECG monitoring equipment. Three of its expectations shaped
requirements in this project directly:

| Expectation | Where it appears here |
|---|---|
| The monitor must detect and alarm a lead-off condition | **SR-03**, measured at 1.51 s against a 2 s budget. The debounces on both sides were sized to fit *inside* that budget rather than on top of it (D-18, D-28) |
| The monitor must not display a heart rate it cannot substantiate | **SR-07**. The gate withholds the rate rather than reporting a wrong one, and its threshold is derived from SR-01 — so "too poor to report" and "cannot meet the accuracy requirement" are the same statement (D-35) |
| The input range must accommodate ±5 mV | **D-26**. The front-end gain was originally the datasheet's ~1100, which clips at ±1.5 mV; MIT-BIH records 203 and 208 exceed 4 mV. Gain is now 330, sized from this requirement |

Not addressed: pacemaker pulse detection and rejection, common-mode rejection verified to
the standard's test conditions, published accuracy under the standard's noise tests, and
the alarm priority scheme of IEC 60601-1-8.

---

## 3. Hazard analysis

Structured the way ISO 14971 asks: what could harm the patient, how, and what stands in
the way. **Severity** is the consequence if the mitigation fails, not the likelihood.

| # | Hazard | Cause | Severity | Mitigation | Evidence |
|---|---|---|---|---|---|
| H-1 | Electric shock | Mains fault reaching the patient connection | Catastrophic | Battery operation; no mains-referenced circuit during measurement | Design constraint, not tested |
| H-2 | **A wrong vital sign is believed** | Detector reports a confident number from artefact | Serious — treatment decisions follow displayed numbers | Flat-line gate (D-08), settling window after reconnection (D-22), quality gate derived from SR-01 (D-35), red/IR agreement for SpO₂ (D-32) | SR-07 scenario: 102/102 frames withheld, 0 wrong rates |
| H-3 | Deterioration missed because an alarm did not fire | Threshold, debounce or suppression logic wrong | Serious | Sustain times sized inside the requirement budget; suppression only where a more specific alarm already explains the fault (D-19) | SR-03 scenario: 1.51 s |
| H-4 | Deterioration missed through alarm fatigue | Too many false alarms train clinicians to ignore them | Serious | Sustained conditions rather than single readings; redundant alarms suppressed (D-19); asystole cannot fire before acquisition has settled (D-47) | Regression tests per failure mode |
| H-5 | Alarm silenced and forgotten | Acknowledgement treated as dismissal | Serious | Acknowledgement is a bounded silence that expires by severity; a recurrence is a new, unsilenced episode (D-44) | Scenario: alarm still listed while silenced |
| H-6 | A patient disappears from the display | Device drops off and its tile is removed | Serious — the screen looks calmer exactly when it should not | Beds persist marked offline; losing the server link clears nothing (D-48); silent device alarms (5.0 s measured) | Scenario: rate withdrawn, alarm raised |
| H-7 | Stale reading believed to be current | Last value left on screen after data stops | Serious | Numbers withdrawn to `--` after 3 s (D-23); absent sensor readings sent as `null`, never repeated (D-16) | Server tests; visible on the dashboard |
| H-8 | Patient data lost | Network outage during monitoring | Moderate | Device-side ring buffer with backfill; `seq` gaps make any loss detectable (D-15, D-27) | SR-05 scenario: 787/787 frames, 0 gaps |
| H-9 | Wrong patient's data shown | Device identity confused between sessions | Serious | One device ID per connection, enforced at the server; a connection carrying two IDs is refused | Server tests |
| H-10 | Skin injury or infection | Electrode gel, prolonged contact, reused pads | Minor | Not addressed — a materials and single-use question, outside this build | — |
| H-11 | Unauthorised access to patient data | No authentication on the dashboard or history API | Serious under any real deployment | **Not addressed.** Anyone who can reach the port can view every bed and acknowledge any alarm | Recorded as open in `docs/progress.md` |

**H-2 is the hazard this project is actually about.** Seven of the fifty decisions in
[decisions.md](decisions.md) exist because of it, and every one was forced by a specific
observed failure rather than anticipated in advance — which is itself worth noticing about
how this kind of hazard gets found.

---

## 4. What would have to change for this to be real

Roughly in order of how much work each is:

1. **Isolation.** A Type CF applied part with an isolation barrier, defibrillation
   protection, and leakage current measured rather than argued.
2. **A quality system.** Design history file, formal risk management under ISO 14971,
   software lifecycle under IEC 62304, and traceability from hazard to mitigation to
   verification — this project has the traceability shape but none of the process.
3. **Clinical validation.** The SpO₂ calibration curve in particular cannot be validated
   in simulation at all: it needs dual-wavelength hardware and arterial blood-gas
   co-oximetry across a controlled desaturation (D-34).
4. **Alarm system design to IEC 60601-1-8** — priority encoding, audible characteristics,
   and the alarm system verification the standard specifies.
5. **Security.** Authentication, transport encryption, and an audit trail for
   acknowledgements. Currently absent (H-11).
6. **Pacemaker handling**, arrhythmia classification, and the accuracy testing 60601-2-27
   specifies under its own noise conditions.

None of this is a reason not to have built it. It is the difference between a system that
demonstrates the engineering and a device that may touch a patient, and being able to
state that difference precisely is part of the engineering.
