/**
 * Score the streaming detector against MIT-BIH, the same way the offline one is scored.
 *
 * The offline Python detector is the validated reference (see docs/verification.md);
 * this server runs a causal port of it. "The port is equivalent" is a claim that has
 * to be measured, not asserted - so this replays the same recordings through the
 * streaming detector, in realistic frame-sized blocks, and reports sensitivity and
 * positive predictivity against the same cardiologist annotations and the same
 * ±150 ms matching window.
 *
 * Usage:
 *   node tools/validateStreaming.js            # default fixtures
 *   node tools/validateStreaming.js 100 105
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative, resolve } from 'node:path';

import { QrsStreamDetector } from '../src/dsp/qrsDetector.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const FIXTURE_DIR = join(ROOT, 'data', 'fixtures');

/** Samples per WebSocket frame - the block size the firmware will send. */
const FRAME_SAMPLES = 32;
/** Matching window used throughout the QRS-detector literature. */
const TOLERANCE_MS = 150;

/**
 * Match detections to reference beats, each reference matched at most once.
 * Mirrors `compare_to_reference` in dsp/pan_tompkins.py.
 */
export function score(detected, reference, fs, toleranceMs = TOLERANCE_MS) {
  const tol = (toleranceMs * fs) / 1000;
  const matched = new Set();
  let tp = 0;
  let errorSum = 0;

  for (const d of detected) {
    // Reference beats are sorted, so a binary search finds the neighbourhood.
    let lo = 0;
    let hi = reference.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (reference[mid] < d) lo = mid + 1;
      else hi = mid;
    }
    let best = -1;
    let bestDist = tol + 1;
    for (let j = lo - 1; j <= lo + 1; j += 1) {
      if (j >= 0 && j < reference.length && !matched.has(j)) {
        const dist = Math.abs(reference[j] - d);
        if (dist < bestDist) {
          best = j;
          bestDist = dist;
        }
      }
    }
    if (best >= 0 && bestDist <= tol) {
      matched.add(best);
      errorSum += bestDist;
      tp += 1;
    }
  }

  const fp = detected.length - tp;
  const fn = reference.length - tp;
  return {
    tp,
    fp,
    fn,
    sensitivity: tp + fn > 0 ? tp / (tp + fn) : 0,
    ppv: tp + fp > 0 ? tp / (tp + fp) : 0,
    meanErrorMs: tp > 0 ? (errorSum / tp / fs) * 1000 : 0,
  };
}

/** Replay one fixture through the streaming detector in frame-sized blocks. */
export function runFixture(record) {
  const path = join(FIXTURE_DIR, `${record}.json`);
  let fixture;
  try {
    fixture = JSON.parse(readFileSync(path, 'utf8'));
  } catch (err) {
    if (err.code === 'ENOENT') {
      throw new Error(
        `fixture ${relative(ROOT, path)} not found - run: python dsp/export_fixture.py ${record}`,
      );
    }
    throw err;
  }

  const { fs, signal, referenceBeats } = fixture;
  const detector = new QrsStreamDetector({ fs });
  const detected = [];

  const started = process.hrtime.bigint();
  for (let i = 0; i < signal.length; i += FRAME_SAMPLES) {
    const block = signal.slice(i, i + FRAME_SAMPLES);
    const { beats } = detector.push(block);
    for (const b of beats) detected.push(b);
  }
  const elapsedS = Number(process.hrtime.bigint() - started) / 1e9;

  // Beats inside the learning window cannot be detected by design; excluding that
  // window from the reference count measures the detector rather than its start-up.
  const learningEnd = detector.learningSamples;
  const reference = referenceBeats.filter((b) => b >= learningEnd);

  return {
    record,
    fs,
    durationS: signal.length / fs,
    elapsedS,
    learningBeatsExcluded: referenceBeats.length - reference.length,
    stats: detector.stats,
    ...score(detected, reference, fs),
    nReference: reference.length,
    nDetected: detected.length,
  };
}

function main() {
  const records = process.argv.slice(2);
  const targets = records.length ? records : ['100', '105', '203'];

  const pad = (v, w) => String(v).padStart(w);
  console.log(
    `${pad('rec', 5)} ${pad('beats', 7)} ${pad('Se %', 7)} ${pad('PPV %', 7)} ${pad('FP', 5)} ${pad('FN', 5)} ${pad('err ms', 7)} ${pad('xRT', 8)}`,
  );
  console.log('-'.repeat(56));

  let tp = 0;
  let fp = 0;
  let fn = 0;
  let failed = false;

  for (const rec of targets) {
    let r;
    try {
      r = runFixture(rec);
    } catch (err) {
      console.error(`${pad(rec, 5)}  SKIPPED - ${err.message}`);
      failed = true;
      continue;
    }
    tp += r.tp;
    fp += r.fp;
    fn += r.fn;
    console.log(
      `${pad(rec, 5)} ${pad(r.nReference, 7)} ${pad((r.sensitivity * 100).toFixed(2), 7)} ` +
        `${pad((r.ppv * 100).toFixed(2), 7)} ${pad(r.fp, 5)} ${pad(r.fn, 5)} ` +
        `${pad(r.meanErrorMs.toFixed(1), 7)} ${pad(Math.round(r.durationS / r.elapsedS), 7)}x`,
    );
  }

  if (tp + fn > 0) {
    console.log('-'.repeat(56));
    console.log(
      `${pad('ALL', 5)} ${pad(tp + fn, 7)} ${pad(((tp / (tp + fn)) * 100).toFixed(2), 7)} ` +
        `${pad(((tp / (tp + fp)) * 100).toFixed(2), 7)} ${pad(fp, 5)} ${pad(fn, 5)}`,
    );
  }
  process.exitCode = failed ? 1 : 0;
}

// Compare resolved paths, not URL strings: import.meta.url percent-encodes
// characters that are legal in a path (a space becomes %20), so the common
// `import.meta.url === 'file://' + process.argv[1]` idiom silently fails for anyone
// whose checkout sits in a directory with a space in its name.
if (process.argv[1] && resolve(fileURLToPath(import.meta.url)) === resolve(process.argv[1])) {
  main();
}
