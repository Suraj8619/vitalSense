/**
 * Pulse oximetry: SpO2, pulse rate and perfusion from red and infrared PPG.
 *
 * A port of `dsp/ppg.py`, which is the design of record. Every constant here comes from
 * `coefficients.js`, generated from the Python module, so the two cannot drift apart by
 * someone editing one of them (D-12). Whether they *behave* the same is not assumed
 * either - `server/tools/validatePpg.js` scores this implementation against the Python
 * one on the same recordings, the way the QRS port was scored in Phase 4a (D-13).
 *
 * The algorithm, briefly: oxygenated and deoxygenated haemoglobin absorb red and
 * infrared light differently, so the ratio of pulsatile to steady signal in each channel
 * (AC/DC), divided one by the other, depends on saturation and almost nothing else -
 * not skin tone, not finger thickness, not LED brightness. See the Python module for the
 * full explanation and for what this can and cannot validate.
 *
 * Unlike the ECG path this is *not* sample-by-sample streaming. Saturation is a
 * whole-window measurement: it needs several beats to average over, and the pulse period
 * comes from an autocorrelation across the window. The server buffers PPG and calls
 * `estimateSpo2` on the buffer.
 */
import { PPG_BANDPASS_SOS, PPG_PARAMS } from './coefficients.js';
import { SosCascade } from './biquad.js';

const P = PPG_PARAMS;

/** Sample rates for which coefficients were generated. */
export const SUPPORTED_RATES = Object.keys(PPG_BANDPASS_SOS).map(Number).sort((a, b) => a - b);

/**
 * Zero-phase band-pass, matching scipy's `sosfiltfilt`.
 *
 * Forward then backward, so the group delay cancels. That matters here: the AC amplitude
 * is measured inside a window bounded by two detected peaks, and a filter delay would
 * slide the window off the beat it is supposed to describe.
 *
 * The odd-symmetric padding reproduces scipy's default `padtype`. Edge handling is the
 * one place a port like this usually diverges, so it is written out explicitly rather
 * than left to whatever happens at the array boundary.
 */
export function sosfiltfilt(sos, x) {
  const n = x.length;
  const padlen = Math.min(3 * (2 * sos.length + 1), n - 1);
  if (padlen < 1) return Float64Array.from(x);

  const padded = new Float64Array(n + 2 * padlen);
  // Odd extension: reflect about the endpoint value, so a signal with a trend does not
  // acquire a step at the edge.
  for (let i = 0; i < padlen; i += 1) padded[i] = 2 * x[0] - x[padlen - i];
  for (let i = 0; i < n; i += 1) padded[padlen + i] = x[i];
  for (let i = 0; i < padlen; i += 1) padded[n + padlen + i] = 2 * x[n - 1] - x[n - 2 - i];

  const forward = new SosCascade(sos);
  forward.primeWithDC(padded[0]);
  const once = forward.process(padded);

  const reversed = Float64Array.from(once).reverse();
  const backward = new SosCascade(sos);
  backward.primeWithDC(reversed[0]);
  const twice = Float64Array.from(backward.process(reversed)).reverse();

  return twice.slice(padlen, padlen + n);
}

function bandFor(fs) {
  const sos = PPG_BANDPASS_SOS[fs];
  if (!sos) {
    // Silently designing a filter for an unexpected rate would move the pulse band
    // without telling anyone - the same failure D-14 refuses for the ECG path.
    throw new Error(
      `no PPG coefficients for ${fs} Hz; generated rates are ${SUPPORTED_RATES.join(', ')}. ` +
        'Regenerate with: python dsp/export_coefficients.py',
    );
  }
  return sos;
}

/** Isolate the cardiac component: 0.5-5 Hz covers 30-300 bpm and the first harmonic. */
export function pulsatile(x, fs) {
  return sosfiltfilt(bandFor(fs), x);
}

function mean(a) {
  let s = 0;
  for (let i = 0; i < a.length; i += 1) s += a[i];
  return a.length ? s / a.length : 0;
}

function std(a) {
  const m = mean(a);
  let s = 0;
  for (let i = 0; i < a.length; i += 1) s += (a[i] - m) ** 2;
  return a.length ? Math.sqrt(s / a.length) : 0;
}

function median(a) {
  if (!a.length) return NaN;
  const s = Float64Array.from(a).sort();
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function ptp(a, from = 0, to = a.length) {
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = from; i < to; i += 1) {
    if (a[i] < lo) lo = a[i];
    if (a[i] > hi) hi = a[i];
  }
  return hi - lo;
}

function correlation(a, b) {
  const ma = mean(a);
  const mb = mean(b);
  let num = 0;
  let da = 0;
  let db = 0;
  for (let i = 0; i < a.length; i += 1) {
    const x = a[i] - ma;
    const y = b[i] - mb;
    num += x * y;
    da += x * x;
    db += y * y;
  }
  return da > 0 && db > 0 ? num / Math.sqrt(da * db) : 0;
}

/**
 * Pulse period by autocorrelation, and how strongly the signal supports it.
 *
 * The sub-harmonic handling is the important part and is not an optimisation: a periodic
 * signal correlates with itself at every multiple of its period, and with beat-to-beat
 * variation the *longer* lag can win outright. On BIDMC recordings a plain argmax
 * reported exactly half the patient's pulse rate. Taking the shortest lag within
 * `subharmonicTolerance` of the best fixes it (D-36).
 */
export function periodicity(ac, fs) {
  const n = ac.length;
  if (n < 4) return { periodS: null, strength: 0 };
  const m = mean(ac);
  const x = new Float64Array(n);
  for (let i = 0; i < n; i += 1) x[i] = ac[i] - m;

  const lo = Math.floor((fs * 60) / P.maxPulseBpm);
  const hi = Math.min(Math.floor((fs * 60) / P.minPulseBpm), n - 1);
  if (hi <= lo) return { periodS: null, strength: 0 };

  let zero = 0;
  for (let i = 0; i < n; i += 1) zero += x[i] * x[i];
  if (zero <= 0) return { periodS: null, strength: 0 };

  const corr = new Float64Array(hi + 1);
  for (let lag = lo; lag <= hi; lag += 1) {
    let s = 0;
    for (let i = 0; i + lag < n; i += 1) s += x[i] * x[i + lag];
    corr[lag] = s / zero;
  }

  // Local maxima only: without this a shoulder of the zero-lag peak can win purely by
  // being close to it.
  const peaks = [];
  for (let lag = lo + 1; lag < hi; lag += 1) {
    if (corr[lag] > corr[lag - 1] && corr[lag] >= corr[lag + 1]) peaks.push(lag);
  }
  if (!peaks.length) {
    let best = lo;
    for (let lag = lo; lag <= hi; lag += 1) if (corr[lag] > corr[best]) best = lag;
    return { periodS: best / fs, strength: corr[best] };
  }

  let best = peaks[0];
  for (const lag of peaks) if (corr[lag] > corr[best]) best = lag;
  if (corr[best] <= 0) return { periodS: null, strength: 0 };

  const threshold = P.subharmonicTolerance * corr[best];
  let chosen = best;
  for (const lag of peaks) {
    if (corr[lag] >= threshold) {
      chosen = lag; // peaks are in ascending order, so the first match is the shortest
      break;
    }
  }
  return { periodS: chosen / fs, strength: corr[chosen] };
}

/**
 * Local maxima above `height`, thinned so none are closer than `minDistance`.
 *
 * Deliberately not a call into a peak-finding library: this exists twice, here and in
 * `dsp/ppg.py`, and the cross-language comparison only means something if both sides run
 * the same algorithm (D-38). Thinning is greedy by amplitude, tallest first.
 */
export function pickPeaks(x, minDistance, height) {
  if (x.length < 3) return [];
  const candidates = [];
  for (let i = 1; i < x.length - 1; i += 1) {
    if (x[i] > x[i - 1] && x[i] >= x[i + 1] && x[i] >= height) candidates.push(i);
  }
  candidates.sort((a, b) => x[b] - x[a] || a - b);

  const keep = [];
  for (const idx of candidates) {
    if (keep.every((k) => Math.abs(idx - k) >= minDistance)) keep.push(idx);
  }
  return keep.sort((a, b) => a - b);
}

/** Systolic peaks on the IR channel, which has the stronger pulsatile signal. */
export function findPulses(ir, fs) {
  const ac = pulsatile(ir, fs);
  for (let i = 0; i < ac.length; i += 1) ac[i] = -ac[i]; // transmitted light dips as volume rises
  const s = std(ac);
  if (s <= 0) return [];
  const { periodS } = periodicity(ac, fs);
  const minDistance = periodS ? Math.floor(0.6 * periodS * fs) : Math.floor((fs * 60) / P.maxPulseBpm);
  return pickPeaks(ac, Math.max(minDistance, 1), 0.4 * s);
}

/** Empirical ratio-of-ratios to saturation. The curve is an assumption, not a result (D-34). */
export function spo2FromRatio(r) {
  return P.calIntercept - P.calSlope * r;
}

/**
 * Estimate saturation, pulse rate and perfusion - or explain why it cannot.
 *
 * Returns `spo2Pct: null` whenever the signal does not support a number. Every refusal
 * carries a reason, because "no reading" and "no reading because the finger moved" are
 * different things to a clinician.
 */
export function estimateSpo2(red, ir, fs) {
  if (red.length !== ir.length) {
    throw new Error(`red and ir must be the same length, got ${red.length} and ${ir.length}`);
  }
  if (!(fs > 0)) throw new Error(`fs must be positive, got ${fs}`);

  const nothing = (reason, extra = {}) => ({
    spo2Pct: null,
    ratio: null,
    pulseBpm: null,
    perfusionPct: 0,
    nBeats: 0,
    quality: 'unusable',
    channelCorr: 0,
    periodicity: 0,
    reason,
    ...extra,
  });

  if (red.length < Math.floor(fs * 2)) return nothing('less than 2 s of signal');
  const dcIr = mean(ir);
  const dcRed = mean(red);
  if (!(dcIr > 0) || !(dcRed > 0)) {
    return nothing('no light reaching the detector - sensor off the finger?');
  }

  const acIr = pulsatile(ir, fs);
  const acRed = pulsatile(red, fs);
  const perfusionPct = (100 * ptp(acIr)) / dcIr;
  const channelCorr = std(acIr) > 0 && std(acRed) > 0 ? correlation(acRed, acIr) : 0;

  const peaks = findPulses(ir, fs);
  if (peaks.length < 3) {
    return nothing('fewer than 3 pulses found', { perfusionPct, nBeats: peaks.length, channelCorr });
  }

  const { periodS, strength } = periodicity(acIr, fs);
  const pulseBpm = periodS ? 60 / periodS : null;
  const base = { perfusionPct, nBeats: peaks.length, channelCorr, periodicity: strength };

  if (perfusionPct < P.minPerfusionPct) {
    return {
      ...nothing(`perfusion ${perfusionPct.toFixed(2)} % below the ${P.minPerfusionPct} % floor`),
      ...base,
    };
  }
  if (strength < P.minPeriodicity) {
    return {
      ...nothing(`periodicity ${strength.toFixed(2)} below ${P.minPeriodicity} - no pulse present`),
      ...base,
    };
  }
  if (channelCorr < P.minChannelCorr) {
    return {
      ...nothing(
        `red/IR pulsatile correlation ${channelCorr.toFixed(3)} below ${P.minChannelCorr} - artefact, not pulsation`,
      ),
      ...base,
    };
  }

  const ratios = [];
  for (let b = 0; b < peaks.length - 1; b += 1) {
    const lo = peaks[b];
    const hi = peaks[b + 1];
    if (hi - lo < 2) continue;
    const dI = mean(ir.slice(lo, hi));
    const dR = mean(red.slice(lo, hi));
    if (!(dI > 0) || !(dR > 0)) continue;
    const aI = ptp(acIr, lo, hi);
    const aR = ptp(acRed, lo, hi);
    if (!(aI > 0)) continue;
    ratios.push(aR / dR / (aI / dI));
  }
  if (!ratios.length) return { ...nothing('no usable beat windows'), ...base, pulseBpm };

  const ratio = median(ratios);
  if (ratio < P.minPlausibleR || ratio > P.maxPlausibleR) {
    return {
      ...nothing(`R = ${ratio.toFixed(2)} outside the calibrated range`),
      ...base,
      ratio,
      pulseBpm,
    };
  }

  // Beat-to-beat spread measures how much to trust the median. It cannot catch a
  // self-consistent artefact - that is what the channel correlation above is for (D-32).
  let spread = 0;
  if (ratios.length > 3) {
    const sorted = Float64Array.from(ratios).sort();
    const q = (p) => sorted[Math.min(sorted.length - 1, Math.floor(p * (sorted.length - 1) + 0.5))];
    spread = q(0.75) - q(0.25);
  }
  let quality = spread < 0.15 && channelCorr >= P.goodChannelCorr ? 'good' : 'poor';

  let spo2 = spo2FromRatio(ratio);
  if (spo2 > 100) {
    // Physically impossible; the curve is approximate near full saturation. Clamping is
    // right, but the reading stops being independent evidence.
    spo2 = 100;
    quality = 'poor';
  }

  return {
    spo2Pct: Math.round(spo2 * 10) / 10,
    ratio: Math.round(ratio * 1e4) / 1e4,
    pulseBpm,
    ...base,
    quality,
    reason: `${ratios.length} beat windows, IQR ${spread.toFixed(3)}, red/IR corr ${channelCorr.toFixed(3)}`,
  };
}
