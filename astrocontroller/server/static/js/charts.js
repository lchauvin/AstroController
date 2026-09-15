/* Chart helpers around the vendored uPlot build.
 *
 * Three rules the panels get for free by going through here:
 *
 *  - every chart carries a crosshair and a tooltip, because a chart you cannot
 *    interrogate is a picture;
 *  - series colours come from the categorical palette in CSS custom
 *    properties, in fixed slot order, never cycled;
 *  - identity is never colour alone -- each chart renders a legend of its own,
 *    above the plot, with the live value beside each name.
 */
"use strict";

import { cssVar, esc } from "./core.js";

/** Categorical slots, in fixed order. A fourth series is a design decision. */
export const SERIES_COLOURS = ["--series-1", "--series-2", "--series-3"];

const charts = new Set();

export class LineChart {
  /**
   * @param {HTMLElement} host   container; sized by CSS, not by the caller
   * @param {object} spec
   *   series:   [{label, colour, fill, width, dash, axis}]
   *   legendEl: element to render the legend + live values into
   *   xLabel:   formatter for the crosshair heading
   *   yLabel:   formatter for each value
   *   yRange:   optional [min, max]
   */
  constructor(host, spec) {
    this.host = host;
    this.spec = spec;
    this.plot = null;
    this.data = null;
    this.tip = null;
    host.style.position = "relative";
    charts.add(this);

    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);
  }

  get width() {
    return Math.max(120, this.host.clientWidth || 600);
  }

  get height() {
    return Math.max(90, this.host.clientHeight || 180);
  }

  /** Rebuild from scratch -- used on the first draw and after a theme change. */
  build() {
    if (typeof uPlot === "undefined") {
      this.host.innerHTML = `<div class="chart-empty">Charts unavailable (uPlot did not load).</div>`;
      return false;
    }
    this.host.innerHTML = "";

    const axis = {
      stroke: cssVar("--muted"),
      grid: { stroke: cssVar("--grid"), width: 1 },
      ticks: { stroke: cssVar("--grid"), width: 1 },
      font: `11px ${cssVar("--sans") || "system-ui"}`,
    };

    const series = [{ label: "x" }].concat(
      this.spec.series.map((s, index) => {
        const colour = cssVar(s.colour || SERIES_COLOURS[index % SERIES_COLOURS.length]);
        return {
          label: s.label,
          stroke: colour,
          // A bar series is drawn by the custom `draw` hook (signed, from a
          // zero baseline) rather than as a line, so its uPlot series is an
          // invisible width-0 carrier that still feeds the scale + tooltip.
          // `hidden` series are pure data carriers (e.g. bar direction signs)
          // and are drawn neither as a line nor in the legend.
          width: s.isBar || s.hidden ? 0 : s.width ?? 2,
          dash: s.dash,
          // A series on the right-hand axis gets its own scale; left is the
          // default "y" scale. Omit the key entirely when absent -- passing
          // `scale: undefined` would clobber uPlot's default and mis-map the
          // series onto the wrong (or no) scale.
          ...(s.scale ? { scale: s.scale } : {}),
          fill: s.fill ? colourWithAlpha(colour, s.fill) : undefined,
          points: { show: s.points === true, size: 5, stroke: colour, fill: colour },
        };
      })
    );

    const axes = [
      // `space` is a minimum gap between ticks in px. Without it a narrow
      // card asks for a tick every 20px and the labels overlap into mush.
      { ...axis, values: this.spec.xTicks, space: 74 },
      { ...axis, size: 46, values: this.spec.yTicks, space: 34 },
    ];
    if (this.spec.y2Ticks) {
      axes.push({
        ...axis,
        side: 1,
        scale: "y2",
        size: 40,
        values: this.spec.y2Ticks,
        space: 34,
        grid: { show: false }, // one grid is enough; the second clutters
      });
    }

    this.plot = new uPlot(
      {
        width: this.width,
        height: this.height,
        legend: { show: false },
        cursor: {
          y: false,
          points: { size: 7, width: 2 },
          drag: { x: false, y: false },
          // Two charts sharing a `syncKey` move their crosshairs together --
          // used to line up the error and correction traces, which are drawn
          // as separate charts but describe the same guide steps.
          ...(this.spec.syncKey ? { sync: { key: this.spec.syncKey } } : {}),
        },
        scales: {
          x: { time: false },
          y: this.spec.yRange ? { range: this.spec.yRange } : {},
          ...(this.spec.y2Ticks || this.spec.y2Range
            ? { y2: this.spec.y2Range ? { range: this.spec.y2Range } : {} }
            : {}),
        },
        axes,
        series,
        hooks: {
          setCursor: [(plot) => this.onCursor(plot)],
          // Bars (signed correction pulses) paint on top of the line series
          // after each frame. uPlot's own bars are bottom-anchored, wrong for
          // signed corrections, so `drawBars` does the work itself.
          draw: this.spec.series.some((s) => s.isBar)
            ? [(plot) => drawBars(plot, this)]
            : [],
        },
      },
      this.data || this.spec.series.map(() => []).concat([[]]),
      this.host
    );

    this.tip = document.createElement("div");
    this.tip.className = "chart-tip";
    this.tip.hidden = true;
    this.host.appendChild(this.tip);
    return true;
  }

  update(data) {
    this.data = data;
    if (!this.plot && !this.build()) return;
    this.plot.setData(data);
    this.renderLegend(data[0] ? data[0].length - 1 : -1);
  }

  resize() {
    if (this.plot) this.plot.setSize({ width: this.width, height: this.height });
  }

  /** Colours live in CSS variables, so a theme switch needs a fresh plot. */
  retheme() {
    if (!this.plot) return;
    this.plot.destroy();
    this.plot = null;
    if (this.data) this.update(this.data);
  }

  onCursor(plot) {
    const index = plot.cursor.idx;
    if (index === null || index === undefined || !this.data) {
      this.tip.hidden = true;
      this.renderLegend(this.data && this.data[0] ? this.data[0].length - 1 : -1);
      return;
    }
    const heading = this.spec.xLabel ? this.spec.xLabel(this.data[0][index], index) : "";
    const rows = this.spec.series
      .map((s, i) => {
        if (s.hidden) return "";
        const value = this.data[i + 1]?.[index];
        if (value === null || value === undefined) return "";
        const colour = cssVar(s.colour || SERIES_COLOURS[i % SERIES_COLOURS.length]);
        return `<div class="tip-row" style="color:${colour}"><i></i>
          <span style="color:var(--text-2)">${esc(s.label)}</span>
          <b>${esc(this.spec.yLabel ? this.spec.yLabel(value, i) : value)}</b></div>`;
      })
      .join("");
    this.tip.innerHTML = `<div class="tip-head">${esc(heading)}</div>${rows}`;
    this.tip.hidden = false;
    this.tip.style.left = `${plot.cursor.left}px`;
    this.tip.style.top = `${plot.cursor.top}px`;
    this.renderLegend(index);
  }

  /** The legend doubles as the readout: series name, swatch, value at cursor. */
  renderLegend(index) {
    const host = this.spec.legendEl;
    if (!host) return;
    host.innerHTML = this.spec.series
      .map((s, i) => {
        if (s.hidden) return "";
        const colour = cssVar(s.colour || SERIES_COLOURS[i % SERIES_COLOURS.length]);
        const value = index >= 0 ? this.data?.[i + 1]?.[index] : undefined;
        const shown =
          value === null || value === undefined
            ? ""
            : `<b>${esc(this.spec.yLabel ? this.spec.yLabel(value, i) : value)}</b>`;
        // A bar series gets a short bar swatch rather than a line.
        const swatch = s.isBar ? '<i class="bar"></i>' : "<i></i>";
        return `<span style="color:${colour}">${swatch}
          <span style="color:var(--text-2)">${esc(s.label)}</span>${shown}</span>`;
      })
      .join("");
  }

  empty(message) {
    if (this.plot) {
      this.plot.destroy();
      this.plot = null;
    }
    this.data = null;
    this.host.innerHTML = `<div class="chart-empty">${esc(message)}</div>`;
    if (this.spec.legendEl) this.spec.legendEl.innerHTML = "";
  }
}

export function rethemeAll() {
  for (const chart of charts) chart.retheme();
}

function colourWithAlpha(colour, alpha) {
  // color-mix keeps this working for whatever the theme supplies, including
  // named colours and hex of either length.
  return `color-mix(in srgb, ${colour} ${Math.round(alpha * 100)}%, transparent)`;
}

/**
 * Series custom draw: signed bars hanging off a zero baseline (PHD2 style).
 *
 * A series flagged `isBar` (with `sign` naming a peer series carrying ±1
 * directions) is painted as a vertical bar whose height is its value and whose
 * side is its sign. Bars sit on their series' own scale, so the right-hand
 * axis stays honest.
 */
function drawBars(plot, chart) {
  const { ctx } = plot;
  const t = chart.data?.[0] || [];
  if (!t.length) return;

  const series = chart.spec.series;
  const signByName = (name) => {
    if (!name) return null;
    const idx = series.findIndex((s) => s.label === name);
    return idx >= 0 ? chart.data?.[idx + 1] : null;
  };

  for (let si = 0; si < series.length; si++) {
    const spec = series[si];
    if (!spec.isBar) continue;
    const data = chart.data?.[si + 1];
    if (!data || !data.length) continue;
    const signs = signByName(spec.sign);
    const scale = spec.scale || "y2";
    const y0 = plot.valToPos(0, scale, true);
    const w = Math.max(1.5, Math.min(4, (plot.bbox.width / Math.max(1, t.length)) * 0.6));
    const colour = cssVar(spec.colour || SERIES_COLOURS[si % SERIES_COLOURS.length]);
    ctx.fillStyle = colourWithAlpha(colour, 0.55);
    for (let i = 0; i < data.length; i++) {
      const v = data[i];
      if (!v || t[i] === undefined) continue; // 0 = no pulse issued, not a bar
      const sign = signs && signs[i] ? (signs[i] < 0 ? -1 : 1) : 1;
      const x = plot.valToPos(t[i], "x", true) - w / 2;
      // Let the scale place the signed value; the zero line is the baseline.
      const yVal = plot.valToPos(sign * v, scale, true);
      ctx.fillRect(x, Math.min(y0, yVal), w, Math.abs(y0 - yVal));
    }
  }
}

/* ── small standalone marks ────────────────────────────────────────── */

/**
 * A sparkline for a stat tile.
 *
 * Deliberately axis-free and label-free: it answers "which way is this going"
 * and nothing else. The number above it is the value.
 */
export function sparkline(values, { width = 92, height = 34, colour = "var(--series-1)" } = {}) {
  const points = (values || []).filter((v) => Number.isFinite(v));
  if (points.length < 2) return "";
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = width / (points.length - 1);
  const path = points
    .map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)} ${(height - 3 - ((v - min) / span) * (height - 8)).toFixed(1)}`)
    .join(" ");
  return `<svg class="tile-spark" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${path}" fill="none" stroke="${colour}" stroke-width="1.8"/>
  </svg>`;
}

/** Sequence progress as a ring, with the count in the middle. */
export function progressRing(done, total, { size = 74 } = {}) {
  const radius = size / 2 - 6;
  const circumference = 2 * Math.PI * radius;
  const fraction = total > 0 ? Math.min(1, done / total) : 0;
  return `<svg viewBox="0 0 ${size} ${size}" class="ring" width="${size}" height="${size}">
    <circle cx="${size / 2}" cy="${size / 2}" r="${radius}" fill="none"
            stroke="var(--panel-3)" stroke-width="6"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${radius}" fill="none"
            stroke="var(--series-1)" stroke-width="6" stroke-linecap="round"
            stroke-dasharray="${(circumference * fraction).toFixed(1)} ${circumference.toFixed(1)}"
            transform="rotate(-90 ${size / 2} ${size / 2})"/>
    <text x="50%" y="50%" text-anchor="middle" dominant-baseline="central"
          fill="var(--text)" stroke="none"
          style="font:600 15px var(--sans); font-variant-numeric: tabular-nums">
      ${total > 0 ? Math.round(fraction * 100) : 0}%
    </text>
  </svg>`;
}
