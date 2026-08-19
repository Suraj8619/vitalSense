/**
 * Causal, streaming Pan-Tompkins QRS detection.
 *
 * This is the real-time counterpart of `dsp/pan_tompkins.py`. The offline version
 * may look at the whole recording and filter with zero phase distortion; this one
 * sees each sample once, in order, and can never look forward. Its accuracy is
 * therefore expected to be slightly below the offline version's, and
 * `tools/validateStreaming.js` measures that gap against the same MIT-BIH records
 * rather than assuming it.
 *
 * Three consequences of being causal, all deliberate:
 *
 * 1. **Filtering is one-directional**, so every stage has group delay. The trailing
 *    150 ms integration window delays the detection bump by about half its width;
 *    R-peak snapping compensates by searching backwards from the bump.
 * 2. **A peak is only knowable one sample late** - a local maximum is confirmed by
 *    the sample after it. The detector therefore reports beats with a one-sample
 *    lag, which is 2.8 ms at 360 Hz.
 * 3. **Thresholds must be learned online.** For the first two seconds the detector
 *    only observes, seeding its signal and noise estimates before it will report a
 *    beat. Beats in that window are missed by design - stated here so it is a known
 *    limitation rather than a mystery in the results.
 */

import { MovingAverage, RingBuffer, SosCascade } from './biquad.js';

/** Trailing window for the energy-concentration measure, matching dsp/quality.py. */
const QUALITY_WINDOW_S = 4;
/**
 * Concentration below which a reported rate stops meeting SR-01.
 *
 * Not a percentile of any recording: derived from the requirement. Sweeping noise over
 * synthetic ECG with a known rate, heart-rate error stays at 0.00 bpm down to 15.9 and
 * jumps past 24 bpm at 13.9 (D-35). Generated into coefficients.js would be better still;
 * for now it is defined once here and mirrored by the Python constant, which the
 * cross-language check in dsp/quality.py pins.
 */
export const UNUSABLE_CONCENTRATION = 15.0;

/**
 * The detector's own signal-to-noise ratio (spki/npki) below which a low energy
 * concentration means "noise", not "fast rhythm".
 *
 * The concentration statistic is rate-confounded: above ~125 bpm the QRS bumps crowd
 * together, the median of the integrated signal rises, and a perfectly clean
 * tachycardia scores below the concentration gate - which would silence the monitor for
 * exactly the rhythm it exists to alarm on (the D-35 trap, reappearing along the rate
 * axis; found when the TESTING.md tachycardia walkthrough never fired its alarm).
 *
 * The fix is a second, independent condition: withhold only when the concentration is
 * low AND the detector's running signal-to-noise estimate is low too. Measured on the
 * shipped detector: clean 130 bpm scores SNR ~32 and clean 150 bpm ~13, while every
 * noise case that produced a wrong rate scored <= 5.7. The floor of 8 sits in that gap.
 * A clean rhythm at ~180 bpm still scores ~5.7 and remains withheld - a real,
 * documented limitation (D-51) rather than a silent one.
 */
export const UNUSABLE_SNR = 8.0;
import { DETECTION_SOS, DISPLAY_SOS, NOTCH, QRS_PARAMS, SAMPLE_RATE_HZ } from './coefficients.js';

/** Seconds of signal history retained for R-peak snapping and slope comparison. */
const HISTORY_S = 4;

export class QrsStreamDetector {
  /**
   * @param {object} [options]
   * @param {number} [options.fs] sampling rate; must match the coefficient design
   * @param {boolean} [options.searchBack] re-scan after a suspiciously long gap
   */
  constructor({ fs = SAMPLE_RATE_HZ, searchBack = true } = {}) {
    if (!Number.isFinite(fs) || fs <= 0) {
      throw new RangeError(`fs must be a positive number, got ${fs}`);
    }
    if (fs !== SAMPLE_RATE_HZ) {
      // The coefficients were designed for one specific rate. Silently accepting a
      // different one would apply the wrong filters to real patient data.
      throw new RangeError(
        `fs ${fs} Hz does not match the ${SAMPLE_RATE_HZ} Hz coefficient design - ` +
          'regenerate coefficients with dsp/export_coefficients.py',
      );
    }
    this.fs = fs;
    this.searchBack = searchBack;

    const ms = (v) => Math.max(1, Math.round((v * fs) / 1000));
    this.refractory = ms(QRS_PARAMS.refractoryMs);
    this.twaveGap = ms(QRS_PARAMS.twaveMs);
    this.fiducialHalf = ms(QRS_PARAMS.fiducialMs);
    this.integrationWidth = ms(QRS_PARAMS.integrationMs);
    this.learningSamples = Math.round(QRS_PARAMS.learningS * fs);
    // A trailing moving average delays its output by roughly half its width; the
    // detection bump therefore sits this far after the QRS that produced it.
    this.integrationDelay = Math.floor(this.integrationWidth / 2);

    // Notch is expressed as a single section: [b0,b1,b2, 1,a1,a2].
    this.notch = new SosCascade([[...NOTCH.b, ...NOTCH.a]]);
    this.display = new SosCascade(DISPLAY_SOS);
    this.detection = new SosCascade(DETECTION_SOS);
    this.integrator = new MovingAverage(this.integrationWidth);

    const history = Math.round(HISTORY_S * fs);
    this.cleanedHistory = new RingBuffer(history);
    this.derivHistory = new RingBuffer(history);

    this.deriv = [0, 0, 0, 0, 0]; // five-point derivative delay line
    this.n = 0; // absolute sample counter
    this.learnStartN = 0; // sample index the current learning window began at
    this.prevInt = [0, 0]; // last two integrated values, for local-max detection

    /**
     * A trailing window of integrated values, for the signal-quality gate.
     *
     * The integrator is already computed here, and its distribution is what
     * `dsp/quality.py` measures: a real ECG puts its energy into brief bursts, so the
     * 99th percentile stands far above the median. Noise spreads energy evenly and the
     * ratio collapses. Sampling it from the detector rather than recomputing the
     * Pan-Tompkins stages elsewhere keeps one implementation of the pipeline (D-35).
     */
    this.qualityWindow = new Float64Array(Math.round(QUALITY_WINDOW_S * this.fs));
    this.qualityFill = 0;
    this.qualityAt = 0;

    // Adaptive threshold state.
    this.spki = 0; // running estimate of QRS peak level
    this.npki = 0; // running estimate of noise peak level
    this.learning = true;
    this.learnMax = 0;
    this.learnSum = 0;

    this.lastQrs = -Infinity; // absolute index of the last accepted beat
    this.rrOk = []; // recent RR intervals inside the accepted band
    this.recentCandidates = []; // for search-back: [{index, value}]

    // Running signal-amplitude estimate, for the flat-line gate.
    this.ampMean = 0;
    this.ampVar = 0;
    this.ampCount = 0;

    this.stats = { beats: 0, searchBack: 0, twaveRejected: 0, samples: 0 };
  }

  /**
   * Restart acquisition, discarding all filter and threshold state.
   *
   * Called when the electrodes are reconnected. The step from a railed lead-off
   * level back to a real signal drives a large transient through the IIR filters,
   * and the adaptive thresholds are still tuned to the flat-line period - together
   * they produce a burst of detections that look like a very fast heart rate.
   * Clearing the state means the detector relearns from the new signal instead of
   * reporting artefacts of the reconnection.
   */
  restart() {
    this.notch.reset();
    this.display.reset();
    this.detection.reset();
    this.integrator.reset();
    this.qualityFill = 0;
    this.qualityAt = 0;
    this.deriv = [0, 0, 0, 0, 0];
    this.prevInt = [0, 0];
    this.learning = true;
    this.learnMax = 0;
    this.learnSum = 0;
    this.learnStartN = this.n;
    this.spki = 0;
    this.npki = 0;
    this.lastQrs = -Infinity;
    this.rrOk = [];
    this.recentCandidates = [];
    this.ampMean = 0;
    this.ampVar = 0;
    this.ampCount = 0;
  }

  get thresholdI1() {
    return this.npki + 0.25 * (this.spki - this.npki);
  }

  /** Running standard deviation of the cleaned signal (Welford's algorithm). */
  get signalStd() {
    return this.ampCount > 1 ? Math.sqrt(this.ampVar / (this.ampCount - 1)) : 0;
  }

  /**
   * True when the input is too small to be a real ECG - detached electrode, railed
   * amplifier, or asystole. Mirrors the gate in the offline detector: an adaptive
   * threshold rescales to whatever it is given, so without this it locks onto
   * numerical noise and reports a confident heart rate (requirement SR-07).
   */
  /**
   * QRS energy concentration over the trailing window: p99 / median.
   *
   * Returns null until the window has filled - a partial window would answer a question
   * about a period that has not happened yet, which is the shape of the very first
   * asystole bug (BUILD_LOG 005).
   *
   * Deliberately measured *after* the 150 ms integration, where QRS width has been
   * normalised away. Kurtosis and the 5-15 Hz power share both fall for a wide
   * ventricular beat, so a gate built on them silences the monitor exactly when a
   * patient starts throwing ventricular beats (D-35).
   */
  /** The detector's running signal-to-noise estimate. Infinity until noise is seen. */
  get snr() {
    if (!(this.npki > 0)) return Infinity;
    return this.spki / this.npki;
  }

  get energyConcentration() {
    if (this.qualityFill < this.qualityWindow.length) return null;
    const sorted = Float64Array.from(this.qualityWindow).sort();
    const median = sorted[Math.floor(sorted.length / 2)];
    if (!(median > 0)) return null;
    const p99 = sorted[Math.min(sorted.length - 1, Math.floor(0.99 * sorted.length))];
    return p99 / median;
  }

  get flatline() {
    return this.ampCount > this.fs && this.signalStd < QRS_PARAMS.minSignalStd;
  }

  /**
   * Feed a block of raw samples.
   *
   * @param {ArrayLike<number>} samples raw ECG, mV, oldest first
   * @returns {{cleaned: Float64Array, beats: number[], beatOffsets: number[]}}
   *   `cleaned` is the display-path waveform for this block; `beats` are absolute
   *   sample indices of R peaks confirmed during this block; `beatOffsets` are those
   *   same beats as offsets into `cleaned`, or negative if the peak sits in an
   *   earlier block (which snapping backwards can produce).
   */
  push(samples) {
    const blockStart = this.n;
    const cleaned = new Float64Array(samples.length);
    const beats = [];

    for (let i = 0; i < samples.length; i += 1) {
      const raw = samples[i];
      if (!Number.isFinite(raw)) {
        // A NaN would poison every IIR state it touches and never wash out. Hold
        // the previous value instead, and let the frame validator report the fault.
        cleaned[i] = this.cleanedHistory.count > 0 ? this.cleanedHistory.at(this.n - 1) : 0;
        this.#advance(cleaned[i], 0);
        continue;
      }

      const notched = this.notch.processSample(raw);
      const display = this.display.processSample(notched);
      cleaned[i] = display;

      const banded = this.detection.processSample(notched);

      // Five-point derivative: (2x[n] + x[n-1] - x[n-3] - 2x[n-4]) * fs/8, applied
      // causally, so it reports the slope centred two samples back.
      this.deriv = [banded, this.deriv[0], this.deriv[1], this.deriv[2], this.deriv[3]];
      const d =
        ((2 * this.deriv[0] + this.deriv[1] - this.deriv[3] - 2 * this.deriv[4]) * this.fs) / 8;

      const integrated = this.integrator.process(d * d);
      this.qualityWindow[this.qualityAt] = integrated;
      this.qualityAt = (this.qualityAt + 1) % this.qualityWindow.length;
      if (this.qualityFill < this.qualityWindow.length) this.qualityFill += 1;
      const beat = this.#decide(integrated);
      if (beat !== null) beats.push(beat);

      this.#advance(display, d);
    }

    this.stats.samples += samples.length;
    return {
      cleaned,
      beats,
      beatOffsets: beats.map((b) => b - blockStart),
    };
  }

  /** Push one sample's outputs into history and advance the sample counter. */
  #advance(cleanedValue, derivValue) {
    this.cleanedHistory.push(cleanedValue);
    this.derivHistory.push(derivValue);

    // Welford update on the cleaned signal, for the flat-line gate.
    this.ampCount += 1;
    const delta = cleanedValue - this.ampMean;
    this.ampMean += delta / this.ampCount;
    this.ampVar += delta * (cleanedValue - this.ampMean);

    this.n += 1;
  }

  /**
   * Run the adaptive decision stage for one integrated sample.
   * @returns {number|null} absolute index of a confirmed R peak, or null
   */
  #decide(integrated) {
    const [twoAgo, oneAgo] = this.prevInt;
    this.prevInt = [oneAgo, integrated];

    // --- learning window: observe only, then seed the estimates ---
    if (this.learning) {
      this.learnMax = Math.max(this.learnMax, integrated);
      this.learnSum += integrated;
      const learned = this.n - this.learnStartN;
      if (learned >= this.learningSamples) {
        this.spki = this.learnMax * 0.25;
        this.npki = (this.learnSum / Math.max(1, learned)) * 0.5;
        this.learning = false;
      }
      return null;
    }

    if (this.flatline) return null;

    // A local maximum is only confirmed by the following sample, so the candidate
    // sits one sample back from the one just pushed.
    const isLocalMax = oneAgo > twoAgo && oneAgo >= integrated;
    if (!isLocalMax) return null;

    const candidateIdx = this.n - 1;
    const value = oneAgo;

    this.recentCandidates.push({ index: candidateIdx, value });
    // Retain only what search-back could still reach.
    const keepFrom = candidateIdx - Math.round(4 * this.fs);
    while (this.recentCandidates.length && this.recentCandidates[0].index < keepFrom) {
      this.recentCandidates.shift();
    }

    if (candidateIdx - this.lastQrs < this.refractory) return null;

    let confirmed = null;

    // --- search-back: a gap beyond 166% of the recent average RR suggests a missed
    // beat, so re-examine that gap at half the threshold ---
    if (this.searchBack && this.rrOk.length > 0 && Number.isFinite(this.lastQrs)) {
      const meanRr = this.rrOk.reduce((a, b) => a + b, 0) / this.rrOk.length;
      if (candidateIdx - this.lastQrs > 1.66 * meanRr) {
        const lowBound = this.lastQrs + this.refractory;
        const half = 0.5 * this.thresholdI1;
        let best = null;
        for (const c of this.recentCandidates) {
          if (c.index > lowBound && c.index < candidateIdx && c.value > half) {
            if (!best || c.value > best.value) best = c;
          }
        }
        if (best) {
          this.#accept(best.index, best.value, true);
          this.stats.searchBack += 1;
          confirmed = this.#snapToRPeak(best.index);
        }
      }
    }

    if (value > this.thresholdI1) {
      // --- T-wave discrimination: a beat arriving too soon is a T wave unless it
      // rises as steeply as the QRS before it ---
      if (Number.isFinite(this.lastQrs) && candidateIdx - this.lastQrs < this.twaveGap) {
        if (this.#slopeAt(candidateIdx) < QRS_PARAMS.twaveSlopeFrac * this.#slopeAt(this.lastQrs)) {
          this.npki = 0.125 * value + 0.875 * this.npki;
          this.stats.twaveRejected += 1;
          return confirmed;
        }
      }
      this.#accept(candidateIdx, value, false);
      confirmed = this.#snapToRPeak(candidateIdx);
    } else {
      this.npki = 0.125 * value + 0.875 * this.npki;
    }

    return confirmed;
  }

  #accept(index, value, viaSearchBack) {
    // Search-back detections are weaker evidence, so they move the signal estimate
    // less - the weighting from the original paper.
    this.spki = viaSearchBack ? 0.25 * value + 0.75 * this.spki : 0.125 * value + 0.875 * this.spki;

    if (Number.isFinite(this.lastQrs)) {
      const rr = index - this.lastQrs;
      if (this.rrOk.length === 0) {
        this.rrOk.push(rr);
      } else {
        const mean = this.rrOk.reduce((a, b) => a + b, 0) / this.rrOk.length;
        if (rr >= 0.92 * mean && rr <= 1.16 * mean) {
          this.rrOk.push(rr);
          if (this.rrOk.length > 8) this.rrOk.shift();
        }
      }
    }
    this.lastQrs = index;
    this.stats.beats += 1;
  }

  /** Peak absolute slope near an index - steep for a QRS, shallow for a T wave. */
  #slopeAt(index) {
    const half = Math.max(1, Math.round(0.035 * this.fs));
    const lo = Math.max(this.derivHistory.oldestIndex, index - half);
    const hi = Math.min(this.derivHistory.count, index + half + 1);
    let max = 0;
    for (let i = lo; i < hi; i += 1) max = Math.max(max, Math.abs(this.derivHistory.at(i)));
    return max;
  }

  /**
   * Snap a detection onto the true R peak in the cleaned waveform.
   *
   * The integration bump lags the QRS by about half the integration window, so the
   * search is centred that far back. The largest *absolute* deflection is taken, so
   * leads where the R wave points downwards need no special case.
   */
  #snapToRPeak(detectionIdx) {
    const centre = detectionIdx - this.integrationDelay;
    const lo = Math.max(this.cleanedHistory.oldestIndex, centre - this.fiducialHalf);
    const hi = Math.min(this.cleanedHistory.count, centre + this.fiducialHalf + 1);
    if (lo >= hi) return centre;

    let bestIdx = lo;
    let bestVal = -Infinity;
    for (let i = lo; i < hi; i += 1) {
      const v = Math.abs(this.cleanedHistory.at(i));
      if (v > bestVal) {
        bestVal = v;
        bestIdx = i;
      }
    }
    return bestIdx;
  }
}
