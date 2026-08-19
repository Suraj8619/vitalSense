/**
 * Per-device monitoring session.
 *
 * Owns everything that is stateful about one patient: filter and detector state,
 * the rolling heart-rate window, alarm latching, and frame accounting. One object
 * per connected device means two patients can never share DSP state - a bug class
 * worth designing out rather than testing for.
 */

import { AlarmEngine } from './alarms.js';
import { QrsStreamDetector, UNUSABLE_CONCENTRATION, UNUSABLE_SNR } from './dsp/qrsDetector.js';
import { RollingHeartRate } from './dsp/vitals.js';
import { estimateSpo2, SUPPORTED_RATES } from './dsp/ppg.js';
import { makeVitalsFrame } from './protocol.js';
import { SAMPLE_RATE_HZ } from './dsp/coefficients.js';

/** Seconds of PPG held for the saturation estimate - long enough to average beats. */
const PPG_WINDOW_S = 10;
/** Below this there are too few beats for a median of per-beat ratios to mean anything. */
const PPG_MIN_S = 4;

/** Agreement tolerance between the two rates, in bpm, before anything is said about it. */
const RATE_AGREEMENT_BPM = 8;
/** How close to exactly half the heart rate counts as a suspected sub-harmonic lock. */
const HARMONIC_TOLERANCE_BPM = 6;

/** Heart rate is derived from the beats in this trailing window. */
const HR_WINDOW_S = 10;
/**
 * How long the monitor withholds a heart rate after the electrodes are reconnected.
 *
 * Reconnection drives a step change through the filters and leaves the adaptive
 * thresholds tuned to the flat-line period; the resulting burst of detections
 * reads as a very fast heart rate. Bedside monitors blank the rate across this
 * settling period for the same reason. Long enough to cover the detector's 2 s
 * relearning window plus filter settling.
 */
const RECONNECT_SETTLE_S = 3;

export class DeviceSession {
  /**
   * @param {string} deviceId
   * @param {object} [options]
   * @param {number} [options.fs]
   * @param {() => number} [options.now] clock injection, so tests are not timing-dependent
   */
  constructor(deviceId, { fs = SAMPLE_RATE_HZ, now = Date.now, store = null } = {}) {
    this.deviceId = deviceId;
    this.fs = fs;
    this.now = now;

    // Persistence, if any. A null store is a supported configuration, not a broken one
    // (D-43), so every call site must tolerate a session id of null.
    this.store = store;
    this.storeSessionId = null;
    this.lastPersistedSecond = null;
    this.persistedAlarms = new Map();

    this.detector = new QrsStreamDetector({ fs });
    this.rollingHr = new RollingHeartRate(fs, HR_WINDOW_S);
    this.alarms = new AlarmEngine();

    this.startedAt = now();
    this.lastFrameAt = now();
    this.lastSeq = null;
    /** Set when the device's socket closes; cleared on reconnect. */
    this.disconnectedAt = null;
    this.leadsOffPrev = false;
    /** Sample index before which no heart rate may be reported. */
    this.settleUntilSample = 0;

    this.stats = {
      framesReceived: 0,
      framesDropped: 0, // inferred from gaps in seq
      framesRejected: 0,
      beatsDetected: 0,
    };

    // Last known good readings, kept only to report them; never re-sent as if fresh.
    this.lastSpo2 = null;
    // Saturation is a whole-window measurement, not a per-sample one: it needs several
    // beats to average over and its pulse period comes from an autocorrelation across
    // the window. So raw PPG is buffered and the estimate recomputed as it fills.
    this.ppgRed = [];
    this.ppgIr = [];
    this.ppgFs = null;
    this.lastSpo2Result = null;
    this.spo2Source = null;
    this.lastTemp = null;
  }

  /**
   * Classify how much the derived numbers can be trusted.
   *
   * Returns 'unusable' when the system should refuse to report a heart rate at all
   * (requirement SR-07) - a monitor that says "I don't know" is safe, one that
   * guesses is not.
   *
   * @param {boolean} leadsOff
   * @param {{trustworthy: boolean, bpm: number|null}} hr
   */
  #signalQuality(leadsOff, hr) {
    if (leadsOff || this.detector.flatline) return 'unusable';
    // Settling after a reconnection: there is signal, but nothing derived from it
    // is trustworthy yet.
    if (this.detector.n < this.settleUntilSample) return 'unusable';

    // SR-07. The gate that decides whether to answer at all: below this the rate stops
    // meeting SR-01, so "too poor to report" and "cannot meet the requirement" are the
    // same statement (D-35). Until Phase 5 this metric existed only in Python and the
    // server never consulted it - the fault-injection matrix caught the shipped system
    // reporting 102 wrong rates on a swamped signal while calling the quality 'good'.
    // Both conditions, not either: concentration alone is rate-confounded and silences
    // a clean tachycardia (the rhythm this monitor most needs to report), while the
    // SNR alone cannot separate a clean 180 bpm from noise. Low concentration AND a
    // low detector SNR means the energy really is spread by noise (D-51).
    const concentration = this.detector.energyConcentration;
    if (
      concentration !== null &&
      concentration < UNUSABLE_CONCENTRATION &&
      this.detector.snr < UNUSABLE_SNR
    ) {
      return 'unusable';
    }

    if (!hr.trustworthy && hr.nBeats > 1) return 'poor';
    return 'good';
  }

  /**
   * Fold this frame's PPG into the buffer and re-derive saturation.
   *
   * Two sources are possible and they are not equivalent. A v2 device sends raw red and
   * infrared light and the server computes the ratio of ratios here, where it can be
   * regression-tested against recordings (D-03). A v1 device sends a number its sensor
   * module computed internally, which is usable but is not evidence this system can
   * check. Whichever is used, the vitals frame says which - a number's provenance is
   * part of the number.
   *
   * @param {object} frame validated device frame
   * @returns {{value: number|null, source: string|null, result: object|null}}
   */
  #updateSpo2(frame) {
    if (frame.ppg) {
      if (this.ppgFs !== frame.ppg.fs) {
        // A rate change mid-session means the buffer holds two different time bases.
        // Reset rather than mix them (the same reasoning as D-14).
        this.ppgRed = [];
        this.ppgIr = [];
        this.ppgFs = frame.ppg.fs;
      }
      this.ppgRed.push(...frame.ppg.red);
      this.ppgIr.push(...frame.ppg.ir);
      const capacity = Math.round(PPG_WINDOW_S * this.ppgFs);
      if (this.ppgRed.length > capacity) {
        this.ppgRed = this.ppgRed.slice(-capacity);
        this.ppgIr = this.ppgIr.slice(-capacity);
      }

      if (!SUPPORTED_RATES.includes(this.ppgFs)) {
        this.lastSpo2Result = null;
        this.spo2Source = null;
        return { value: null, source: null, result: null };
      }
      if (this.ppgRed.length >= Math.round(PPG_MIN_S * this.ppgFs)) {
        const result = estimateSpo2(
          Float64Array.from(this.ppgRed),
          Float64Array.from(this.ppgIr),
          this.ppgFs,
        );
        this.lastSpo2Result = result;
        this.spo2Source = result.spo2Pct === null ? null : 'server';
        return { value: result.spo2Pct, source: this.spo2Source, result };
      }
      return { value: null, source: null, result: this.lastSpo2Result };
    }

    // v1 device, or a v2 device whose PPG sensor has nothing to report.
    this.lastSpo2Result = null;
    if (frame.spo2 !== null && frame.spo2 !== undefined) {
      this.spo2Source = 'device';
      return { value: frame.spo2, source: 'device', result: null };
    }
    this.spo2Source = null;
    return { value: null, source: null, result: null };
  }

  /**
   * Compare the two independent measurements of one quantity.
   *
   * The ECG counts electrical depolarisations; the PPG counts arterial pressure waves.
   * They are different physics measuring the same heart, which makes their disagreement
   * far stronger evidence than any single-signal threshold - the point D-37 arrived at
   * after finding that no periodicity threshold separates good PPG windows from bad.
   *
   * The comparison is deliberately **asymmetric**, because the physiology is:
   *
   * - A pulse rate *below* the heart rate is real and clinically important. In atrial
   *   fibrillation and frequent ectopy some beats are too weak to open the aortic valve,
   *   so they never reach the finger. That is a **pulse deficit**, a sign a clinician
   *   wants to see - not an error to be hidden.
   * - A pulse rate *above* the heart rate is impossible. Every pulse needs a beat behind
   *   it. Seeing more pulses than beats means the PPG is counting something that is not
   *   a pulse, so its rate is withheld.
   * - A pulse rate near exactly *half* the heart rate is the signature of sub-harmonic
   *   lock, the failure that reported 45 bpm for a patient at 90 (D-36). The fix reduced
   *   it; this catches what survives.
   *
   * @param {number|null} hrBpm heart rate from the ECG
   * @param {number|null} pulseBpm pulse rate from the PPG
   * @returns {{agreement: string|null, trustPulse: boolean}}
   */
  #crossCheckRates(hrBpm, pulseBpm) {
    if (hrBpm === null || pulseBpm === null) return { agreement: null, trustPulse: true };

    const difference = pulseBpm - hrBpm;
    if (Math.abs(difference) <= RATE_AGREEMENT_BPM) return { agreement: 'agree', trustPulse: true };

    if (Math.abs(pulseBpm * 2 - hrBpm) <= HARMONIC_TOLERANCE_BPM) {
      return { agreement: 'harmonic-suspected', trustPulse: false };
    }
    if (difference > 0) {
      // More pulses than beats. Whatever the optical channel is following, it is not
      // the heart.
      return { agreement: 'implausible', trustPulse: false };
    }
    // Fewer pulses than beats: report both numbers and name the finding.
    return { agreement: 'pulse-deficit', trustPulse: true };
  }

  /**
   * Write this frame's derived numbers to history, at 1 Hz.
   *
   * Frames arrive at ~11 Hz; storing all of them would be eleven times the volume for a
   * trend nobody reads at that resolution. One row per second is what the schema's
   * primary key already declares, and the alarm engine keeps its own full-rate state -
   * history is for review, not for control.
   *
   * Deliberately not awaited by the caller. A slow or failed write must not delay the
   * ingest path: losing history is bad, and a monitor that stops displaying because a
   * disk filled up is worse.
   */
  #persist(vitals) {
    if (!this.store || this.storeSessionId === null) return;

    const second = Math.floor(vitals.tServer / 1000) * 1000;
    if (second !== this.lastPersistedSecond) {
      this.lastPersistedSecond = second;
      void this.store.recordVitals(this.storeSessionId, {
        tServer: second,
        sampleIndex: vitals.sampleIndex,
        hr: vitals.hr,
        hrClass: vitals.hrClass,
        spo2: vitals.spo2,
        spo2Source: vitals.spo2Source,
        spo2Quality: vitals.spo2Quality,
        pulseBpm: vitals.pulseBpm,
        perfusionPct: vitals.perfusionPct,
        rateAgreement: vitals.rateAgreement,
        tempC: vitals.temp,
        signalQuality: vitals.signalQuality,
      });
    }

    // Alarm *episodes*: diff the active set against what is already open, so a condition
    // holding for ten minutes is one row rather than seven thousand.
    const active = new Map(vitals.alarms.map((a) => [a.code, a]));
    for (const [code, alarm] of active) {
      if (!this.persistedAlarms.has(code)) {
        this.persistedAlarms.set(code, alarm.since ?? vitals.tServer);
        void this.store.raiseAlarm(this.storeSessionId, {
          code,
          severity: alarm.severity,
          message: alarm.message,
          raisedAt: alarm.since ?? vitals.tServer,
        });
      }
    }
    for (const code of [...this.persistedAlarms.keys()]) {
      if (!active.has(code)) {
        this.persistedAlarms.delete(code);
        void this.store.clearAlarm(this.storeSessionId, code, vitals.tServer);
      }
    }
  }

  /**
   * Test seam for the private cross-check. Exposed deliberately and narrowly: the rule
   * has four branches with clinical meaning and testing it only through a full ingest
   * would leave three of them uncovered.
   */
  crossCheckForTest(hrBpm, pulseBpm) {
    return this.#crossCheckRates(hrBpm, pulseBpm);
  }

  /**
   * Process one validated device frame.
   *
   * @param {object} frame validated by `validateSampleFrame`
   * @returns {object} the vitals frame to broadcast
   */
  ingest(frame) {
    const now = this.now();

    // --- frame accounting: gaps in seq are the only evidence of lost data ---
    if (this.lastSeq !== null) {
      const gap = frame.seq - this.lastSeq - 1;
      // A device reboot restarts seq at 0; that is a new session, not a million
      // dropped frames, so only forward gaps count.
      if (gap > 0) this.stats.framesDropped += gap;
    }
    this.lastSeq = frame.seq;
    this.lastFrameAt = now;
    this.stats.framesReceived += 1;

    // --- electrode reconnection: restart acquisition rather than carry artefacts ---
    if (this.leadsOffPrev && !frame.leadsOff) {
      this.detector.restart();
      this.rollingHr.beats = [];
      this.settleUntilSample = this.detector.n + RECONNECT_SETTLE_S * this.fs;
      // The silence during the interruption is not the patient's silence (D-47).
      this.rollingHr.restart(this.detector.n);
    }
    this.leadsOffPrev = frame.leadsOff;

    // --- DSP ---
    const { cleaned, beats } = this.detector.push(frame.ecg);
    this.stats.beatsDetected += beats.length;
    // Absolute index of the first sample of this block, after the detector advanced.
    const sampleIndex = this.detector.n - cleaned.length;

    // Detection is meaningless while the electrodes are off; feeding those beats
    // into the rate window would leave a stale heart rate on screen after the lead
    // was pulled - exactly the failure the leads-off alarm exists to prevent.
    const settling = this.detector.n < this.settleUntilSample;
    if (!frame.leadsOff && !settling) {
      this.rollingHr.add(beats, this.detector.n);
    } else {
      // Beats found while the electrodes are off, or while the front end is still
      // settling after reconnection, are artefacts - counting them would put a
      // fabricated heart rate on screen.
      this.rollingHr.add([], this.detector.n);
    }

    const hr = this.rollingHr.current();
    const quality = this.#signalQuality(frame.leadsOff, hr);
    const reportHr = quality === 'unusable' ? null : hr.bpm;

    const derived = this.#updateSpo2(frame);
    if (derived.value !== null) this.lastSpo2 = derived.value;
    if (frame.temp !== null) this.lastTemp = frame.temp;

    const rawPulse = derived.result ? derived.result.pulseBpm : null;
    const cross = this.#crossCheckRates(reportHr, rawPulse);

    const secondsSinceLastBeat = this.rollingHr.samplesSinceLastBeat(this.detector.n) / this.fs;

    const active = this.alarms.update(
      {
        leadsOff: frame.leadsOff,
        hr: reportHr,
        hrClass: quality === 'unusable' ? 'insufficient data' : hr.classification,
        spo2: derived.value,
        signalQuality: quality,
        rateAgreement: cross.agreement,
        secondsSinceLastBeat,
        secondsSinceLastFrame: 0, // this frame just arrived
      },
      now,
    );

    const vitalsFrame = makeVitalsFrame({
      deviceId: this.deviceId,
      tServer: now,
      sampleIndex,
      ecg: cleaned,
      // Absolute sample indices, not offsets into this block. R-peak snapping walks
      // backwards past the start of the block (the integration window lags the QRS
      // by ~75 ms, which is more than one 89 ms frame after fiducial search), so
      // block-relative offsets were frequently negative and the dashboard dropped
      // them - only about 8% of beats survived. Absolute indices let the dashboard
      // mark a beat on waveform it already holds.
      beats,
      hr: reportHr,
      hrClass: quality === 'unusable' ? 'insufficient data' : hr.classification,
      spo2: derived.value,
      spo2Source: derived.source,
      spo2Quality: cross.trustPulse ? (derived.result ? derived.result.quality : null) : 'poor',
      pulseBpm: cross.trustPulse ? rawPulse : null,
      perfusionPct: derived.result ? derived.result.perfusionPct : null,
      rateAgreement: cross.agreement,
      temp: frame.temp,
      signalQuality: quality,
      alarms: active,
      stats: {
        ...this.stats,
        uptimeS: Math.round((now - this.startedAt) / 100) / 10,
      },
    });
    this.#persist(vitalsFrame);
    return vitalsFrame;
  }

  /**
   * Re-evaluate alarms without a new frame, so a device that goes silent is
   * noticed. Called on a timer by the server.
   *
   * @returns {object|null} a vitals frame if something changed, else null
   */
  tick() {
    const now = this.now();
    const secondsSinceLastFrame = (now - this.lastFrameAt) / 1000;
    if (secondsSinceLastFrame < 1) return null;

    const hr = this.rollingHr.current();
    // No cross-check here. This path runs when no frame has arrived, so it reports no
    // heart rate and no pulse rate - there is nothing to compare, and comparing two
    // absent numbers would only invent a verdict about a patient nobody is measuring.
    const secondsSinceLastBeat = this.rollingHr.samplesSinceLastBeat(this.detector.n) / this.fs;
    const quality = secondsSinceLastFrame >= 5 ? 'unusable' : this.#signalQuality(false, hr);

    const active = this.alarms.update(
      {
        leadsOff: false,
        hr: null,
        hrClass: 'insufficient data',
        spo2: null,
        signalQuality: quality,
        secondsSinceLastBeat,
        secondsSinceLastFrame,
      },
      now,
    );

    const vitalsFrame = makeVitalsFrame({
      deviceId: this.deviceId,
      tServer: now,
      sampleIndex: this.detector.n,
      ecg: [],
      beats: [],
      hr: null,
      hrClass: 'insufficient data',
      spo2: null,
      temp: null,
      signalQuality: quality,
      alarms: active,
      stats: { ...this.stats, uptimeS: Math.round((now - this.startedAt) / 100) / 10 },
    });
    this.#persist(vitalsFrame);
    return vitalsFrame;
  }
}
