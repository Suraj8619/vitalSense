/**
 * Alarm engine.
 *
 * Rules are declarative (see `RULES`) and share one evaluation loop, so adding an
 * alarm cannot accidentally introduce different latching behaviour from the others.
 *
 * Every rate-based alarm requires its condition to hold for a **sustained** period
 * rather than firing on a single reading. That is not a nicety: monitors that alarm
 * on one noisy beat train their users to ignore alarms, and alarm fatigue is a
 * documented cause of missed real events. The sustain times come from
 * docs/protocol.md so the specification and the implementation cannot disagree.
 */

/** Severity ordering, most urgent first - used to sort what the dashboard shows. */
const SEVERITY_RANK = { high: 0, medium: 1, low: 2 };

/**
 * @typedef {object} AlarmContext
 * @property {boolean} leadsOff
 * @property {number|null} hr
 * @property {string} hrClass
 * @property {number|null} spo2
 * @property {'good'|'poor'|'unusable'} signalQuality
 * @property {number} secondsSinceLastBeat
 * @property {number} secondsSinceLastFrame
 */

/**
 * Alarm definitions.
 *
 * `test` returns true while the condition holds. An alarm becomes active once the
 * condition has held continuously for `sustainMs`, and clears as soon as it stops.
 */
export const RULES = [
  {
    code: 'LEADS_OFF',
    severity: 'high',
    // SR-03 requires the alarm within 2 s of the electrode detaching, end to end.
    // The debounce is part of that budget, not additional to it: with a 2000 ms
    // debounce the alarm could not fire before 2 s, so frame and processing latency
    // pushed it to 2.1 s and the requirement failed by construction. 1500 ms leaves
    // 500 ms of margin for the rest of the path.
    sustainMs: 1500,
    message: 'ECG electrodes detached',
    test: (c) => c.leadsOff === true,
  },
  {
    code: 'DEVICE_SILENT',
    severity: 'high',
    sustainMs: 0, // the silence itself is the sustain
    message: 'No data received from device',
    test: (c) => c.secondsSinceLastFrame >= 5,
  },
  {
    code: 'ASYSTOLE',
    severity: 'high',
    sustainMs: 0,
    message: 'No heartbeat detected',
    // Only meaningful while the electrodes are on and the signal is usable -
    // otherwise this is a wiring fault, already reported as LEADS_OFF, and firing
    // both would bury the one the clinician can act on.
    test: (c) => !c.leadsOff && c.signalQuality !== 'unusable' && c.secondsSinceLastBeat >= 4,
  },
  {
    code: 'TACHYCARDIA',
    severity: 'medium',
    sustainMs: 10000,
    message: 'Heart rate above 100 bpm',
    test: (c) => c.hrClass === 'tachycardia',
  },
  {
    code: 'BRADYCARDIA',
    severity: 'medium',
    sustainMs: 10000,
    message: 'Heart rate below 60 bpm',
    test: (c) => c.hrClass === 'bradycardia',
  },
  {
    code: 'SPO2_LOW',
    severity: 'medium',
    sustainMs: 10000,
    message: 'Blood oxygen saturation below 92%',
    test: (c) => c.spo2 !== null && c.spo2 !== undefined && c.spo2 < 92,
  },
  {
    code: 'SIGNAL_POOR',
    severity: 'low',
    sustainMs: 5000,
    message: 'Signal quality too poor to derive vitals',
    // Only meaningful while data is actually arriving. A silent device has no
    // signal to judge, and raising SIGNAL_POOR next to DEVICE_SILENT would add a
    // second alarm that tells the clinician nothing the first one did not.
    test: (c) => c.signalQuality === 'unusable' && !c.leadsOff && c.secondsSinceLastFrame < 5,
  },
];

/**
 * How long an acknowledgement silences an alarm before it sounds again, by severity.
 *
 * Acknowledging is a **silence**, not a dismissal. If the condition is still true after
 * the interval, the alarm returns - because the patient's situation has not changed, only
 * the clinician's awareness of it, and awareness fades. Every bedside monitor works this
 * way, and the reason is the failure mode in the other direction: an alarm that can be
 * permanently switched off while the condition holds is an alarm that gets switched off
 * (D-44).
 *
 * High-severity alarms return soonest. A silenced asystole is not something to leave
 * silenced for five minutes.
 */
export const REALARM_MS = Object.freeze({
  high: 60_000,
  medium: 120_000,
  low: 300_000,
});

export class AlarmEngine {
  /**
   * @param {object} [options]
   * @param {typeof RULES} [options.rules]
   */
  constructor({ rules = RULES } = {}) {
    this.rules = rules;
    /** @type {Map<string, {since: number, active: boolean}>} */
    this.state = new Map();
    /**
     * Acknowledgements, keyed by code. Cleared when the alarm's condition clears, so a
     * condition that goes away and comes back is a new episode that has not been
     * acknowledged - the clinician silenced *that* event, not this one.
     * @type {Map<string, {at: number, by: string|null}>}
     */
    this.acknowledged = new Map();
  }

  /**
   * Evaluate every rule against the current context.
   *
   * @param {AlarmContext} context
   * @param {number} now epoch milliseconds
   * @returns {Array<{code: string, severity: string, since: number, message: string}>}
   *   active alarms, most severe first
   */
  update(context, now) {
    const active = [];

    for (const rule of this.rules) {
      let holding = false;
      try {
        holding = rule.test(context) === true;
      } catch {
        // A rule that throws on odd input must not take the alarm engine down with
        // it - the remaining alarms are still the ones keeping the patient safe.
        holding = false;
      }

      const previous = this.state.get(rule.code);

      if (!holding) {
        this.state.delete(rule.code);
        // The episode is over. A later recurrence is a new event and arrives unsilenced.
        this.acknowledged.delete(rule.code);
        continue;
      }

      const since = previous ? previous.since : now;
      this.state.set(rule.code, { since, active: now - since >= rule.sustainMs });

      if (now - since >= rule.sustainMs) {
        const ack = this.acknowledged.get(rule.code);
        let silenced = false;
        if (ack) {
          const window = REALARM_MS[rule.severity] ?? REALARM_MS.medium;
          if (now - ack.at < window) {
            silenced = true;
          } else {
            // The silence has expired and the condition still holds. Sound again.
            this.acknowledged.delete(rule.code);
          }
        }
        active.push({
          code: rule.code,
          severity: rule.severity,
          since,
          message: rule.message,
          acknowledged: silenced,
          acknowledgedAt: silenced ? ack.at : null,
          acknowledgedBy: silenced ? ack.by : null,
          // When it will sound again if nothing changes - shown so the silence is
          // visibly temporary rather than looking like the alarm went away.
          resoundsAt: silenced ? ack.at + (REALARM_MS[rule.severity] ?? REALARM_MS.medium) : null,
        });
      }
    }

    active.sort(
      (a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] || a.since - b.since,
    );
    return active;
  }

  /**
   * Silence an alarm that is currently sounding.
   *
   * Refuses codes that are not active: acknowledging an alarm before it exists would
   * pre-silence it, which is exactly the shortcut that makes acknowledgement dangerous.
   *
   * @returns {boolean} whether anything was silenced
   */
  acknowledge(code, now, by = null) {
    const s = this.state.get(code);
    if (!s || !s.active) return false;
    this.acknowledged.set(code, { at: now, by });
    return true;
  }

  /** Codes whose condition is holding but whose sustain time has not yet elapsed. */
  pending(now) {
    const out = [];
    for (const [code, s] of this.state) {
      if (!s.active) {
        const rule = this.rules.find((r) => r.code === code);
        out.push({ code, msRemaining: Math.max(0, rule.sustainMs - (now - s.since)) });
      }
    }
    return out;
  }

  reset() {
    this.state.clear();
    this.acknowledged.clear();
  }
}
