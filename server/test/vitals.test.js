/**
 * Tests for heart-rate derivation.
 *
 * These mirror the checks in `dsp/vitals.py`, because the two implementations must
 * agree on what a heart rate is - the dashboard and the verification report would
 * otherwise be describing different systems.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  classifyRate,
  DEFAULT_LIMITS,
  heartRate,
  MAX_PLAUSIBLE_RR_S,
  RollingHeartRate,
  rrIntervals,
} from '../src/dsp/vitals.js';

const FS = 360;

/** Evenly spaced beats at a given rate. */
const beatsAt = (bpm, count) =>
  Array.from({ length: count }, (_, i) => Math.round((i * 60 * FS) / bpm));

test('recovers exact heart rates', () => {
  for (const bpm of [45, 60, 72, 100, 180]) {
    const hr = heartRate(beatsAt(bpm, 40), FS);
    assert.ok(Math.abs(hr.bpm - bpm) < 0.5, `${bpm} bpm -> ${hr.bpm?.toFixed(2)}`);
    assert.equal(hr.trustworthy, true);
  }
});

test('classification boundaries match the documented thresholds', () => {
  assert.equal(classifyRate(50), 'bradycardia');
  assert.equal(classifyRate(59.9), 'bradycardia');
  assert.equal(classifyRate(DEFAULT_LIMITS.brady), 'normal', 'the threshold itself is normal');
  assert.equal(classifyRate(80), 'normal');
  assert.equal(classifyRate(DEFAULT_LIMITS.tachy), 'normal');
  assert.equal(classifyRate(100.1), 'tachycardia');
  assert.equal(classifyRate(null), 'insufficient data');
  assert.equal(classifyRate(NaN), 'insufficient data');
  assert.equal(classifyRate(0), 'insufficient data');
});

test('a spurious extra detection does not move the reported rate', () => {
  const beats = beatsAt(60, 20);
  beats.splice(10, 0, beats[9] + 20); // 55 ms after a beat - physiologically impossible
  const hr = heartRate(beats.sort((a, b) => a - b), FS);
  assert.ok(hr.nRejected >= 1, 'implausible interval should be rejected');
  assert.ok(Math.abs(hr.bpm - 60) < 1, `rate should stay ~60, got ${hr.bpm.toFixed(2)}`);
});

test('a missed beat does not move the reported rate', () => {
  const beats = beatsAt(60, 30);
  beats.splice(15, 1);
  const hr = heartRate(beats, FS);
  assert.ok(Math.abs(hr.bpm - 60) < 1, `rate should stay ~60, got ${hr.bpm.toFixed(2)}`);
});

test('mostly-implausible intervals are reported as untrustworthy, not as a number', () => {
  // Detections scattered at impossible spacings: a rate here would be fiction.
  const junk = [0, 10, 20, 30, 40, 50, 60, 70];
  const hr = heartRate(junk, FS);
  assert.equal(hr.trustworthy, false);
  assert.equal(hr.classification, 'insufficient data');
});

test('HRV metrics separate regular from irregular rhythms', () => {
  const steady = heartRate(beatsAt(60, 50), FS);
  assert.ok(steady.sdnnMs < 1, `regular rhythm SDNN should be ~0, got ${steady.sdnnMs}`);
  assert.ok(steady.rmssdMs < 1, `regular rhythm RMSSD should be ~0, got ${steady.rmssdMs}`);

  let t = 0;
  const jittered = Array.from({ length: 200 }, (_, i) => {
    t += 360 + 40 * Math.sin(i * 2.4); // deterministic jitter around 60 bpm
    return Math.round(t);
  });
  const irregular = heartRate(jittered, FS);
  assert.ok(irregular.sdnnMs > 40, `irregular rhythm SDNN should be large, got ${irregular.sdnnMs}`);
});

test('degenerate inputs return "insufficient data" rather than throwing', () => {
  for (const beats of [[], [100]]) {
    const hr = heartRate(beats, FS);
    assert.equal(hr.bpm, null);
    assert.equal(hr.classification, 'insufficient data');
    assert.equal(hr.trustworthy, false);
  }
  assert.throws(() => rrIntervals([0, 100], 0), RangeError);
});

test('RollingHeartRate forgets beats that leave the window', () => {
  const rolling = new RollingHeartRate(FS, 10);
  assert.throws(() => new RollingHeartRate(FS, 0), RangeError);

  const beats = beatsAt(72, 200);
  const last = beats[beats.length - 1];
  rolling.add(beats, last);

  // A 10 s window at 72 bpm holds ~12 beats, not 200.
  assert.ok(rolling.beats.length < 20, `window should hold ~12 beats, holds ${rolling.beats.length}`);
  const hr = rolling.current();
  assert.ok(Math.abs(hr.bpm - 72) < 1, `windowed rate ${hr.bpm?.toFixed(2)} should be ~72`);
});

test('RollingHeartRate reports the gap since the last beat', () => {
  const rolling = new RollingHeartRate(FS, 10);
  assert.equal(
    rolling.samplesSinceLastBeat(1000),
    1000,
    'before any beat, count from session start - Infinity would alarm asystole immediately',
  );

  rolling.add([500], 500);
  assert.equal(rolling.samplesSinceLastBeat(1000), 500);

  // After a long silence the window empties and the rate becomes unknown - the
  // input the asystole alarm depends on.
  rolling.add([], 500 + MAX_PLAUSIBLE_RR_S * FS * 10);
  assert.equal(rolling.beats.length, 0);
  assert.equal(rolling.current().classification, 'insufficient data');
});

test('a restart does not charge the interruption to the patient', () => {
  // After electrodes come off for ten seconds and the front end settles for three, the
  // time since the last beat is already thirteen seconds - so asystole would fire the
  // instant signal quality returned, before the patient had any chance to produce a
  // beat the system was willing to look at.
  const fs = 360;
  const rolling = new RollingHeartRate(fs, 10);
  rolling.add([0, 360, 720], 720);
  assert.equal(rolling.samplesSinceLastBeat(720), 0);

  // Ten seconds of nothing, then acquisition restarts.
  const afterGap = 720 + 10 * fs;
  assert.equal(rolling.samplesSinceLastBeat(afterGap) / fs, 10, 'the gap is real while it lasts');

  rolling.restart(afterGap);
  assert.equal(rolling.samplesSinceLastBeat(afterGap), 0, 'the clock starts again at the restart');
  assert.equal(
    rolling.samplesSinceLastBeat(afterGap + 2 * fs) / fs, 2,
    'and counts only the silence since',
  );
  assert.equal(rolling.current().bpm, null, 'beats from before the interruption are forgotten');
});
