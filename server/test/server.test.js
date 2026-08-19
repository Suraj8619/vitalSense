/**
 * Protocol, alarm and session tests.
 *
 * The alarm tests drive a fake clock rather than sleeping, so the specified sustain
 * times are asserted exactly and the suite stays fast and deterministic. Timing that
 * a test cannot check precisely is timing that drifts.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { AlarmEngine, RULES } from '../src/alarms.js';
import { LIMITS, makeVitalsFrame, PROTOCOL_VERSION, validateSampleFrame } from '../src/protocol.js';
import { DeviceSession } from '../src/session.js';
import { SAMPLE_RATE_HZ } from '../src/dsp/coefficients.js';
import { syntheticEcg } from './helpers/syntheticEcg.js';

const FS = SAMPLE_RATE_HZ;

/** A well-formed device frame, with overrides applied. */
const frame = (over = {}) => ({
  v: PROTOCOL_VERSION,
  deviceId: 'test-01',
  seq: 0,
  tDevice: 0,
  fs: FS,
  ecg: new Array(32).fill(0.1),
  spo2: 97,
  temp: 36.8,
  leadsOff: false,
  battery: 3.9,
  ...over,
});

// --- protocol -------------------------------------------------------------------

test('accepts a well-formed frame', () => {
  const { ok, errors, frame: parsed } = validateSampleFrame(frame());
  assert.equal(ok, true, errors.join('; '));
  assert.equal(parsed.deviceId, 'test-01');
  assert.equal(parsed.ecg.length, 32);
});

test('rejects an unknown protocol version without parsing the rest', () => {
  const { ok, errors } = validateSampleFrame(frame({ v: 99, deviceId: 12345 }));
  assert.equal(ok, false);
  assert.equal(errors.length, 1, 'version failure should short-circuit, not cascade');
  assert.match(errors[0], /unsupported protocol version/);
});

test('rejects structurally invalid frames', () => {
  const cases = [
    [null, /must be a JSON object/],
    ['a string', /must be a JSON object/],
    [frame({ deviceId: '' }), /deviceId/],
    [frame({ seq: -1 }), /seq/],
    [frame({ seq: 1.5 }), /seq/],
    [frame({ tDevice: 'soon' }), /tDevice/],
    [frame({ fs: 250 }), /does not match the configured/],
    [frame({ ecg: 'not an array' }), /ecg must be an array/],
    [frame({ ecg: [] }), /ecg length/],
    [frame({ ecg: new Array(LIMITS.maxSamplesPerFrame + 1).fill(0) }), /ecg length/],
    [frame({ ecg: [NaN, NaN] }), /every ECG sample/],
    [frame({ leadsOff: 'no' }), /leadsOff/],
  ];
  for (const [input, pattern] of cases) {
    const { ok, errors } = validateSampleFrame(input);
    assert.equal(ok, false, `should have rejected ${JSON.stringify(input).slice(0, 60)}`);
    assert.ok(errors.some((e) => pattern.test(e)), `expected ${pattern}, got ${errors.join('; ')}`);
  }
});

test('a few bad samples warn but do not discard the whole frame', () => {
  const ecg = new Array(32).fill(0.1);
  ecg[5] = NaN;
  const { ok, warnings } = validateSampleFrame(frame({ ecg }));
  assert.equal(ok, true, 'one bad sample should not lose the 31 good ones');
  assert.ok(warnings.some((w) => /non-finite/.test(w)));
});

test('out-of-range sensor readings are discarded, not reported', () => {
  const { ok, frame: parsed, warnings } = validateSampleFrame(frame({ spo2: 300, temp: 99 }));
  assert.equal(ok, true);
  assert.equal(parsed.spo2, null, 'a 300% SpO2 must not reach the dashboard');
  assert.equal(parsed.temp, null);
  assert.equal(warnings.length, 2);
});

test('vitals frames always carry alarms and an explicit hr', () => {
  const v = makeVitalsFrame({
    deviceId: 'd', tServer: 1, sampleIndex: 0, ecg: [0.123456], beats: [],
    hr: null, hrClass: 'insufficient data', spo2: null, temp: null,
    signalQuality: 'unusable', alarms: [], stats: {},
  });
  assert.deepEqual(v.alarms, [], 'alarms must be present even when empty');
  assert.equal(v.hr, null, 'unknown heart rate is null, never omitted');
  assert.equal(v.ecg[0], 0.1235, 'waveform is rounded for transport');
});

// --- alarm engine ---------------------------------------------------------------

/** Baseline context in which no alarm should fire. */
const calm = {
  leadsOff: false,
  hr: 72,
  hrClass: 'normal',
  spo2: 97,
  signalQuality: 'good',
  secondsSinceLastBeat: 0.5,
  secondsSinceLastFrame: 0,
};

test('no alarms fire on a healthy patient', () => {
  const engine = new AlarmEngine();
  assert.deepEqual(engine.update(calm, 1000), []);
});

test('alarms require their condition to be sustained', () => {
  const engine = new AlarmEngine();
  const tachy = { ...calm, hrClass: 'tachycardia', hr: 130 };
  const rule = RULES.find((r) => r.code === 'TACHYCARDIA');

  assert.deepEqual(engine.update(tachy, 0), [], 'must not fire immediately');
  assert.deepEqual(engine.update(tachy, rule.sustainMs - 1), [], 'must not fire early');

  const fired = engine.update(tachy, rule.sustainMs);
  assert.equal(fired.length, 1);
  assert.equal(fired[0].code, 'TACHYCARDIA');
  assert.equal(fired[0].since, 0, 'reports when the condition began, not when it fired');
});

test('an alarm clears as soon as its condition ends, and re-arms afterwards', () => {
  const engine = new AlarmEngine();
  const tachy = { ...calm, hrClass: 'tachycardia' };
  engine.update(tachy, 0);
  assert.equal(engine.update(tachy, 10000).length, 1);
  assert.deepEqual(engine.update(calm, 10500), [], 'clears when the rate normalises');

  // A recurrence must wait out the full sustain again, not resume where it left off.
  engine.update(tachy, 11000);
  assert.deepEqual(engine.update(tachy, 15000), [], 'sustain restarts on recurrence');
  assert.equal(engine.update(tachy, 21000).length, 1);
});

test('leads-off alarm fits inside the 2 s requirement budget', () => {
  const engine = new AlarmEngine();
  const off = { ...calm, leadsOff: true, signalQuality: 'unusable' };
  const rule = RULES.find((r) => r.code === 'LEADS_OFF');

  assert.ok(rule.sustainMs < 2000, 'debounce must leave margin inside the 2 s budget (SR-03)');
  engine.update(off, 0);
  assert.deepEqual(engine.update(off, rule.sustainMs - 1), []);
  const fired = engine.update(off, rule.sustainMs);
  assert.equal(fired[0].code, 'LEADS_OFF');
});

test('asystole is suppressed when the electrodes are off or the signal is unusable', () => {
  const engine = new AlarmEngine();
  const silent = { ...calm, secondsSinceLastBeat: 10 };

  const leadsOff = engine.update({ ...silent, leadsOff: true }, 5000);
  assert.ok(!leadsOff.some((a) => a.code === 'ASYSTOLE'), 'a detached lead is not a cardiac arrest');

  engine.reset();
  const unusable = engine.update({ ...silent, signalQuality: 'unusable' }, 5000);
  assert.ok(!unusable.some((a) => a.code === 'ASYSTOLE'));

  engine.reset();
  const real = engine.update(silent, 5000);
  assert.ok(real.some((a) => a.code === 'ASYSTOLE'), 'genuine silence with good signal must alarm');
});

test('signal-poor is suppressed while the device is silent', () => {
  const engine = new AlarmEngine();
  const dead = { ...calm, signalQuality: 'unusable', secondsSinceLastFrame: 12 };
  const codes = engine.update(dead, 20000).map((a) => a.code);
  assert.ok(codes.includes('DEVICE_SILENT'));
  assert.ok(!codes.includes('SIGNAL_POOR'), 'one alarm for one fault, not two');
});

test('alarms are ordered most severe first', () => {
  const engine = new AlarmEngine();
  const bad = {
    ...calm,
    leadsOff: true,
    hrClass: 'tachycardia',
    spo2: 80,
    signalQuality: 'unusable',
    secondsSinceLastFrame: 0,
  };
  engine.update(bad, 0);
  const active = engine.update(bad, 20000);
  const severities = active.map((a) => a.severity);
  assert.deepEqual([...severities].sort((a, b) => (a === 'high' ? -1 : 1)), severities);
  assert.equal(active[0].severity, 'high');
});

test('a rule that throws does not take the other alarms down', () => {
  const engine = new AlarmEngine({
    rules: [
      { code: 'BOOM', severity: 'high', sustainMs: 0, message: 'x', test: () => { throw new Error('bad rule'); } },
      { code: 'FINE', severity: 'low', sustainMs: 0, message: 'y', test: () => true },
    ],
  });
  const active = engine.update(calm, 0);
  assert.deepEqual(active.map((a) => a.code), ['FINE']);
});

// --- session integration --------------------------------------------------------

/** Feed a synthetic recording through a session, one frame at a time. */
function runSession(options = {}) {
  const { hrBpm = 72, seconds = 30, leadsOffFrom = null, leadsOffTo = null, dropFrames = [] } = options;
  const { signal } = syntheticEcg({ durationS: seconds, hrBpm, noiseStd: 0.02 });

  let clock = 1_000_000;
  const session = new DeviceSession('test-01', { fs: FS, now: () => clock });
  const outputs = [];

  const perFrame = 32;
  let seq = 0;
  for (let i = 0; i + perFrame <= signal.length; i += perFrame) {
    const tSeconds = i / FS;
    if (dropFrames.includes(seq)) {
      seq += 1; // simulate a frame lost in flight: seq advances, nothing is sent
      clock += (perFrame / FS) * 1000;
      continue;
    }
    const off = leadsOffFrom !== null && tSeconds >= leadsOffFrom && tSeconds < leadsOffTo;
    outputs.push(
      session.ingest({
        v: 1,
        deviceId: 'test-01',
        seq,
        tDevice: Math.round(tSeconds * 1000),
        fs: FS,
        ecg: off ? new Array(perFrame).fill(1.8) : Array.from(signal.slice(i, i + perFrame)),
        spo2: off ? null : 97,
        temp: 36.8,
        leadsOff: off,
        battery: 3.9,
      }),
    );
    seq += 1;
    clock += (perFrame / FS) * 1000;
  }
  return { session, outputs };
}

test('session derives the correct heart rate end to end', () => {
  const { outputs } = runSession({ hrBpm: 72, seconds: 40 });
  const settled = outputs.slice(Math.floor(outputs.length / 2));
  const rates = settled.map((o) => o.hr).filter((v) => v !== null);

  assert.ok(rates.length > 0, 'a heart rate should be reported once settled');
  const median = rates.sort((a, b) => a - b)[Math.floor(rates.length / 2)];
  assert.ok(Math.abs(median - 72) <= 5, `median reported rate ${median} should be within 5 bpm of 72`);
  assert.ok(settled.every((o) => o.hrClass === 'normal'), 'a 72 bpm rhythm is normal');
});

test('session reports no heart rate at all before it has data', () => {
  const { outputs } = runSession({ seconds: 5 });
  assert.equal(outputs[0].hr, null, 'first frame cannot know a heart rate');
  assert.deepEqual(outputs[0].alarms, [], 'and must not alarm about not knowing it');
});

test('detaching a lead stops the heart rate rather than freezing it', () => {
  const { outputs } = runSession({ seconds: 40, leadsOffFrom: 20, leadsOffTo: 35 });
  const duringDetach = outputs.filter((o) => o.signalQuality === 'unusable');

  assert.ok(duringDetach.length > 0, 'detachment should be visible in signal quality');
  assert.ok(
    duringDetach.every((o) => o.hr === null),
    'a stale heart rate must not remain on screen after the lead is pulled',
  );
  assert.ok(
    outputs.some((o) => o.alarms.some((a) => a.code === 'LEADS_OFF')),
    'LEADS_OFF should fire',
  );
});

test('dropped frames are counted, not silently absorbed', () => {
  const { outputs } = runSession({ seconds: 20, dropFrames: [10, 11, 12, 50] });
  const last = outputs[outputs.length - 1];
  assert.equal(last.stats.framesDropped, 4, 'seq gaps are the only evidence data was lost');
});

test('beats are absolute indices that line up with the waveform stream', () => {
  const { outputs } = runSession({ seconds: 30 });
  const allBeats = outputs.flatMap((o) => o.beats);

  assert.ok(allBeats.length > 20, `expected many beats, got ${allBeats.length}`);
  // Roughly one beat per second at 72 bpm, over ~28 usable seconds.
  assert.ok(allBeats.length > 25, 'nearly every beat should reach the dashboard');

  for (let i = 1; i < allBeats.length; i += 1) {
    assert.ok(allBeats[i] > allBeats[i - 1], 'beat indices must increase monotonically');
  }

  const last = outputs[outputs.length - 1];
  assert.equal(
    last.sampleIndex + last.ecg.length,
    outputs.reduce((n, o) => n + o.ecg.length, 0),
    'sampleIndex must track the total samples streamed',
  );
});

test('reconnecting an electrode does not fabricate a heart rate', () => {
  // Regression: the reconnection step drove a transient through the filters while
  // the adaptive thresholds were still tuned to the flat-line period, and the
  // resulting burst of detections was displayed as 190 bpm "tachycardia" with the
  // signal marked good - on a patient whose lead had just been reattached.
  const { outputs } = runSession({ hrBpm: 72, seconds: 60, leadsOffFrom: 20, leadsOffTo: 30 });

  const framesPerSecond = FS / 32;
  const reattachIndex = Math.round(30 * framesPerSecond);
  // Sampled with margin inside the 3 s settle window rather than up to its exact
  // boundary frame - the guarantee under test is "no rate while settling", not the
  // sample on which settling ends.
  const settling = outputs.slice(reattachIndex + 1, reattachIndex + Math.floor(2.5 * framesPerSecond));

  assert.ok(settling.length > 20, 'settle window should span many frames');
  assert.ok(
    settling.every((o) => o.hr === null),
    'no heart rate may be reported while the front end settles after reconnection',
  );
  assert.ok(
    settling.every((o) => o.signalQuality === 'unusable'),
    'settling signal must be marked unusable, not good',
  );

  // ...and once settled, the true rate comes back.
  const recovered = outputs.slice(reattachIndex + Math.round(12 * framesPerSecond));
  const rates = recovered.map((o) => o.hr).filter((v) => v !== null);
  assert.ok(rates.length > 0, 'the monitor must recover a heart rate after settling');
  const median = rates.sort((a, b) => a - b)[Math.floor(rates.length / 2)];
  assert.ok(Math.abs(median - 72) <= 6, `recovered rate ${median} should return to ~72 bpm`);
  assert.ok(
    recovered.every((o) => o.hr === null || o.hr < 130),
    'no fabricated tachycardia after recovery',
  );
});
