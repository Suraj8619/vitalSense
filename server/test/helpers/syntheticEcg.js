/**
 * Minimal synthetic ECG for JavaScript tests.
 *
 * A deliberately small mirror of `dsp/synth.py` - same sum-of-Gaussians morphology,
 * same idea of returning known R-peak positions - so the streaming detector can be
 * tested against exact ground truth without loading a 5 MB clinical fixture.
 */

/** [offset from R peak (s), amplitude (mV), width (s)] */
const TEMPLATE = [
  [-0.2, 0.12, 0.025], // P
  [-0.02, -0.15, 0.008], // Q
  [0.0, 1.0, 0.009], // R
  [0.025, -0.25, 0.01], // S
  [0.3, 0.3, 0.045], // T
];

/** Deterministic pseudo-random generator, so a failing test always fails the same way. */
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Box-Muller normal deviate from a uniform generator. */
function normal(rand) {
  const u = Math.max(rand(), Number.EPSILON);
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rand());
}

/**
 * @param {object} [opts]
 * @param {number} [opts.durationS]
 * @param {number} [opts.fs]
 * @param {number} [opts.hrBpm]
 * @param {number} [opts.noiseStd] white noise, mV RMS
 * @param {number} [opts.mainsAmp] 50 Hz interference, mV
 * @param {number} [opts.wanderAmp] baseline drift, mV
 * @param {number} [opts.dcOffset] electrode offset, mV
 * @param {number} [opts.seed]
 * @returns {{signal: Float64Array, rPeaks: number[], fs: number}}
 */
export function syntheticEcg({
  durationS = 30,
  fs = 360,
  hrBpm = 72,
  noiseStd = 0,
  mainsAmp = 0,
  wanderAmp = 0,
  dcOffset = 0,
  seed = 1,
} = {}) {
  const n = Math.round(durationS * fs);
  const signal = new Float64Array(n);
  const rand = mulberry32(seed);
  const rr = 60 / hrBpm;

  const peakTimes = [];
  for (let t = rr * 0.5; t < durationS - 0.4; t += rr) peakTimes.push(t);

  for (const pt of peakTimes) {
    for (const [offset, amp, sigma] of TEMPLATE) {
      const centre = pt + offset;
      const lo = Math.max(0, Math.floor((centre - 4 * sigma) * fs));
      const hi = Math.min(n, Math.floor((centre + 4 * sigma) * fs) + 1);
      for (let i = lo; i < hi; i += 1) {
        const dt = i / fs - centre;
        signal[i] += amp * Math.exp(-0.5 * (dt / sigma) ** 2);
      }
    }
  }

  for (let i = 0; i < n; i += 1) {
    const t = i / fs;
    if (wanderAmp) signal[i] += wanderAmp * Math.sin(2 * Math.PI * 0.15 * t);
    if (mainsAmp) signal[i] += mainsAmp * Math.sin(2 * Math.PI * 50 * t);
    if (noiseStd) signal[i] += normal(rand) * noiseStd;
    if (dcOffset) signal[i] += dcOffset;
  }

  return { signal, rPeaks: peakTimes.map((t) => Math.round(t * fs)), fs };
}
