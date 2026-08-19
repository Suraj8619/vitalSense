/**
 * Tests for the streaming DSP building blocks.
 *
 * The properties asserted here are the ones the rest of the system depends on:
 * filtering must not depend on how samples happen to be grouped into frames, buffers
 * must not lie about what they still hold, and the detector must not invent beats
 * when there is no patient attached.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { Biquad, MovingAverage, RingBuffer, SosCascade } from '../src/dsp/biquad.js';
import { DISPLAY_SOS, NOTCH, SAMPLE_RATE_HZ } from '../src/dsp/coefficients.js';
import { QrsStreamDetector } from '../src/dsp/qrsDetector.js';
import { syntheticEcg } from './helpers/syntheticEcg.js';

const FS = SAMPLE_RATE_HZ;

/** RMS amplitude of a tone at `freq` after passing through `makeFilter()`. */
function toneResponse(makeFilter, freq, fs = FS, seconds = 4) {
  const filter = makeFilter();
  const n = Math.round(seconds * fs);
  let sumSq = 0;
  let counted = 0;
  for (let i = 0; i < n; i += 1) {
    const y = filter.processSample(Math.sin((2 * Math.PI * freq * i) / fs));
    // Skip the first second so the measurement reflects steady state, not settling.
    if (i > fs) {
      sumSq += y * y;
      counted += 1;
    }
  }
  return Math.sqrt(sumSq / counted) / Math.SQRT1_2; // normalise against unit-amplitude input
}

test('Biquad rejects malformed coefficients', () => {
  assert.throws(() => new Biquad([1, 2, 3]), TypeError);
  assert.throws(() => new Biquad([1, 0, 0, 0, 0, 0]), RangeError);
  assert.throws(() => new SosCascade([]), TypeError);
});

test('notch attenuates 50 Hz and passes the QRS band', () => {
  const make = () => new SosCascade([[...NOTCH.b, ...NOTCH.a]]);
  const at50 = toneResponse(make, 50);
  const at15 = toneResponse(make, 15);
  assert.ok(at50 < 0.05, `50 Hz should be attenuated >26 dB, got gain ${at50.toFixed(4)}`);
  assert.ok(at15 > 0.95, `15 Hz should pass, got gain ${at15.toFixed(4)}`);
});

test('display bandpass rejects drift and EMG, passes the QRS band', () => {
  const make = () => new SosCascade(DISPLAY_SOS);
  assert.ok(toneResponse(make, 0.1, FS, 40) < 0.1, 'baseline wander should be attenuated');
  assert.ok(toneResponse(make, 15) > 0.9, '15 Hz should pass');
  assert.ok(toneResponse(make, 100) < 0.1, '100 Hz EMG should be attenuated');
});

test('DC priming removes the electrode-offset transient', () => {
  // A large constant offset through a bandpass should produce ~0, immediately.
  const primed = new SosCascade(DISPLAY_SOS);
  const out = primed.process(new Float64Array(200).fill(2.5));
  const worst = Math.max(...Array.from(out, Math.abs));
  assert.ok(worst < 1e-6, `primed cascade should not ring on constant input, peak ${worst}`);

  // Without priming the same input rings substantially - this is what priming fixes.
  const raw = new SosCascade(DISPLAY_SOS);
  raw.primed = true; // skip auto-priming to demonstrate the difference
  const rawOut = raw.process(new Float64Array(200).fill(2.5));
  assert.ok(
    Math.max(...Array.from(rawOut, Math.abs)) > 0.01,
    'unprimed cascade should show a startup transient',
  );
});

test('filtering does not depend on how samples are grouped into frames', () => {
  const { signal } = syntheticEcg({ durationS: 20, noiseStd: 0.05, dcOffset: 0.5 });

  const whole = new SosCascade(DISPLAY_SOS).process(signal);

  for (const chunk of [1, 7, 32, 256]) {
    const cascade = new SosCascade(DISPLAY_SOS);
    const pieces = [];
    for (let i = 0; i < signal.length; i += chunk) {
      pieces.push(cascade.process(signal.slice(i, i + chunk)));
    }
    const joined = Float64Array.from(pieces.flatMap((p) => Array.from(p)));
    let maxDiff = 0;
    for (let i = 0; i < whole.length; i += 1) maxDiff = Math.max(maxDiff, Math.abs(whole[i] - joined[i]));
    assert.ok(maxDiff < 1e-12, `chunk size ${chunk} changed the output by ${maxDiff}`);
  }
});

test('RingBuffer wraps and reports what it still holds', () => {
  const rb = new RingBuffer(4);
  assert.throws(() => new RingBuffer(0), RangeError);

  for (let i = 0; i < 4; i += 1) rb.push(i * 10);
  assert.equal(rb.at(0), 0);
  assert.equal(rb.at(3), 30);
  assert.equal(rb.oldestIndex, 0);

  rb.push(40); // evicts index 0
  assert.equal(rb.oldestIndex, 1);
  assert.equal(rb.has(0), false);
  assert.throws(() => rb.at(0), RangeError, 'evicted samples must throw, not return stale data');
  assert.equal(rb.at(4), 40);
  assert.deepEqual(Array.from(rb.slice(2, 5)), [20, 30, 40]);
  assert.deepEqual(Array.from(rb.slice(0, 5)), [10, 20, 30, 40], 'slice clamps to retained range');
});

test('MovingAverage matches a naive recomputation', () => {
  const width = 9;
  const ma = new MovingAverage(width);
  const values = Array.from({ length: 200 }, (_, i) => Math.sin(i / 3) + i / 100);

  values.forEach((v, i) => {
    const got = ma.process(v);
    const window = values.slice(Math.max(0, i - width + 1), i + 1);
    const want = window.reduce((a, b) => a + b, 0) / window.length;
    assert.ok(Math.abs(got - want) < 1e-9, `sample ${i}: got ${got}, want ${want}`);
  });
});

test('detector refuses a sampling rate its coefficients were not designed for', () => {
  assert.throws(() => new QrsStreamDetector({ fs: 250 }), RangeError);
  assert.throws(() => new QrsStreamDetector({ fs: 0 }), RangeError);
});

test('detector finds every beat in a clean synthetic ECG', () => {
  const { signal, rPeaks, fs } = syntheticEcg({ durationS: 60, hrBpm: 72 });
  const det = new QrsStreamDetector({ fs });

  const found = [];
  for (let i = 0; i < signal.length; i += 32) {
    found.push(...det.push(signal.slice(i, i + 32)).beats);
  }

  // Beats inside the learning window cannot be detected by design.
  const expected = rPeaks.filter((p) => p >= det.learningSamples);
  assert.ok(
    found.length >= expected.length - 1 && found.length <= expected.length + 1,
    `expected ~${expected.length} beats, found ${found.length}`,
  );

  const tolerance = 0.15 * fs;
  for (const truth of expected) {
    const nearest = found.reduce((best, f) => Math.min(best, Math.abs(f - truth)), Infinity);
    assert.ok(nearest <= tolerance, `no detection within 150 ms of beat at sample ${truth}`);
  }
});

test('detector survives noise, mains and baseline wander together', () => {
  const { signal, rPeaks, fs } = syntheticEcg({
    durationS: 60,
    hrBpm: 72,
    noiseStd: 0.05,
    mainsAmp: 0.4,
    wanderAmp: 1.0,
    dcOffset: 0.7,
    seed: 5,
  });
  const det = new QrsStreamDetector({ fs });
  const found = [];
  for (let i = 0; i < signal.length; i += 32) found.push(...det.push(signal.slice(i, i + 32)).beats);

  const expected = rPeaks.filter((p) => p >= det.learningSamples);
  const matched = expected.filter((truth) => found.some((f) => Math.abs(f - truth) <= 0.15 * fs));
  const sensitivity = matched.length / expected.length;
  assert.ok(sensitivity > 0.97, `sensitivity ${(sensitivity * 100).toFixed(1)}% under combined noise`);
});

test('detection is independent of frame size', () => {
  const { signal, fs } = syntheticEcg({ durationS: 30, noiseStd: 0.03, seed: 9 });

  const runWith = (chunk) => {
    const det = new QrsStreamDetector({ fs });
    const beats = [];
    for (let i = 0; i < signal.length; i += chunk) beats.push(...det.push(signal.slice(i, i + chunk)).beats);
    return beats;
  };

  const reference = runWith(32);
  for (const chunk of [1, 16, 90, 512]) {
    assert.deepEqual(runWith(chunk), reference, `frame size ${chunk} changed the detected beats`);
  }
});

test('detector reports no beats when the lead is dead', () => {
  const cases = {
    'flat zero': new Float64Array(FS * 30),
    'constant DC': new Float64Array(FS * 30).fill(5),
    'microvolt noise': Float64Array.from({ length: FS * 30 }, (_, i) => 0.002 * Math.sin(i / 5)),
  };

  for (const [label, signal] of Object.entries(cases)) {
    const det = new QrsStreamDetector();
    const beats = [];
    for (let i = 0; i < signal.length; i += 32) beats.push(...det.push(signal.slice(i, i + 32)).beats);
    assert.equal(beats.length, 0, `${label} should produce no beats, got ${beats.length}`);
  }
});

test('a NaN sample does not poison the filter state', () => {
  const { signal, fs } = syntheticEcg({ durationS: 30, hrBpm: 72 });
  const corrupted = Float64Array.from(signal);
  corrupted[5000] = NaN;
  corrupted[5001] = Infinity;

  const det = new QrsStreamDetector({ fs });
  const cleanedAll = [];
  const beats = [];
  for (let i = 0; i < corrupted.length; i += 32) {
    const out = det.push(corrupted.slice(i, i + 32));
    cleanedAll.push(...out.cleaned);
    beats.push(...out.beats);
  }

  assert.ok(cleanedAll.every(Number.isFinite), 'output must stay finite after a NaN input');
  assert.ok(beats.length > 20, `detection should continue after the glitch, got ${beats.length} beats`);
});

test('detector output offsets line up with the returned waveform', () => {
  const { signal, fs } = syntheticEcg({ durationS: 20, hrBpm: 72 });
  const det = new QrsStreamDetector({ fs });
  let absolute = 0;
  for (let i = 0; i < signal.length; i += 32) {
    const block = signal.slice(i, i + 32);
    const { cleaned, beats, beatOffsets } = det.push(block);
    assert.equal(cleaned.length, block.length, 'one cleaned sample per input sample');
    beats.forEach((b, k) => assert.equal(beatOffsets[k], b - absolute, 'offset must be beat - blockStart'));
    absolute += block.length;
  }
});
