/**
 * Score the JavaScript SpO2 implementation against the Python design.
 *
 * `server/src/dsp/ppg.js` is a port. D-13 says a port is a claim until it is measured, so
 * this runs both on identical recordings - the fixtures carry the Python results
 * alongside the inputs - and prints the disagreement.
 *
 * The two implementations are not expected to be bit-identical: the zero-phase filter's
 * edge padding and the ordering of floating-point sums differ between numpy and
 * JavaScript. What matters is whether they would ever show a clinician a different
 * number, so the pass criteria are clinical rather than numerical: saturation within
 * 0.1 %, pulse rate within 0.5 bpm, and the same accept/refuse decision every time.
 *
 * Run: npm run validate:ppg   (from server/)
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { estimateSpo2, periodicity, pulsatile } from '../src/dsp/ppg.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = path.join(HERE, '..', '..', 'data', 'fixtures', 'ppg.json');

const TOL = { spo2: 0.1, pulseBpm: 0.5, ratio: 0.005, corr: 0.01, periodicity: 0.01 };

function load() {
  try {
    return JSON.parse(readFileSync(FIXTURE, 'utf8'));
  } catch {
    console.error(
      `fixture not found at ${FIXTURE}\n` +
        'generate it with:  python dsp/export_ppg_fixture.py',
    );
    process.exit(1);
  }
}

const diff = (a, b) => (a === null || b === null ? (a === b ? 0 : NaN) : Math.abs(a - b));
const fmt = (v, d = 2) => (v === null || v === undefined ? '   --' : v.toFixed(d));

function main() {
  const { cases } = load();
  const synthetic = cases.filter((c) => c.kind === 'synthetic');
  const bidmc = cases.filter((c) => c.kind === 'bidmc');

  let failures = 0;
  const worst = { spo2: 0, pulse: 0, ratio: 0, corr: 0 };

  console.log('\nSpO2: JavaScript port vs Python design\n');
  console.log(
    `${'case'.padEnd(20)} ${'true'.padStart(5)} ${'py'.padStart(6)} ${'js'.padStart(6)} ` +
      `${'Δspo2'.padStart(7)} ${'Δpulse'.padStart(7)} ${'Δratio'.padStart(7)}  decision`,
  );
  console.log('-'.repeat(86));

  for (const c of synthetic) {
    const got = estimateSpo2(Float64Array.from(c.red), Float64Array.from(c.ir), c.fs);
    const e = c.expected;

    const dSpo2 = diff(e.spo2Pct, got.spo2Pct);
    const dPulse = diff(e.pulseBpm, got.pulseBpm);
    const dRatio = diff(e.ratio, got.ratio);
    const dCorr = diff(e.channelCorr, got.channelCorr);

    const sameDecision = (e.spo2Pct === null) === (got.spo2Pct === null);
    const ok =
      sameDecision &&
      !(dSpo2 > TOL.spo2) &&
      !(dPulse > TOL.pulseBpm) &&
      !(dRatio > TOL.ratio) &&
      !Number.isNaN(dSpo2);
    if (!ok) failures += 1;

    if (Number.isFinite(dSpo2)) worst.spo2 = Math.max(worst.spo2, dSpo2);
    if (Number.isFinite(dPulse)) worst.pulse = Math.max(worst.pulse, dPulse);
    if (Number.isFinite(dRatio)) worst.ratio = Math.max(worst.ratio, dRatio);
    if (Number.isFinite(dCorr)) worst.corr = Math.max(worst.corr, dCorr);

    const decision = sameDecision
      ? e.spo2Pct === null
        ? 'both refuse'
        : 'both report'
      : `MISMATCH (py ${e.spo2Pct === null ? 'refuse' : 'report'})`;
    console.log(
      `${(ok ? '  ' : '! ') + c.name.padEnd(18)} ${fmt(c.trueSpo2, 0).padStart(5)} ` +
        `${fmt(e.spo2Pct, 1).padStart(6)} ${fmt(got.spo2Pct, 1).padStart(6)} ` +
        `${fmt(dSpo2, 3).padStart(7)} ${fmt(dPulse, 3).padStart(7)} ${fmt(dRatio, 4).padStart(7)}  ${decision}`,
    );
  }

  console.log('\nPulse period on real ICU recordings (BIDMC)\n');
  console.log(`${'case'.padEnd(20)} ${'py bpm'.padStart(8)} ${'js bpm'.padStart(8)} ${'Δbpm'.padStart(7)} ${'Δstrength'.padStart(10)}`);
  console.log('-'.repeat(58));
  let worstBidmc = 0;
  for (const c of bidmc) {
    const ac = pulsatile(Float64Array.from(c.signal), c.fs);
    const { periodS, strength } = periodicity(ac, c.fs);
    const jsBpm = periodS ? 60 / periodS : null;
    const dBpm = diff(c.expected.pulseBpm, jsBpm);
    const dStr = diff(c.expected.strength, strength);
    const ok = !(dBpm > TOL.pulseBpm) && !Number.isNaN(dBpm);
    if (!ok) failures += 1;
    if (Number.isFinite(dBpm)) worstBidmc = Math.max(worstBidmc, dBpm);
    console.log(
      `${(ok ? '  ' : '! ') + c.name.padEnd(18)} ${fmt(c.expected.pulseBpm, 2).padStart(8)} ` +
        `${fmt(jsBpm, 2).padStart(8)} ${fmt(dBpm, 3).padStart(7)} ${fmt(dStr, 4).padStart(10)}`,
    );
  }

  console.log('-'.repeat(86));
  console.log(
    `worst disagreement:  SpO2 ${worst.spo2.toFixed(4)} %  pulse ${worst.pulse.toFixed(4)} bpm  ` +
      `R ${worst.ratio.toFixed(5)}  red/IR corr ${worst.corr.toFixed(5)}  BIDMC pulse ${worstBidmc.toFixed(4)} bpm`,
  );
  console.log(
    `\n${failures === 0 ? 'PASS' : 'FAIL'}: ${cases.length - failures}/${cases.length} cases agree ` +
      `within the clinical tolerance (SpO2 ${TOL.spo2} %, pulse ${TOL.pulseBpm} bpm), ` +
      'and every accept/refuse decision matches.\n',
  );
  process.exit(failures === 0 ? 0 : 1);
}

main();
