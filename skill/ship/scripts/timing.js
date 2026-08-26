/**
 * timing.js — High-resolution timing helper for Node.js skill scripts.
 *
 * Provides a ``Timer`` class that records per-step and nested-step elapsed
 * wall-clock time using ``process.hrtime.bigint()``.
 *
 * Usage::
 *
 *   const { Timer } = require('./timing.js');
 *
 *   const root = new Timer('root');
 *   root.start();
 *   // ... do work ...
 *   const elapsed = root.stop();
 *   console.log(root.render());          // human-readable
 *   console.log(JSON.stringify(root.toDict()));  // JSON-serializable
 *
 * The timer tracks nested named steps with wall-clock elapsed time, supports
 * JSON serialization, and renders both human-readable (table/Markdown) and
 * JSON formats. Percentages of individual steps sum to ~100% of total time.
 * Sub-second precision (3 decimal places, milliseconds) is used for human
 * output.
 *
 * Overhead is negligible — only ``process.hrtime.bigint()`` calls and
 * list/dict operations inside context wrappers.
 *
 * @module timing
 */



/**
 * Convert a BigInt nanosecond value to seconds (float).
 * @param {bigint} ns - Nanosecond value.
 * @returns {number} Seconds.
 */
function nanosToSeconds(ns) {
  return Number(ns) / 1_000_000_000;
}

/**
 * Convert seconds (float) to nanoseconds (BigInt).
 * @param {number} s - Seconds.
 * @returns {bigint} Nanosecond value.
 */
function secondsToNanos(s) {
  return BigInt(Math.round(s * 1_000_000_000));
}

/**
 * Timer — records per-step and nested-step elapsed time.
 *
 * @param {string} name - The step label.
 * @param {Timer|null} [parent] - Optional parent Timer for nesting.
 */
class Timer {
  constructor(name, parent = null) {
    this.name = name;
    this.parent = parent;
    this.startNs = null;
    this.elapsedNs = 0n;
    this.nestedSteps = [];
  }

  /** Start timing. */
  start() {
    this.startNs = process.hrtime.bigint();
    if (this.parent !== null) {
      this.parent.nestedSteps.push(this);
    }
    return this;
  }

  /** Stop timing and return elapsed seconds. */
  stop() {
    this.elapsedNs = process.hrtime.bigint() - this.startNs;
    return nanosToSeconds(this.elapsedNs);
  }

  /** Total elapsed time including nested children (seconds). */
  get totalSeconds() {
    if (this.nestedSteps.length > 0) {
      return this.nestedSteps.reduce((sum, s) => sum + s.totalSeconds, 0);
    }
    return nanosToSeconds(this.elapsedNs);
  }

  /** Percentage of total time (relative to root). */
  get percentage() {
    const root = this._findRoot();
    if (!root) return 0;
    const total = root.totalSeconds;
    if (total === 0) return 0;
    return (this.totalSeconds / total) * 100;
  }

  /** Walk up to find the root timer. */
  _findRoot() {
    let current = this;
    while (current.parent !== null) {
      current = current.parent;
    }
    return current;
  }

  /** Render a human-readable table report. */
  render() {
    const lines = [];
    lines.push('Timing Report');
    lines.push('='.repeat(70));
    lines.push(
      `${'Step'.padEnd(30)} ${'Elapsed'.padStart(10)} ${'%'.padStart(6)} ${'Total'.padStart(10)}`
    );
    lines.push('-'.repeat(70));

    const root = this._findRoot();
    const rootTotal = root ? root.totalSeconds : this.totalSeconds;

    if (this.nestedSteps.length > 0) {
      // Root has children — show children under root name
      lines.push(this.name);
      this._renderTree(lines, 0, rootTotal);
    } else {
      // Root has no children — show this timer's own row
      lines.push(
        `${this.name.padEnd(30)} ${this.totalSeconds.toFixed(3).padStart(10)} ` +
        `${this.percentage.toFixed(1).padStart(5)}% ${this.totalSeconds.toFixed(3).padStart(10)}`
      );
    }

    lines.push('-'.repeat(70));
    lines.push(
      `${'Total'.padEnd(30)} ${rootTotal.toFixed(3).padStart(10)} ${'100.0'.padStart(6)} ${rootTotal.toFixed(3).padStart(10)}`
    );
    lines.push('='.repeat(70));
    return lines.join('\n');
  }

  /** Recursively render nested steps. */
  _renderTree(lines, indent, rootTotal) {
    const prefix = '  '.repeat(indent);
    for (const step of this.nestedSteps) {
      const pct = rootTotal > 0 ? (step.totalSeconds / rootTotal) * 100 : 0;
      lines.push(
        `${prefix}${step.name.padEnd(28)} ${step.totalSeconds.toFixed(3).padStart(10)} ` +
        `${pct.toFixed(1).padStart(5)}% ${step.totalSeconds.toFixed(3).padStart(10)}`
      );
      if (step.nestedSteps.length > 0) {
        step._renderTree(lines, indent + 1, rootTotal);
      }
    }
  }

  /** Return a JSON-serializable dict representation. */
  toDict() {
    return {
      name: this.name,
      elapsed: Math.round(nanosToSeconds(this.elapsedNs) * 1000) / 1000,
      total_time: Math.round(this.totalSeconds * 1000) / 1000,
      percentage: Math.round(this.percentage * 10) / 10,
      nested_steps: this.nestedSteps.map((s) => s.toDict()),
    };
  }

  /** Return a JSON string. */
  toJson() {
    return JSON.stringify(this.toDict(), null, 2);
  }
}

export { Timer, nanosToSeconds, secondsToNanos };
