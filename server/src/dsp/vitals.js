/**
 * Heart rate and rhythm classification from detected beats.
 *
 * A direct port of `dsp/vitals.py`, kept deliberately small so the two stay
 * comparable by reading them side by side. The thresholds live here as the single
 * definition used by both the alarm engine and the dashboard (requirement SR-08).
 */

/** Beats closer than this are detector artefacts, not physiology (240 bpm). */
export const MIN_PLAUSIBLE_RR_S = 0.25;
/** Intervals longer than this are a gap in detection, not a heart rate (20 bpm). */
export const MAX_PLAUSIBLE_RR_S = 3.0;

/** Alarm thresholds, in bpm. Defined once; imported by the alarm engine. */
export const DEFAULT_LIMITS = Object.freeze({ brady: 60, tachy: 100 });

/** Below this fraction of usable RR intervals, the rate is not trustworthy. */
const MIN_USABLE_FRACTION = 0.5;

/**
 * Classify a heart rate against the alarm thresholds.
 * @param {number|null} bpm
 * @param {{brady: number, tachy: number}} [limits]
 * @returns {'bradycardia'|'normal'|'tachycardia'|'insufficient data'}
 */
export function classifyRate(bpm, limits = DEFAULT_LIMITS) {
  if (bpm === null || !Number.isFinite(bpm) || bpm <= 0) return 'insufficient data';
  if (bpm < limits.brady) return 'bradycardia';
  if (bpm > limits.tachy) return 'tachycardia';
  return 'normal';
}

/**
 * RR intervals in seconds from absolute R-peak sample indices.
 * @param {number[]} rPeaks
 * @param {number} fs
 * @returns {number[]}
 */
export function rrIntervals(rPeaks, fs) {
  if (!Number.isFinite(fs) || fs <= 0) throw new RangeError(`fs must be positive, got ${fs}`);
  const out = [];
  for (let i = 1; i < rPeaks.length; i += 1) out.push((rPeaks[i] - rPeaks[i - 1]) / fs);
  return out;
}

/** Median of an array. Does not mutate the input. */
function median(values) {
  if (values.length === 0) return NaN;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/**
 * Heart rate and variability over a set of beats.
 *
 * The reported rate is the **median** of the instantaneous rates rather than
 * `60 / mean(RR)`: one missed beat doubles an interval and one spurious beat halves
 * it, and a mean carries that error straight into a number that can trip an alarm.
 *
 * @param {number[]} rPeaks absolute sample indices
 * @param {number} fs
 * @param {{brady: number, tachy: number}} [limits]
 */
export function heartRate(rPeaks, fs, limits = DEFAULT_LIMITS) {
  const all = rrIntervals(rPeaks, fs);
  const usable = all.filter((rr) => rr >= MIN_PLAUSIBLE_RR_S && rr <= MAX_PLAUSIBLE_RR_S);
  const rejected = all.length - usable.length;

  if (usable.length === 0) {
    return {
      bpm: null,
      rrS: [],
      nBeats: rPeaks.length,
      nRejected: rejected,
      sdnnMs: 0,
      rmssdMs: 0,
      classification: 'insufficient data',
      trustworthy: false,
    };
  }

  const instantaneous = usable.map((rr) => 60 / rr);
  const bpm = median(instantaneous);

  const mean = usable.reduce((a, b) => a + b, 0) / usable.length;
  const sdnn =
    usable.length >= 2
      ? Math.sqrt(usable.reduce((a, rr) => a + (rr - mean) ** 2, 0) / (usable.length - 1)) * 1000
      : 0;

  let rmssd = 0;
  if (usable.length >= 2) {
    let acc = 0;
    for (let i = 1; i < usable.length; i += 1) acc += (usable[i] - usable[i - 1]) ** 2;
    rmssd = Math.sqrt(acc / (usable.length - 1)) * 1000;
  }

  // If most intervals had to be discarded, the surviving few are not a basis for a
  // clinical number - say so rather than publishing a confident value (SR-07).
  const trustworthy = all.length === 0 ? false : usable.length / all.length >= MIN_USABLE_FRACTION;

  return {
    bpm,
    rrS: usable,
    nBeats: rPeaks.length,
    nRejected: rejected,
    sdnnMs: sdnn,
    rmssdMs: rmssd,
    classification: trustworthy ? classifyRate(bpm, limits) : 'insufficient data',
    trustworthy,
  };
}

/**
 * Rolling heart rate over the beats seen in a trailing time window.
 *
 * Keeps only the beats inside the window, so memory is bounded no matter how long
 * the monitor runs.
 */
export class RollingHeartRate {
  /**
   * @param {number} fs
   * @param {number} [windowS] trailing window length in seconds
   * @param {{brady: number, tachy: number}} [limits]
   */
  constructor(fs, windowS = 10, limits = DEFAULT_LIMITS) {
    if (!Number.isFinite(fs) || fs <= 0) throw new RangeError(`fs must be positive, got ${fs}`);
    if (!Number.isFinite(windowS) || windowS <= 0) {
      throw new RangeError(`windowS must be positive, got ${windowS}`);
    }
    this.fs = fs;
    this.windowS = windowS;
    this.limits = limits;
    this.beats = [];
    /** Most recent beat ever seen, retained after it leaves the window. */
    this.lastBeatSample = null;
    /**
     * Sample index from which "time without a beat" is measured when no beat has been
     * seen since. Moves forward when acquisition restarts, so the silence during an
     * interruption is not charged to the patient afterwards.
     */
    this.silenceFromSample = 0;
  }

  /** Add detected beats (absolute sample indices) and drop ones outside the window. */
  add(beats, currentSample) {
    for (const b of beats) {
      this.beats.push(b);
      if (this.lastBeatSample === null || b > this.lastBeatSample) this.lastBeatSample = b;
    }
    const cutoff = currentSample - this.windowS * this.fs;
    while (this.beats.length && this.beats[0] < cutoff) this.beats.shift();
  }

  /** Current heart rate over the window. */
  current() {
    return heartRate(this.beats, this.fs, this.limits);
  }

  /**
   * Restart the silence clock, and forget beats from before an interruption.
   *
   * Called when acquisition restarts - electrodes reattached, filters and thresholds
   * discarded (D-22). Without this, the gap during the interruption is still counted
   * afterwards: after ten seconds of leads-off plus three seconds of settling, the
   * "time since last beat" is already thirteen seconds, so asystole fires the instant
   * signal quality returns, before the patient has had any opportunity to produce a
   * beat the system was willing to look at.
   *
   * That is the same mistake as the very first asystole bug - alarming on a fact the
   * system cannot yet know - arriving from the other direction: not from never having
   * measured, but from having deliberately stopped measuring (D-47).
   */
  restart(currentSample) {
    this.beats = [];
    this.lastBeatSample = null;
    this.silenceFromSample = currentSample;
  }

  /**
   * Samples since the most recent beat - the input to asystole detection.
   *
   * Before any beat has ever been seen this returns the samples elapsed since the
   * session began (or since the last restart), **not** Infinity. Returning Infinity
   * made a freshly connected monitor declare asystole in its first frame, before it had
   * acquired enough signal to detect anything: an alarm asserting a fact the system
   * could not yet know.
   */
  samplesSinceLastBeat(currentSample) {
    if (this.lastBeatSample === null) return Math.max(0, currentSample - this.silenceFromSample);
    return currentSample - this.lastBeatSample;
  }
}
