# VitalSense, explained from the beginning

*A walkthrough of the whole project in plain language — what problem each piece solves,
why it was built, and what was learned building it. Written as if you have never worked
with any of these technologies.*

---

## The problem the whole project exists for

In a hospital, a machine next to the bed watches a patient's vital signs: how fast the
heart is beating, how much oxygen is in the blood, body temperature. Nurses cannot stand
next to every bed all night, so the machine must do two jobs:

1. **Show the numbers**, continuously and correctly.
2. **Raise an alarm** when something goes wrong.

That sounds simple. It is not, and the reason it is not became the theme of this entire
project:

> **A monitor that shows a wrong number is more dangerous than one that shows nothing.**

If the screen says "heart rate 75" and the real rate is 190, a nurse walks past a patient
in trouble. If the screen says "I can't read the signal right now", the nurse comes to
check. The second machine is *less impressive* and *safer*. Almost every hard decision in
this project came down to that trade.

I built the whole thing — the electronics design, the software that runs on a chip, the
maths that finds heartbeats, the server, the screens, and the testing that proves it
works — with one constraint: **I had no physical hardware.** How to make honest claims
about a device that doesn't physically exist yet became the second theme.

---

## Step 1 — Before writing any code: how will we know it works?

**The problem:** if I build a heartbeat detector and point it at my own chest, and it
says "72 beats per minute" — is that right? I don't actually know my heart rate to the
beat. A live person can't tell you which beats your machine missed.

**The solution:** doctors already solved this. There is a famous public dataset called
the **MIT-BIH Arrhythmia Database**: about 30 minutes each of real hospital patients'
heart signals, recorded in the 1980s, where **cardiologists marked every single
heartbeat by hand** — including the abnormal ones. 22,459 beats, each with an answer key.

So the build order was decided by the testing method, not the other way around:

1. First, build the maths and test it against the answer key.
2. Only then worry about hardware.

**Why this matters:** it means every claim in this project is a *measured number*, not an
impression. "The detector finds 98.73 % of beats" is checkable by anyone who runs one
command.

**A choice made here that pays off everywhere:** the recordings were sampled 360 times
per second, so I designed the entire system to run at 360 samples per second too. Same
rate everywhere = the test results apply directly to the real device.

---

## Step 2 — Cleaning the signal

**The problem:** the electrical signal from a heart is *tiny* — about one thousandth of a
volt. Riding on top of it is junk that is much bigger than the signal itself:

- **Mains hum.** Every wall socket and light fixture radiates a 50 Hz buzz, and the human
  body picks it up like an antenna.
- **Baseline wander.** Breathing slowly rocks the whole signal up and down.
- **Muscle noise.** Every muscle twitch is also an electrical signal.

**The solution: filters.** A filter is a piece of maths that lets some frequencies
through and blocks others — like a bouncer with a guest list. Heartbeat shapes live
roughly between 0.5 and 40 "wiggles per second" (Hz), so:

- a **bandpass filter** keeps 0.5–40 Hz and discards the rest (kills the wander and most
  muscle noise),
- a **notch filter** surgically removes exactly 50 Hz (kills the mains hum).

**A subtlety that shaped the whole system:** there are two ways to run a filter. Offline,
with the whole recording available, you can filter *perfectly* with no time distortion.
Live, you only know the past — the filter necessarily delays and slightly distorts the
signal. I built both modes from day one, because the test bench gets the perfect one and
the real device can only ever have the live one — and pretending otherwise would make the
test results a lie.

---

## Step 3 — Finding the heartbeats

**The problem:** in the cleaned signal, where exactly is each beat?

**The solution:** a classic 1985 algorithm called **Pan-Tompkins**, chosen over fancier
machine-learning options because every step of it can be explained and defended:

1. Filter to 5–15 Hz — the band where the sharp spike of a heartbeat (the "QRS") has
   most of its energy. *(Note: this is a different band from the display filter. What is
   best for a computer to detect is not what is best for a doctor to look at, so the
   signal is split into two paths. Real monitors do the same.)*
2. Take the slope — beats are the steepest thing in the signal.
3. Square it — makes big slopes much bigger than small ones, and everything positive.
4. Smooth over 150 ms — one bump per beat.
5. **Adaptive threshold** — the clever part. Instead of a fixed rule like "anything above
   X is a beat", the threshold *learns* the patient's own signal level and keeps
   adjusting. A quiet signal gets a low bar, a strong one gets a high bar.

**Result against the cardiologists' answer key:** 98.73 % of beats found, 98.64 % of
detections were real beats. On the clean recordings: 99.98 %.

**The first frightening discovery:** I fed the detector a completely *flat* signal — what
it would see if the cable were unplugged — and it confidently reported a heart rate.
Why? An adaptive threshold adapts to *whatever it is given*. Given nothing but
microscopic numerical fuzz, it lowered its bar all the way and found "beats" in the fuzz.

A machine that invents a heartbeat for an unplugged cable is the nightmare scenario. The
fix is a hard floor: below a minimum signal size, refuse to detect anything. Simple —
but the *lesson* wasn't: **adaptive algorithms don't fail by going quiet. They fail by
producing a plausible answer from garbage.** This exact failure returned twice more in
different disguises.

**And an honesty decision:** two of the ten test recordings are famously messy, and the
detector does noticeably worse on them. I could have tuned the settings per-recording
until the table looked perfect. That would make the numbers better and the detector
worse — tuning to your test data means it only works on your test data. The bad numbers
are published as-is, with an explanation.

---

## Step 4 — Turning beats into a heart rate (more careful than it sounds)

**The problem:** you have beat times. The rate is just beats-per-minute... right?

**The trap:** suppose one noise blip gets counted as a beat. If you average the
intervals, that one blip yanks the displayed rate — maybe enough to ring a false alarm.

**The solution:** use the **median** (the middle value) of the recent intervals instead
of the average. One or two bad intervals then change *nothing*, because the middle of the
pack is still a real interval. Plus a sanity rule: intervals that imply a rate above 240
or below 20 are physically implausible and are discarded.

This is a tiny decision, but it's the pattern of the whole project in miniature: **assume
some inputs will be wrong, and design so a few wrong inputs can't move what the nurse
sees.**

---

## Step 5 — The hardware problem, and the unusual answer

**The problem:** the plan called for a real circuit — a sensor chip that amplifies the
heart's microvolts, wired to a small computer (an ESP32, a ₹400 WiFi chip) that samples
it 360 times a second. The parts never arrived.

**The obvious move** is to build everything else and say "hardware pending". **The better
move**, it turned out, was to *build the hardware in software*: a simulation of the whole
electrical chain, component by component —

- the amplifier with its real gain and its real limits,
- the resistors and capacitors, snapped to values you can actually buy,
- the analog filters and the distortion they cause,
- the chip's analog-to-digital converter, with its real coarseness (it reports the
  voltage as one of only 4,096 steps), its errors, its noise, its timing jitter,
- even the chemistry: an electrode stuck to skin generates its own small battery voltage
  that drifts as the gel dries.

Then I re-ran the *entire* 22,459-beat validation **through this simulated hardware** —
so the maths is tested on the mangled, quantised, noisy signal a real device would
produce, not on pristine recording data.

**The payoff came immediately.** The amplifier gain in my design was copied from the
chip-maker's example circuit: 1100×. Run against the real recordings, the simulation
showed the amplifier *clipping* — two of the ten patients have unusually large
heartbeats, bigger than the example circuit was ever designed for, and the tops of their
beats were getting sliced off. The medical standard for ECG monitors says a device must
handle ±5 mV; the example circuit handled ±1.5. I changed the gain to 330×.

**Why this matters:** a physical prototype would *not* have caught this — I would have
tested it on myself, and my heartbeats are ordinary. The simulation tested against 10
different patients including the unusual ones. Sometimes the model is a *better* test
than the thing itself.

---

## Step 6 — Writing the chip's software so it can be tested without the chip

**The problem:** software for a chip normally runs *only* on that chip. No chip, no way
to run it — so how could any of it be verified?

**The solution:** split the code in two:

- **The core** — the filter, the memory buffer, the sensor decoding, the message
  formatting — written in plain, portable C with zero chip-specific commands. This
  compiles and runs on my laptop.
- **The shell** — the ~300 lines that genuinely need the chip (pins, timers, WiFi) —
  kept separate.

The core is then tested savagely on the laptop: 74 automated checks, and — the good part
— **the chip's actual filter code is scored against the same 22,459-beat answer key** as
everything else. It agrees with the reference maths to within half of one ADC step.

**Two chip-specific choices worth explaining like a human:**

- *The chip's filter uses whole numbers, not decimals.* Decimal maths on a small chip can
  take a variable amount of time; whole-number maths takes the same time every time, and
  when you must sample exactly 360 times a second, "the same time every time" is the
  feature. The cost: you must be extremely careful about rounding — I found a bug where
  always rounding downward made the signal drift like a slow leak, invisible for minutes.
- *Never sample "when the program gets around to it".* A hardware timer fires 360 times a
  second like a metronome and the samples are taken on its tick. If sampling timing
  wobbles, the maths downstream (which assumes evenly spaced samples) quietly degrades.

**And the buffer:** the chip keeps the last 30 seconds of signal in memory. If WiFi
drops, nothing is lost — on reconnect it sends the backlog. Every message carries a
sequence number, so if anything *is* ever lost, the server can tell exactly what and how
much. Silent loss is not allowed to exist.

---

## Step 7 — The server: where the thinking happens

**The problem:** should the smart maths live on the chip at the bedside, or on a server?

**The decision — thin edge, smart server:** the chip does only what *must* happen at the
bedside (steady sampling, basic cleanup, buffering). Everything intelligent — beat
detection, rate calculation, alarm logic — lives on a server. Real hospital systems make
the same split. The reason is testing: server code can be regression-tested against the
answer-key recordings every single day; code buried in a chip at a bedside cannot.

**The catch:** the beat detector was written in Python (the language of the test bench),
but the server runs JavaScript. Two copies of the same maths in two languages *will*
drift apart silently — unless you force them not to. Two defences:

1. The filter's magic numbers are **generated** — one Python script writes them into the
   C code and the JavaScript. Nobody ever types them by hand, and an automated check
   fails the build if a generated file has been hand-edited.
2. The JavaScript copy is **scored against the same answer key** as the Python. Not
   "should be the same" — measured to be the same.

That second habit — *never trust a port, score it* — later found a genuine bug in the
**Python original**: a bookkeeping slip meant one value was reported in the wrong field.
Every Python test passed, because the tests shared the bookkeeping. Only a second,
independent implementation could see it. Two ways of computing one thing beats double-
checking one way, every time it was tried in this project.

---

## Step 8 — The screens

**The problem:** the numbers exist on a server. A nurse needs to see them.

Two screens, both deliberately boring technology (a single web page each, no frameworks),
because a demo that can't fail to start matters more than impressive plumbing:

**The bedside view** — scrolling heartbeat trace on the classic green-on-black, big
numbers for heart rate / oxygen / temperature, alarm banner.

**The central station** — one tile per patient, the whole ward on one screen. Three
design rules, each of which is really a safety rule:

- **Tiles sort by who is in the most trouble**, not by bed number.
- **A missing measurement shows as `--`, never as the last value.** A number on a
  monitor is a claim about *right now*. Ten-second-old data shown as current is a lie
  with a confident face.
- **When a patient's monitor dies, the tile does not vanish.** It turns grey and says
  "offline". A vanished tile looks *identical to a healthy ward* — the screen would look
  calmest at the exact moment it should scream.

---

## Step 9 — Alarms, and the science of not crying wolf

**The problem:** the naive rule — "beep whenever a number crosses a line" — creates a
monitor that beeps constantly. Real hospitals have a name for what happens next: **alarm
fatigue**. Nurses tune out the noise, and then miss the alarm that mattered. False alarms
are not an annoyance; they are the *mechanism* by which real alarms get ignored.

**The design, rule by rule:**

- **Alarms need the condition to hold for a while.** A single fast reading does nothing;
  a fast rate sustained for 10 seconds alarms.
- **Redundant alarms are suppressed.** An unplugged cable triggers "electrodes off". It
  does *not* also trigger "no heartbeat" — the machine can't see a heartbeat *because
  the cable is off*, and burying the actionable alarm under derived ones helps no one.
- **Silencing an alarm is a pause, not a delete.** When a nurse acknowledges an alarm,
  it goes quiet for a bounded time (60 s for the dangerous ones) and **comes back if the
  problem is still there** — because the patient hasn't changed, only the nurse's
  awareness has. And if the problem goes away and returns an hour later, that's a new
  event, not covered by an old acknowledgement.
- **Even the timing needed care.** The requirement said "alarm within 2 seconds of an
  electrode falling off". My first design waited 2 seconds *to be sure* before alarming —
  which means the alarm can never arrive within 2 seconds. The wait has to fit *inside*
  the deadline, with room to spare for everything else. Obvious after; measured, not
  before.

---

## Step 10 — The worst bug, and what it taught

This is the story to remember from the whole project.

**What happened:** an electrode is unplugged, then plugged back in. The monitor shows
**190 bpm — "tachycardia" — signal quality GOOD.** The patient is fine. Every safety
check passed.

**Why:** plugging a cable back in causes a big electrical *thump*. That thump rings
through the filters like a struck bell, and while the adaptive threshold is still
calibrated to the silence, the ringing looks like a burst of fast beats. And here is the
awful part — every safety check asked some version of *"do these beats agree with each
other?"* The fake beats **did** agree with each other. They were regular, plausible,
mutually consistent — because they all came from the same ringing.

> **A self-consistent artefact walks straight through every consistency check.**

**The fix** wasn't a smarter check — it was *context*. The system already knew the
electrodes had just been reconnected; it just wasn't using that fact. Now, on
reconnection, it throws away everything the filters and thresholds have learned and
refuses to display a rate for 3 seconds while things settle. Real bedside monitors blank
the number in this exact situation, for this exact reason.

**Proof it was the right lesson:** the same failure appeared later, independently, in the
blood-oxygen channel. A shaking finger produced a *self-consistent* wobble that read as
"oxygen 87 %" (true: 95 %) — quality "good". The consistency checks passed again. The fix
was again physical context, not statistics: oxygen is measured with two colours of light
through the same finger, and if the finger is *pulsing*, both colours must see the same
pulse shape. An artefact shakes them differently. Check that, and the false reading is
refused.

Once is a bug. Twice is a law of nature: **when the numbers can lie consistently, ask a
question about physics instead.**

---

## Step 11 — Blood oxygen, and the limits of honesty

**How measuring oxygen with light works** (it's lovely): blood changes colour with
oxygen — bright red when oxygenated, darker when not. Shine red and infrared light
through a fingertip; oxygen-rich blood absorbs them differently. The trick that makes it
practical: only the *pulsing* part of each signal comes from fresh arterial blood, so by
comparing just the pulses you cancel out skin tone, finger thickness, and how bright your
lamp is. No per-person calibration needed. That ratio-of-pulses maps to a percentage.

**Built and verified:** the pulse-ratio measurement, tested on 432 synthetic recordings
where the true answer was known — zero wrong readings ever *reported* (bad signals were
refused instead), and pulse timing tested against real intensive-care recordings.

**Not verifiable, and said out loud:** the final step — converting the ratio into the
percentage — relies on a calibration curve that manufacturers obtain by having volunteers
breathe low-oxygen air while drawing arterial blood samples. No public dataset can
substitute for that. So the system reports the percentage *and* the raw ratio it came
from, and the documentation states plainly which half is verified and which half is an
assumption. Claiming otherwise would be exactly the confident-wrong-number failure this
project is against.

---

## Step 12 — Memory: what the system writes down

Everything so far is live. Hospitals also need *history* — what happened on the night
shift?

A database stores, once per second, the vitals and — more interesting — **alarm events
as episodes**: when each alarm started, when someone silenced it (and who), when it
ended. That turns "how long did that alarm ring before anyone responded?" from an
argument into a query.

Two design rules with the usual flavour:

- **The database is optional.** If it's absent or dies mid-shift, the monitor keeps
  monitoring and loses only history. A bedside monitor that goes dark because a *disk
  filled up* has its priorities backwards.
- **In the exported spreadsheet, an unknown value is an empty cell — never 0.** A chart
  someone later builds from that file must show a gap, not a heart rate of zero.

---

## Step 13 — The test that tested the tests

**The problem:** by now there were hundreds of passing unit tests. Here is the
uncomfortable question: *do passing component tests mean the requirements are met?*

**The answer turned out to be no**, and finding that out was Phase 5's whole value. I
built a harness that tests the system the way the world would: start the *real* server,
connect a *simulated device speaking the real protocol*, inject one fault — pull an
electrode, kill the network for 30 seconds, drench the signal in noise — and *measure
the outcome with a stopwatch*, one scenario per requirement.

**First run: one scenario failed.** The requirement "a signal too poor to trust must be
flagged, not reported" had been marked *verified* for weeks. The quality-checking maths
existed, passed all 22 of its own tests, was genuinely good — **and the server never
called it.** The check lived in the Python test bench; the shipped server had a crude
stand-in. On a swamped signal, the live system reported 102 wrong heart rates in a row,
each labelled "good".

Every part was correct. The *wiring between parts* was the bug, and no component test
can see wiring. It took a test of the assembled machine.

**The structural fix, beyond the bug:** requirements now live as data, and the
requirements document is *generated* — each requirement's status is computed from named
evidence. If the evidence doesn't exist, the document prints "not yet measured". It is
mechanically incapable of the mistake I made, which was marking something verified
because *related* tests passed.

**Final tally, every requirement measured on the running system:** electrode-off alarm
in 1.51 s (limit 2), zero frames lost across a 30 s outage, oxygen recovered exactly,
102/102 bad-signal frames withheld, no fabricated rate after reconnection.

---

## Step 14 — What testing by hand found that automation didn't

After all of that — 400+ automated checks, 8 end-to-end scenarios — a person following
the manual test guide found a bug the same day.

**The walkthrough said:** simulate a racing heart (130 bpm) and watch the tachycardia
alarm fire. **It never fired.** The signal-quality gate — the very fix from Step 13 —
judges "is this readable?" partly by how *spiky-vs-flat* the signal is. A fast heart
packs beats so close together that the flat parts between them shrink, the spikiness
score drops... and the gate concluded "unreadable" and withheld the rate. **The monitor
silenced itself for precisely the rhythm it most needs to report.** Every automated
test had used normal heart rates; the recordings top out around 104 bpm. Nothing had
ever asked the question at 130.

The fix: require a *second, independent* piece of evidence before withholding — the
detector's own signal-to-noise estimate, which stays high for a clean fast rhythm and
collapses for real noise. Only when *both* say "junk" does the monitor stay quiet. (And
one residual is documented rather than hidden: at extreme rates near 180, the offline
checker still errs on the cautious side.)

**The meta-lesson closing the project:** every layer of testing caught what the layer
below couldn't. Unit tests caught arithmetic. Cross-language scoring caught a bug in the
reference itself. System scenarios caught missing wiring. And a *human following
instructions* caught the case every automated layer had silently agreed not to ask
about. None of them is sufficient. That's not a failure of any layer — that's why you
have all of them.

---

## What was measured, in the end

| Claim | Number | How it's known |
|---|---|---|
| Finds heartbeats | 98.73 % of 22,459 cardiologist-marked beats | automated comparison, report auto-generated |
| Heart rate accuracy | worst case 2.18 bpm off (limit: 5) | same |
| The chip's maths = the tested maths | agrees within half an ADC step over 6.5 M samples | chip code compiled on laptop, scored on same data |
| Electrode-off alarm | 1.51 s (limit: 2) | stopwatch on the running system |
| 30 s network outage | 0 of 787 messages lost | end-to-end scenario |
| Swamped signal | 102/102 rates withheld, 0 wrong | end-to-end scenario |
| Oxygen from raw light | exact on 432 known-answer tests; 0 wrong readings reported | synthetic ground truth |

And what is *not* claimed: no real hardware has been touched, the oxygen calibration
curve is an assumption, there's no login system, and the analog world (electrode gel,
radio interference, patient safety isolation) is modelled, not measured. Every one of
those is written down in the repository, because the project's one rule applies to its
author too:

> **Say what you know. Say how you know it. Say what you don't.**
