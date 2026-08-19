/**
 * Frame validation for the device↔server protocol (see docs/protocol.md).
 *
 * Everything arriving on `/ingest` is untrusted input: a device with a flashing bug,
 * a half-written frame from a dropped connection, or a simulator sending the wrong
 * shape. A monitor that accepts a malformed frame and displays whatever it managed
 * to parse is worse than one that refuses it, so validation is explicit and total -
 * every field is checked, and the reasons are returned rather than logged and lost.
 */

import { SAMPLE_RATE_HZ } from './dsp/coefficients.js';

/** Protocol version this server speaks. */
export const PROTOCOL_VERSION = 2;

/**
 * Versions this server understands.
 *
 * v1 devices send a `spo2` number they computed themselves; v2 devices send raw red and
 * infrared samples and let the server compute it, which is what D-03's thin-edge split
 * asks for and what lets the calculation be regression-tested against recordings.
 *
 * Both are accepted, and that is a deliberate departure from "reject anything
 * unexpected". Refusing v1 would black out every already-deployed device the moment the
 * server is updated - a fleet does not reflash atomically, and a monitor that goes dark
 * during an upgrade is a worse failure than one running an older frame format. What
 * D-14 forbids is *guessing* at a version, not supporting a known one (D-39).
 */
export const SUPPORTED_VERSIONS = Object.freeze([1, 2]);

/** Bounds chosen to be generous but finite - they catch nonsense, not edge cases. */
export const LIMITS = Object.freeze({
  maxSamplesPerFrame: 2048,
  minSamplesPerFrame: 1,
  maxEcgMv: 50, // real ECG is ~1 mV; 50 allows for a railed amplifier, not for garbage
  spo2: { min: 0, max: 100 },
  tempC: { min: 20, max: 45 },
  batteryV: { min: 0, max: 10 },
  maxDeviceIdLength: 64,
});

const isFiniteNumber = (v) => typeof v === 'number' && Number.isFinite(v);
const isNonNegativeInt = (v) => Number.isInteger(v) && v >= 0;

/**
 * Validate a device sample frame.
 *
 * @param {unknown} raw parsed JSON from the device
 * @param {object} [options]
 * @param {number} [options.expectedFs] sampling rate the DSP is configured for
 * @returns {{ok: boolean, errors: string[], warnings: string[], frame: object|null}}
 */
export function validateSampleFrame(raw, { expectedFs = SAMPLE_RATE_HZ } = {}) {
  const errors = [];
  const warnings = [];

  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return { ok: false, errors: ['frame must be a JSON object'], warnings, frame: null };
  }

  // --- version first: an unknown version means every other field is a guess ---
  if (!SUPPORTED_VERSIONS.includes(raw.v)) {
    return {
      ok: false,
      errors: [
        `unsupported protocol version ${JSON.stringify(raw.v)}, ` +
          `this server speaks ${SUPPORTED_VERSIONS.join(' and ')}`,
      ],
      warnings,
      frame: null,
    };
  }

  if (typeof raw.deviceId !== 'string' || raw.deviceId.length === 0) {
    errors.push('deviceId must be a non-empty string');
  } else if (raw.deviceId.length > LIMITS.maxDeviceIdLength) {
    errors.push(`deviceId exceeds ${LIMITS.maxDeviceIdLength} characters`);
  }

  if (!isNonNegativeInt(raw.seq)) errors.push('seq must be a non-negative integer');
  if (!isNonNegativeInt(raw.tDevice)) errors.push('tDevice must be a non-negative integer (ms since boot)');

  if (!isFiniteNumber(raw.fs) || raw.fs <= 0) {
    errors.push('fs must be a positive number');
  } else if (raw.fs !== expectedFs) {
    // Not a warning: the filter coefficients are designed for one rate, so a
    // mismatch means the DSP would be applied incorrectly to patient data.
    errors.push(`fs ${raw.fs} Hz does not match the configured ${expectedFs} Hz`);
  }

  if (!Array.isArray(raw.ecg)) {
    errors.push('ecg must be an array of samples');
  } else if (raw.ecg.length < LIMITS.minSamplesPerFrame || raw.ecg.length > LIMITS.maxSamplesPerFrame) {
    errors.push(
      `ecg length ${raw.ecg.length} outside [${LIMITS.minSamplesPerFrame}, ${LIMITS.maxSamplesPerFrame}]`,
    );
  } else {
    let nonFinite = 0;
    let outOfRange = 0;
    for (const s of raw.ecg) {
      if (typeof s !== 'number' || !Number.isFinite(s)) nonFinite += 1;
      else if (Math.abs(s) > LIMITS.maxEcgMv) outOfRange += 1;
    }
    // Non-finite samples are reported but do not reject the frame: the detector
    // holds the previous value for them, and dropping a whole frame over one bad
    // ADC reading would lose good data either side of it.
    if (nonFinite > 0) warnings.push(`${nonFinite} non-finite ECG sample(s)`);
    if (outOfRange > 0) warnings.push(`${outOfRange} ECG sample(s) beyond ±${LIMITS.maxEcgMv} mV`);
    if (nonFinite === raw.ecg.length) errors.push('every ECG sample in the frame is non-finite');
  }

  if (typeof raw.leadsOff !== 'boolean') {
    // Required, not optional: a monitor must always know whether it is connected.
    errors.push('leadsOff must be a boolean');
  }

  const optionalNumber = (name, bounds) => {
    const value = raw[name];
    if (value === undefined || value === null) return null;
    if (!isFiniteNumber(value)) {
      errors.push(`${name} must be a number or null`);
      return null;
    }
    if (value < bounds.min || value > bounds.max) {
      // Out of physiological range: keep the frame, drop the reading. Reporting a
      // 300 % SpO2 would be worse than reporting none.
      warnings.push(`${name} ${value} outside [${bounds.min}, ${bounds.max}] - discarded`);
      return null;
    }
    return value;
  };

  // --- raw PPG (v2): the device sends light, the server derives saturation ---
  let ppg = null;
  if (raw.ppg !== undefined && raw.ppg !== null) {
    const block = raw.ppg;
    if (typeof block !== 'object' || Array.isArray(block)) {
      errors.push('ppg must be an object with fs, red and ir');
    } else if (!Array.isArray(block.red) || !Array.isArray(block.ir)) {
      errors.push('ppg.red and ppg.ir must be arrays');
    } else if (block.red.length !== block.ir.length) {
      // Two wavelengths of different lengths cannot be a ratio of ratios. Sampled at
      // the same instant or not comparable at all.
      errors.push(`ppg.red and ppg.ir differ in length (${block.red.length} vs ${block.ir.length})`);
    } else if (!isFiniteNumber(block.fs) || block.fs <= 0) {
      errors.push('ppg.fs must be a positive number');
    } else if (block.red.length > LIMITS.maxSamplesPerFrame) {
      errors.push(`ppg block of ${block.red.length} samples exceeds ${LIMITS.maxSamplesPerFrame}`);
    } else {
      const finite = (arr) => arr.every((v) => typeof v === 'number' && Number.isFinite(v));
      if (!finite(block.red) || !finite(block.ir)) {
        errors.push('ppg samples must all be finite numbers');
      } else {
        ppg = { fs: block.fs, red: block.red, ir: block.ir };
      }
    }
  }

  const spo2 = optionalNumber('spo2', LIMITS.spo2);
  const temp = optionalNumber('temp', LIMITS.tempC);
  const battery = optionalNumber('battery', LIMITS.batteryV);

  if (errors.length > 0) return { ok: false, errors, warnings, frame: null };

  return {
    ok: true,
    errors,
    warnings,
    frame: {
      v: raw.v,
      deviceId: raw.deviceId,
      seq: raw.seq,
      tDevice: raw.tDevice,
      fs: raw.fs,
      ecg: raw.ecg,
      spo2,
      temp,
      leadsOff: raw.leadsOff,
      battery,
      ppg,
    },
  };
}

/**
 * Build a server→dashboard vitals frame.
 *
 * `alarms` is always an array and `hr` is explicitly `null` when unknown, so a
 * dashboard cannot mistake "field missing" for "nothing wrong".
 */
export function makeVitalsFrame({
  deviceId,
  tServer,
  sampleIndex,
  ecg,
  beats,
  hr,
  hrClass,
  spo2,
  spo2Source,
  spo2Quality,
  pulseBpm,
  perfusionPct,
  rateAgreement,
  temp,
  signalQuality,
  alarms,
  stats,
}) {
  return {
    v: PROTOCOL_VERSION,
    type: 'vitals',
    deviceId,
    tServer,
    // Absolute index of the first sample in `ecg`. Beats are absolute indices too,
    // so a beat confirmed after its R peak has already been sent still lands in the
    // right place on the dashboard's waveform (see docs/protocol.md).
    sampleIndex,
    ecg: Array.from(ecg, (s) => Math.round(s * 10000) / 10000),
    beats,
    hr: hr === null || hr === undefined ? null : Math.round(hr * 10) / 10,
    hrClass,
    spo2: spo2 ?? null,
    // Where the number came from. A saturation the server derived from raw red/IR is a
    // different kind of evidence from one a sensor module reported about itself, and a
    // clinician - or a reviewer - should not have to guess which they are looking at.
    spo2Source: spo2Source ?? null,
    spo2Quality: spo2Quality ?? null,
    pulseBpm: pulseBpm === null || pulseBpm === undefined ? null : Math.round(pulseBpm * 10) / 10,
    perfusionPct: perfusionPct === null || perfusionPct === undefined ? null : Math.round(perfusionPct * 100) / 100,
    // How the two independent rate measurements compare: 'agree', 'pulse-deficit'
    // (fewer pulses than beats - a real clinical finding), 'implausible' or
    // 'harmonic-suspected' (both mean the PPG rate was withheld), or null.
    rateAgreement: rateAgreement ?? null,
    temp: temp ?? null,
    signalQuality,
    alarms,
    stats,
  };
}

/** Build an error message sent back to a device whose frame was rejected. */
export function makeErrorFrame(errors) {
  return { v: PROTOCOL_VERSION, type: 'error', errors };
}
