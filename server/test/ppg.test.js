/**
 * SpO2: the JavaScript port, and the server path that uses it.
 *
 * The numerical agreement with the Python design is scored separately by
 * `npm run validate:ppg` against exported fixtures. These tests cover the behaviour that
 * matters at the system level: that raw light becomes a saturation, that a bad signal
 * becomes no saturation rather than a wrong one, and that the number's provenance
 * travels with it.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { estimateSpo2, findPulses, periodicity, pulsatile, SUPPORTED_RATES } from '../src/dsp/ppg.js';
import { validateSampleFrame, SUPPORTED_VERSIONS } from '../src/protocol.js';
import { DeviceSession } from '../src/session.js';

const FS = 100;

/** Red and IR channels with a known saturation, mirroring dsp/ppg.py's synthesiser. */
function synthPpg({ seconds = 12, bpm = 72, spo2 = 97, perfusion = 2, motion = 0, dcIr = 1e5, dcRed = 8e4 } = {}) {
  const n = Math.round(seconds * FS);
  const red = new Float64Array(n);
  const ir = new Float64Array(n);
  const rr = 60 / bpm;
  const ratioIr = perfusion / 100;
  const rTrue = (110 - spo2) / 25;
  const ratioRed = rTrue * ratioIr;

  for (let i = 0; i < n; i += 1) {
    const t = i / FS;
    const phase = (t % rr) / rr;
    const systolic = Math.exp(-(((phase - 0.22) / 0.11) ** 2));
    const dicrotic = 0.35 * Math.exp(-(((phase - 0.52) / 0.1) ** 2));
    // Normalised the same way the Python generator does: unit peak-to-peak.
    const w = (systolic + dicrotic) / 1.0294;
    ir[i] = dcIr * (1 - ratioIr * w);
    red[i] = dcRed * (1 - ratioRed * w);
    if (motion) {
      ir[i] += motion * dcIr * ratioIr * Math.sin(2 * Math.PI * 0.7 * t);
      red[i] += motion * 1.6 * dcRed * ratioRed * Math.sin(2 * Math.PI * 0.7 * t + 0.4);
    }
  }
  return { red, ir };
}

test('a healthy PPG yields a plausible saturation and pulse', () => {
  const { red, ir } = synthPpg({ spo2: 97, bpm: 72 });
  const r = estimateSpo2(red, ir, FS);
  assert.ok(r.spo2Pct !== null, `expected a reading, got: ${r.reason}`);
  assert.ok(Math.abs(r.spo2Pct - 97) < 2, `SpO2 ${r.spo2Pct}`);
  assert.ok(Math.abs(r.pulseBpm - 72) < 3, `pulse ${r.pulseBpm}`);
  assert.ok(r.perfusionPct > 1 && r.perfusionPct < 4, `perfusion ${r.perfusionPct}`);
});

test('desaturation is tracked, not clipped to normal', () => {
  const high = estimateSpo2(...Object.values(synthPpg({ spo2: 98 })), FS);
  const low = estimateSpo2(...Object.values(synthPpg({ spo2: 88 })), FS);
  assert.ok(low.spo2Pct < high.spo2Pct - 5, `${low.spo2Pct} vs ${high.spo2Pct}`);
});

test('the reading is independent of LED brightness and finger thickness', () => {
  const bright = estimateSpo2(...Object.values(synthPpg({ spo2: 95 })), FS);
  const dim = estimateSpo2(...Object.values(synthPpg({ spo2: 95, dcIr: 4e4, dcRed: 3.2e4 })), FS);
  assert.ok(Math.abs(bright.spo2Pct - dim.spo2Pct) < 0.5, `${bright.spo2Pct} vs ${dim.spo2Pct}`);
});

test('motion is refused rather than reported as a false desaturation', () => {
  // The failure this guards: with only a beat-to-beat consistency check, a 95 % patient
  // was reported at 86.9 % marked "good", because a periodic artefact is self-consistent.
  const r = estimateSpo2(...Object.values(synthPpg({ spo2: 95, motion: 1.5 })), FS);
  const accurate = r.spo2Pct !== null && Math.abs(r.spo2Pct - 95) < 2;
  assert.ok(accurate || r.spo2Pct === null, `reported ${r.spo2Pct} with quality ${r.quality}`);
});

test('a dead sensor reports nothing rather than a number', () => {
  const flat = new Float64Array(1200).fill(5e4);
  assert.equal(estimateSpo2(flat, flat, FS).spo2Pct, null);
  const dark = new Float64Array(1200);
  assert.equal(estimateSpo2(dark, dark, FS).spo2Pct, null);
});

test('every refusal explains itself', () => {
  const dark = new Float64Array(1200);
  const r = estimateSpo2(dark, dark, FS);
  assert.ok(r.reason.length > 0, 'refusals must carry a reason');
  assert.equal(r.quality, 'unusable');
});

test('an unsupported sample rate is refused, not silently mis-filtered', () => {
  const { red, ir } = synthPpg();
  assert.throws(() => estimateSpo2(red, ir, 250), /no PPG coefficients for 250/);
  assert.ok(SUPPORTED_RATES.includes(100) && SUPPORTED_RATES.includes(125));
});

test('mismatched channel lengths are refused', () => {
  assert.throws(() => estimateSpo2(new Float64Array(10), new Float64Array(20), FS), /same length/);
});

test('the dicrotic wave is not counted as a beat at low rates', () => {
  const { ir } = synthPpg({ seconds: 20, bpm: 48 });
  const peaks = findPulses(ir, FS);
  const expected = Math.round((20 * 48) / 60);
  assert.ok(Math.abs(peaks.length - expected) <= 2, `${peaks.length} peaks vs ~${expected}`);
});

test('the period estimate prefers the fundamental over its multiples', () => {
  const { ir } = synthPpg({ seconds: 20, bpm: 90 });
  const { periodS } = periodicity(pulsatile(ir, FS), FS);
  assert.ok(Math.abs(60 / periodS - 90) < 3, `${60 / periodS} bpm`);
});

// ------------------------------------------------------------------ protocol ---

const baseFrame = (extra = {}) => ({
  v: 2,
  deviceId: 'dev-1',
  seq: 0,
  tDevice: 0,
  fs: 360,
  ecg: new Array(32).fill(0.1),
  leadsOff: false,
  ...extra,
});

test('both protocol versions are accepted', () => {
  assert.deepEqual(SUPPORTED_VERSIONS, [1, 2]);
  assert.ok(validateSampleFrame(baseFrame({ v: 1 })).ok);
  assert.ok(validateSampleFrame(baseFrame({ v: 2 })).ok);
});

test('an unknown protocol version is refused rather than guessed at', () => {
  const { ok, errors } = validateSampleFrame(baseFrame({ v: 99 }));
  assert.equal(ok, false);
  assert.match(errors[0], /unsupported protocol version/);
});

test('a raw PPG block is accepted and carried through', () => {
  const { ok, frame } = validateSampleFrame(baseFrame({ ppg: { fs: 100, red: [1, 2], ir: [3, 4] } }));
  assert.equal(ok, true);
  assert.deepEqual(frame.ppg, { fs: 100, red: [1, 2], ir: [3, 4] });
});

test('two wavelengths of different lengths cannot be a ratio, and are refused', () => {
  const { ok, errors } = validateSampleFrame(baseFrame({ ppg: { fs: 100, red: [1, 2, 3], ir: [1, 2] } }));
  assert.equal(ok, false);
  assert.match(errors.join(' '), /differ in length/);
});

test('a malformed PPG block is refused', () => {
  for (const ppg of [{ fs: 100, red: [1], ir: 'x' }, { fs: 0, red: [1], ir: [1] }, { fs: 100, red: [NaN], ir: [1] }]) {
    assert.equal(validateSampleFrame(baseFrame({ ppg })).ok, false, JSON.stringify(ppg));
  }
});

// ------------------------------------------------------------------- session ---

/**
 * A synthetic ECG at a given rate, sampled at 360 Hz.
 *
 * Mirrors the P/Q/R/S/T template in `dsp/synth.py` - same offsets, amplitudes and widths
 * - rather than being a hand-rolled approximation. An earlier version used a single
 * narrow Gaussian and scored a QRS energy concentration of 15.5 against a signal-quality
 * gate of 15.0, so the fixture was one rounding away from being declared unusable and the
 * test failed for a reason unrelated to what it was testing. A test signal should sit
 * comfortably inside the class it is meant to represent; this one scores 33.
 */
function qrsAt(n, bpm) {
  const fs = 360;
  const rr = (60 / bpm) * fs;
  const t = (n % rr) / fs;
  // Nearest R peak, before or after, so the P wave of the next beat is present too.
  const wrap = t > rr / fs / 2 ? t - rr / fs : t;
  const g = (offset, amp, sigma) => amp * Math.exp(-(((wrap - offset) / sigma) ** 2));
  return (
    g(-0.2, 0.12, 0.025) +    // P
    g(-0.02, -0.15, 0.008) +  // Q
    g(0, 1.0, 0.009) +        // R
    g(0.025, -0.25, 0.01) +   // S
    g(0.3, 0.3, 0.045)        // T
  );
}

function feed(session, { seconds = 12, spo2 = 96, withPpg = true, deviceSpo2 = null, bpm = 72 }) {
  const { red, ir } = synthPpg({ seconds, spo2, bpm });
  const perFrame = 32;
  const ppgPerFrame = Math.round((perFrame / 360) * FS);
  let out = null;
  let ppgAt = 0;
  for (let i = 0; i * perFrame < seconds * 360; i += 1) {
    const ppg = withPpg
      ? {
          fs: FS,
          red: Array.from(red.slice(ppgAt, ppgAt + ppgPerFrame)),
          ir: Array.from(ir.slice(ppgAt, ppgAt + ppgPerFrame)),
        }
      : undefined;
    ppgAt += ppgPerFrame;
    if (ppg && ppg.red.length === 0) break;
    const { ok, frame } = validateSampleFrame({
      v: withPpg ? 2 : 1,
      deviceId: 'dev-1',
      seq: i,
      tDevice: i * 89,
      fs: 360,
      ecg: new Array(perFrame).fill(0).map((_, k) => qrsAt(i * perFrame + k, bpm)),
      leadsOff: false,
      spo2: deviceSpo2,
      ...(ppg ? { ppg } : {}),
    });
    assert.ok(ok, 'frame should validate');
    out = session.ingest(frame);
  }
  return out;
}

test('the server derives saturation from raw light and says so', () => {
  const session = new DeviceSession('dev-1', { now: () => 1_000_000 });
  const vitals = feed(session, { spo2: 96 });
  assert.equal(vitals.spo2Source, 'server', `reason: ${session.lastSpo2Result?.reason}`);
  assert.ok(Math.abs(vitals.spo2 - 96) < 3, `SpO2 ${vitals.spo2}`);
  assert.ok(vitals.pulseBpm > 50 && vitals.pulseBpm < 100, `pulse ${vitals.pulseBpm}`);
});

test('a v1 device that computed its own value is used, and labelled as such', () => {
  const session = new DeviceSession('dev-1', { now: () => 1_000_000 });
  const vitals = feed(session, { seconds: 6, withPpg: false, deviceSpo2: 94 });
  assert.equal(vitals.spo2, 94);
  assert.equal(vitals.spo2Source, 'device');
});

test('no PPG and no device value means no number, not a stale one', () => {
  const session = new DeviceSession('dev-1', { now: () => 1_000_000 });
  const vitals = feed(session, { seconds: 6, withPpg: false, deviceSpo2: null });
  assert.equal(vitals.spo2, null);
  assert.equal(vitals.spo2Source, null);
});

// -------------------------------------------------------------- cross-check ---

test('agreeing rates are reported as agreeing', () => {
  const session = new DeviceSession('dev-1', { now: () => 1_000_000 });
  const vitals = feed(session, { spo2: 96 });
  assert.equal(vitals.rateAgreement, 'agree', `hr ${vitals.hr} vs pulse ${vitals.pulseBpm}`);
  assert.ok(vitals.pulseBpm !== null);
});

test('the cross-check branches follow the physiology', () => {
  const cross = new DeviceSession('dev-2', { now: () => 1 });
  const out = [];
  for (const [hr, pulse, expected, trusted] of [
    [72, 74, 'agree', true],
    [72, 120, 'implausible', false],
    [90, 45, 'harmonic-suspected', false],
    [110, 70, 'pulse-deficit', true],
    [null, 70, null, true],
    [72, null, null, true],
  ]) {
    const r = cross.crossCheckForTest(hr, pulse);
    out.push([hr, pulse, r.agreement, r.trustPulse]);
    assert.equal(r.agreement, expected, `hr ${hr}, pulse ${pulse}`);
    assert.equal(r.trustPulse, trusted, `hr ${hr}, pulse ${pulse}`);
  }
  assert.equal(out.length, 6);
});

test('a pulse deficit is surfaced, not hidden', () => {
  // Fewer pulses than beats is a real finding - in atrial fibrillation some beats never
  // reach the finger. Both numbers stay on screen and the disagreement is named.
  const session = new DeviceSession('dev-3', { now: () => 1 });
  const r = session.crossCheckForTest(110, 70);
  assert.equal(r.agreement, 'pulse-deficit');
  assert.equal(r.trustPulse, true, 'the pulse rate is still shown - it is the finding');
});

test('a clean tachycardia is reported, not silenced by the quality gate', async () => {
  // The failure this pins: the concentration statistic is rate-confounded, and at
  // 130 bpm a perfectly clean rhythm scored below the gate - so the monitor withheld
  // the rate, TACHYCARDIA never sustained its 10 s, and the alarm for the very rhythm
  // the monitor exists to catch never fired. Found by a person following TESTING.md,
  // not by any automated test (D-51).
  const session = new DeviceSession('tachy-1', { now: () => 1_000_000 });
  const vitals = feed(session, { seconds: 16, spo2: 96, bpm: 130 });
  assert.ok(vitals.hr !== null, `rate was withheld (quality=${vitals.signalQuality})`);
  assert.ok(Math.abs(vitals.hr - 130) < 6, `hr ${vitals.hr}`);
  assert.equal(vitals.hrClass, 'tachycardia');
});
