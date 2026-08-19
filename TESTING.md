# How to test VitalSense

A step-by-step guide you can follow top to bottom. Every step says **what to type**, **what
you should see**, and **what it proves**.

You need three terminal windows. Keep them open the whole time.

- **Terminal A** — the server (leave it running)
- **Terminal B** — the patient simulator
- **Terminal C** — everything else

Everything below assumes you start from the project folder:

```bash
cd "/Users/abhijeetmishra/Developer/CS CORE/vitalsense"
```

---

# Part 0 — Check your setup (once)

**Terminal C:**

```bash
node --version
.venv/bin/python --version
ls data/mitdb/*.dat | wc -l
```

**You should see:** a node version (v22 or higher), a Python version (3.13), and the
number `10`.

If the last one says `0`, the patient recordings are missing. Download them:

```bash
.venv/bin/python dsp/fetch_data.py
```

---

# Part 1 — Start it

## Step 1.1 — Start the database (optional)

The system works fine without a database. You only need it for Part 4 (history).

**Terminal C:**

```bash
docker compose up -d db
```

**You should see:** `Container vitalsense-db-1  Started`

> Skip this if Docker isn't running. Everything except Part 4 still works.

## Step 1.2 — Start the server

**Terminal A:**

```bash
cd "/Users/abhijeetmishra/Developer/CS CORE/vitalsense/server"
DATABASE_URL=postgres://vitalsense:vitalsense@127.0.0.1:5432/vitalsense npm start
```

**You should see:**

```
[db] persistence enabled
VitalSense server listening on ws://127.0.0.1:8080
```

*(Without the database, just run `npm start`. It will say
`[db] no DATABASE_URL - running without history`. That is fine — everything except Part 4
still works.)*

**Leave this terminal alone from now on.**

## Step 1.3 — Start a patient

**Terminal B:**

```bash
cd "/Users/abhijeetmishra/Developer/CS CORE/vitalsense"
.venv/bin/python sim/replay.py --record 100 --speed 1 --loop --device-id bed-01
```

**You should see:**

```
replaying MIT-BIH record 100: 30.1 min, 360 Hz, 1.0x speed, scenario 'none' -> ws://127.0.0.1:8080/ingest
  PPG pulse locked to the record's heart rate: 74.1 bpm
  112 frames sent (0.2 min of signal)
```

...then the frame count keeps ticking up.

**What this is:** a real 30-minute ECG recording from a real patient, streamed at its true
360 samples per second, in exactly the format the ESP32 firmware would send.

## Step 1.4 — Open the screen

In your browser: **<http://127.0.0.1:8080>**

**You should see:** a green ECG scrolling right to left, a small triangle above each
heartbeat, and on the right:

- **HR ~75 bpm**
- **SpO₂ 97 %** with `derived from red/IR` under it
- **TEMP 36.8 °C**
- `SIGNAL GOOD` in the top right

**What this proves:** the whole chain works. The recording's official heart rate is
75.3 bpm, so 75 is correct.

---

# Part 2 — The five things worth testing

Do these in order. Each takes about a minute.

## Test 1 — Pull the electrode off

**Terminal B** — press `Ctrl+C`, then run:

```bash
.venv/bin/python sim/replay.py --record 100 --speed 1 --scenario leads-off --device-id bed-01
```

Now **watch the browser**. The electrode falls off 15 seconds in and goes back on at
25 seconds.

**You should see, in this order:**

| When | What happens |
|---|---|
| ~15 s | The green line goes flat and high |
| ~16.5 s | A red **LEADS_OFF** banner appears |
| same moment | **HR, SpO₂ and TEMP all change to `--`** |
| ~25 s | The line comes back... |
| ~25–28 s | ...**but the heart rate stays blank for about 3 more seconds** |
| ~28 s | The real rate comes back |

**What this proves — two things:**

1. **The numbers disappear.** The monitor had a perfectly good heart rate from one second
   ago. It throws it away, because a number on a monitor is a claim about *right now*.

2. **The 3-second blank after reconnection is the important one.** Plugging an electrode
   back in kicks the filters. That kick used to be counted as heartbeats, and the monitor
   displayed **190 bpm, "tachycardia", signal quality GOOD** — on a patient who was fine.
   Now it refuses to report anything until it has settled.

> **Important:** always use `--speed 1` for this test. Try `--speed 15` and the simulator
> will warn you that the fault becomes too short for the alarm to fire.

## Test 2 — Give it a signal too noisy to read

**Terminal B** — `Ctrl+C`, then:

```bash
.venv/bin/python sim/replay.py --synthetic --hr 72 --noise 0.9 --device-id bed-01 --speed 1
```

Wait about 15 seconds.

**You should see:** HR shows `--`, and the flag says `SIGNAL UNUSABLE`.

**What this proves:** the monitor would rather say *"I don't know"* than show a wrong
number. This is the single most important behaviour in the project. It was also broken
until very late — the check existed but the server never called it, and it took the
end-to-end tests in Part 5 to catch that.

## Test 3 — Fill a ward

**Terminal C:**

```bash
cd "/Users/abhijeetmishra/Developer/CS CORE/vitalsense"
.venv/bin/python sim/replay.py --record 100 --device-id bed-02 --speed 1 --loop &
.venv/bin/python sim/replay.py --record 100 --device-id bed-03 --speed 1 --loop &
```

Open **<http://127.0.0.1:8080/central.html>**

**You should see:** three tiles, one per bed, each with numbers and two small trend graphs
(heart rate and SpO₂).

## Test 4 — Unplug a patient

**Terminal C:**

```bash
pkill -f "device-id bed-03"
```

Now watch the central station **for the next 30 seconds** — this test has a timeline:

| Seconds after the kill | What you should see |
|---|---|
| 0–5 s | bed-03's numbers change to `--`, flag says "no data" |
| 5 s | A red **DEVICE_SILENT** label appears with an **ACK** button, and bed-03 **moves to the top** |
| 30 s | The alarm chip disappears and the tile is simply marked **offline** — but **the tile itself stays on screen** |

**Why it changes at 30 seconds:** the server holds a disconnected patient's session open
for 30 seconds, in case it's just a WiFi blip and the device comes back. After that it
closes the session — there is nothing left to alarm about, so the alarm goes too. The tile
stays for ten more minutes, marked offline, because a tile that silently vanished would
look exactly like a patient who is fine.

**What this proves:**

- Tiles are sorted by *who needs attention*, not by bed number.
- A dead monitor is loudly announced (the alarm), then honestly reported (offline) —
  never quietly removed.

## Test 5 — Silence an alarm and watch it come back

The alarm in Test 4 disappears after 30 seconds, which makes it a poor one to practise
acknowledging on. Use one that *persists*: a racing heart.

**Terminal C:**

```bash
.venv/bin/python sim/replay.py --synthetic --hr 130 --device-id bed-03 --speed 1 --loop &
```

This simulates a patient whose heart is running at 130 bpm. Rate alarms need the condition
to hold for 10 seconds before they fire (one fast reading should not wake a ward), so:

**Wait ~15 seconds.** You should see a **TACHYCARDIA** label with an **ACK** button, and
bed-03 rises above the healthy beds.

**Click ACK.**

**You should see:** the label dims and changes to `silenced 120s`, counting down.

**Now wait it out** (or just check back in two minutes): the countdown reaches zero and
**the alarm sounds again** — because the patient is still at 130 bpm. Silencing changed
your awareness, not the patient.

*(Why 120 seconds? The silence length depends on how dangerous the alarm is: 60 s for
high-priority alarms, 120 s for medium like this one, 300 s for low. A silenced cardiac
arrest should not stay silenced long.)*

**What this proves:** acknowledging is a *pause*, not a delete. And if the condition goes
away and later returns, it arrives as a brand-new alarm that has not been silenced.

**Clean up:**

```bash
pkill -f sim/replay.py
```

---

# Part 3 — The automated tests

These need nothing running. Run them in **Terminal C**.

## The quick ones (about 2 minutes total)

Run them one at a time. Every one should end in `checks passed` or `pass`.

```bash
.venv/bin/python dsp/filters.py          # 15 checks - the filters
.venv/bin/python dsp/synth.py            # 15 checks - the fake ECG generator
.venv/bin/python dsp/pan_tompkins.py     # 23 checks - heartbeat detection
.venv/bin/python dsp/vitals.py           # 28 checks - heart rate and alarm limits
.venv/bin/python dsp/ppg.py              # 41 checks - blood oxygen
.venv/bin/python dsp/quality.py          # 22 checks - "is this signal readable?"
.venv/bin/python sim/afe_model.py        # 47 checks - the fake circuit board
```

```bash
make -C firmware/test test                # 74 checks - the ESP32 code, no board needed
```

```bash
cd server && npm test && cd ..            # 101 tests - the server
```

**What this proves:** every piece works on its own.

## The "do the three versions agree?" tests (about 2 minutes)

The same filter is written three times — in Python, in JavaScript, and in fixed-point C
for the ESP32. These check they actually behave the same.

```bash
.venv/bin/python firmware/test/score_notch.py    # ESP32 C  vs  Python
node server/tools/validatePpg.js                 # JavaScript  vs  Python
cd server && npm run validate:streaming && cd ..  # JavaScript  vs  Python
```

**You should see:** `PASS` on each, with numbers like `0.52 counts` and `0.0000 %`.

**What this proves:** the code that would run on the chip does the same thing as the code
that was validated against real patients.

## The big one — real patient data (about 3 minutes)

```bash
.venv/bin/python dsp/validate.py
```

**You should see:** a table of 10 patient recordings, ending with:

```
  ALL   22459   98.73   98.64 ...
```

**What this means:** 22,459 individual heartbeats, marked by cardiologists. The system
found **98.73 %** of them, and **98.64 %** of what it found were real.

It also rewrites `docs/verification.md`. Open that file — it's generated, never typed by
hand, so it can't drift away from the truth.

## The end-to-end one (about 4 minutes)

```bash
.venv/bin/python sim/run_scenarios.py
```

This starts a real server eight times, connects a fake device, breaks something, and
measures what happens.

**You should see:**

```
  latency              PASS  median 0.5 ms ...
  leads_off            PASS  1.51 s
  outage               PASS  787/787 frames delivered, 0 gaps in seq
  spo2                 PASS  94.0 % against a true 94 %
  poor_signal          PASS  102/102 frames withheld a rate; 0 wrong rates reported
  reconnect_settling   PASS  0 fabricated rates above 110 bpm
  alarm_acknowledge    PASS  234 frames silenced
  device_silent        PASS  alarmed after 5.0 s

8/8 scenarios passed
```

**This is the one to show someone.** Everything else tests parts. This tests the actual
system, and it's what caught the biggest mistake in the project.

> It runs at real speed on purpose. A 2-second alarm cannot be tested in less than
> 2 seconds.

---

# Part 4 — The history (needs the database)

Two things must BOTH be true for this part, or every command below returns an error:

1. The database container is running (Step 1.1: `docker compose up -d db`)
2. The server was started **with** `DATABASE_URL` (the long command in Step 1.2 — not
   plain `npm start`)

Quick way to check both at once:

```bash
curl -s localhost:8080/api/sessions | head -c 200; echo
```

| What prints | What it means |
|---|---|
| `{"sessions": [...]}` | All good — carry on |
| `{"error":"history is not enabled"...}` | Server is running but without `DATABASE_URL`. Stop it (Ctrl+C in Terminal A) and rerun the long Step 1.2 command |
| *nothing at all* | The server is not running. Go back to Step 1.2 |

*(That last case is what `Expecting value: line 1 column 1` from `json.tool` means — it
was handed an empty response, because nothing answered.)*

**Terminal C** — list what has been recorded:

```bash
curl -s localhost:8080/api/sessions | python3 -m json.tool | head -20
```

Pick an `id` from that list — the first entry is the newest session. Put it in place of
`ID` below:

```bash
curl -s localhost:8080/api/sessions/ID/alarms | python3 -m json.tool
```

**You should see** each alarm with when it started, when someone silenced it, and when it
ended.

Download the readings as a spreadsheet file:

```bash
curl -sO localhost:8080/api/sessions/ID/vitals.csv
head -3 vitals.csv
```

**Look closely at the numbers.** Where the monitor didn't know a value, the field is
**empty** — not `0`. A spreadsheet showing `0 bpm` where the monitor meant *"I don't
know"* would be a dangerous lie.

---

# Part 5 — Stop everything

```bash
pkill -f sim/replay.py                  # stop the patients
```

Press `Ctrl+C` in **Terminal A** to stop the server.

```bash
docker compose down                     # stop the database
```

Add `-v` to `docker compose down` if you also want to erase the stored data.

---

# If something goes wrong

| Problem | Fix |
|---|---|
| `Address already in use` | A server is already running: `pkill -f "node src/index.js"` |
| Browser shows "reconnecting…" | The server isn't running. Check Terminal A |
| No devices in the dropdown | No patient is running. Redo Step 1.3 |
| `record 100 not found` | Run `.venv/bin/python dsp/fetch_data.py` |
| `[db] could not initialise persistence` | The database isn't up: `docker compose up -d db`, wait 10 seconds, restart the server |
| An alarm never appears | You used `--speed` above 1. Alarms are timed in real seconds — use `--speed 1` |
| `python: command not found` | Use `.venv/bin/python`, not `python` |
| `Expecting value: line 1 column 1 (char 0)` | The server is not running — `json.tool` got an empty response. Restart Step 1.2 |
| `/api/...` says `history is not enabled` | The server is running without the database. Restart it with the long Step 1.2 command |
| No ACK button on a dead bed | You looked more than 30 s after killing it — the session was closed and the alarm retired. Use Test 5's tachycardia patient instead, whose alarm persists |
| History full of odd names like `dev-A` | The server tests wrote into your database. See the note below |

### Note on that last one

If you run `npm test` while `DATABASE_URL` is set, the server tests write their own
sessions into the same database you're using for demos. They have names like `dev-A`,
`hist-1` and `order-4x8k...`.

To clear them:

```bash
docker exec vitalsense-db-1 psql -U vitalsense -d vitalsense -c \
  "DELETE FROM sessions WHERE device_id NOT LIKE 'bed-%' AND device_id NOT LIKE 'sim-%';"
```

Simplest way to avoid it: run `npm test` **without** `DATABASE_URL` set. The tests all
pass either way.

---

# What to run if you only have five minutes

```bash
# 1. start it
cd server && npm start &
cd .. && .venv/bin/python sim/replay.py --record 100 --speed 1 --scenario leads-off

# 2. watch http://127.0.0.1:8080 for 30 seconds - the numbers vanish and come back

# 3. prove it
.venv/bin/python sim/run_scenarios.py
```

That's the whole story: a real patient recording, a real fault, and eight measurements
that say the system behaved correctly.
