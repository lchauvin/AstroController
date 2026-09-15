/* The Dashboard — monitoring page.
 *
 * Everything at a glance while imaging: the latest frame and its quality, the
 * guiding error rate, a seeing estimate derived from the guide star, the
 * weather and moon for the night, the running sequence, and which devices are
 * connected. Each renderer is "state in, DOM out", registered against the
 * topics it reads (see registerDashboard), so a guide step repaints the
 * guiding glance and nothing else.
 */
"use strict";

import { $, TOKEN, ago, clock, cssVar, duration, esc, fmt, icon, on, prefs, savePrefs, state, url } from "../core.js";
import { LineChart, sparkline } from "../charts.js";
import { DASH, decorateMarks, flagChip, listOr, rmsStatus, setPill, tile } from "../widgets.js";

const authHeaders = TOKEN ? { "X-Auth-Token": TOKEN } : {};

/* ── KPI strip ───────────────────────────────────────────────────── */

function renderKpis() {
  const rms = state.guiding?.rms;
  const phd2 = state.phd2 || {};
  const frame = (state.frames || [])[0];
  const weather = state.weather?.current;
  const graph = state.guiding?.graph;
  const devices = state.nina?.devices || [];
  const up = devices.filter((d) => d.connected).length;

  // The sparkline shows total excursion per sample, the same quantity the RMS
  // number summarises.
  const trace =
    graph?.ra?.length > 4
      ? graph.ra.map((ra, i) => Math.hypot(ra, graph.dec?.[i] ?? 0)).slice(-90)
      : [];

  $("kpi-tiles").innerHTML = [
    tile({
      label: "Guiding RMS",
      value: rms ? fmt(rms.rms_total) : DASH,
      unit: "″",
      sub: rms ? `RA ${fmt(rms.rms_ra)}  ·  Dec ${fmt(rms.rms_dec)}` : "not guiding",
      status: rmsStatus(rms?.rms_total),
      spark: sparkline(trace, { colour: cssVar("--series-1") }),
    }),
    tile({
      label: "Guide HFD",
      value: seeing() || DASH,
      unit: "″",
      sub: seeing() ? "guide-star HFD × scale" : phd2.pixel_scale ? "no HFD yet" : "no scale",
      status: seeing() ? (Number(seeing()) <= 2 ? "good" : Number(seeing()) <= 3.5 ? "warn" : "bad") : "idle",
    }),
    tile({
      label: "Star size",
      value: frame ? fmt(frame.hfr) : DASH,
      unit: "HFR",
      sub: frame ? hfrTrend(frame) : "no frames yet",
      status: frame?.flags?.cloud_suspect ? "warn" : frame ? "good" : "idle",
    }),
    tile({
      label: "Stars detected",
      value: frame?.stars ?? DASH,
      sub: frame ? `${esc(frame.filter || "?")} · ${fmt(frame.exposure_s, 0)}s` : "",
      status: frame?.flags?.cloud_suspect ? "warn" : frame ? "good" : "idle",
    }),
    tile({
      label: "Equipment",
      value: devices.length ? `${up}/${devices.length}` : DASH,
      sub: devices.length ? `${devices.length - up} not connected` : "waiting for NINA",
      status: !devices.length ? "idle" : up === devices.length ? "good" : "warn",
    }),
    tile({
      label: "Cloud now",
      value: weather ? fmt(weather.cloud_total, 0) : DASH,
      unit: "%",
      sub: weather ? `${fmt(weather.temp_c, 1)}°C · dew ${fmt(weather.dewpoint_c, 1)}°C` : state.weather?.error || "no forecast",
      status: !weather ? "idle" : weather.cloud_total > 60 ? "bad" : weather.cloud_total > 25 ? "warn" : "good",
    }),
  ].join("");
}

/**
 * Guide-star image scale readout, in arcseconds, when it can be derived.
 *
 * This is the guide star's half-flux diameter converted with the guider pixel
 * scale (HFD × scale). It is a guide-camera focus + seeing + scale product --
 * deliberately NOT labelled "seeing": at the guide camera's scale it reads
 * larger than true sky seeing, and it is only as accurate as the pixel scale
 * PHD2 reports (get_pixel_scale). Show-if-available; dash when either operand
 * is missing.
 */
function seeing() {
  const hfd = state.guiding?.rms?.hfd_med;
  const scale = state.phd2?.pixel_scale;
  if (!Number.isFinite(hfd) || !Number.isFinite(scale)) return null;
  return (hfd * scale).toFixed(2);
}

function hfrTrend(frame) {
  const median = frame.baseline?.median_hfr;
  if (!median || !frame.hfr) return `${esc(frame.filter || "?")} filter`;
  const delta = ((frame.hfr - median) / median) * 100;
  const arrow = delta > 3 ? "▲" : delta < -3 ? "▼" : "•";
  return `${arrow} ${fmt(Math.abs(delta), 0)}% vs ${esc(frame.filter || "?")} median`;
}

/* ── guiding glance ──────────────────────────────────────────────── */

let glanceChart = null;

/**
 * The dashboard glance chart gets its y-range from the Settings page, or
 * auto-scales when that preference is left blank. The Guiding page's own
 * trace uses a fixed, zero-centred range unconditionally (see
 * ERROR_RANGE_ARCSEC in pages/guiding.js) so this glance is the only chart
 * that ever auto-scales.
 */
function glanceYRange() {
  if (prefs.guideYmin === null && prefs.guideYmax === null) return null;
  return [prefs.guideYmin ?? null, prefs.guideYmax ?? null];
}

function renderGuidingGlance() {
  const phd2 = state.phd2 || {};
  const guiding = state.guiding || {};
  const rms = guiding.rms;

  const label = phd2.connected ? phd2.app_state || "?" : "disconnected";
  const kind = !phd2.connected ? "bad" : phd2.app_state === "Guiding" ? "good" : "warn";
  setPill("dash-guide-state", label, kind);

  $("dash-guide-metrics").innerHTML = [
    ["RMS total", rms ? `${fmt(rms.rms_total)}″` : DASH],
    ["RA", rms ? `${fmt(rms.rms_ra)}″` : DASH],
    ["Dec", rms ? `${fmt(rms.rms_dec)}″` : DASH],
    ["SNR", rms ? fmt(rms.snr_med, 1) : DASH],
  ]
    .map(([name, value]) => `<div class="metric"><b>${esc(value)}</b><label>${esc(name)}</label></div>`)
    .join("");

  const disturbed = guiding.disturbed || [];
  const banner = $("guide-disturbed");
  if (banner) {
    banner.hidden = disturbed.length === 0;
    if (disturbed.length) {
      banner.innerHTML = `${icon("alert")}<span>Measurement paused: ${esc(disturbed.join(", "))}</span>`;
    }
  }

  drawGlance(guiding.graph);
}

function drawGlance(graph) {
  const host = $("dash-guide-chart");
  if (!host) return;
  if (!glanceChart) {
    glanceChart = new LineChart(host, {
      legendEl: $("dash-guide-legend"),
      series: [
        { label: "RA", width: 1.6 },
        { label: "Dec", width: 1.6 },
      ],
      yRange: glanceYRange(),
      xLabel: (seconds) => `${Math.abs(Math.round(seconds))}s ago`,
      yLabel: (value) => `${value.toFixed(2)}″`,
      yTicks: (_p, ticks) => ticks.map((t) => t.toFixed(1)),
      xTicks: (_p, ticks) => ticks.map((t) => `${Math.round(t)}s`),
    });
    glanceChart._lastYRange = JSON.stringify(glanceYRange());
  }
  if (!graph || !graph.t || graph.t.length < 2) {
    glanceChart.empty("Waiting for guide steps…");
    return;
  }
  glanceChart.update([graph.t, graph.ra, graph.dec]);
}

/* ── latest-frame stats & flags ──────────────────────────────────── */

function renderFrameStats() {
  const host = $("frame-stats");
  if (!host) return;
  const frame = (state.frames || [])[0];

  if (!frame) {
    $("frame-flags").innerHTML = "";
    host.innerHTML = "";
    setPill("frame-verdict", "–");
    return;
  }

  const flags = frame.flags || {};
  const chips = [];
  if (flags.saturated) chips.push(flagChip("warn", "alert", "saturated"));
  if (flags.cloud_suspect) chips.push(flagChip("bad", "weather", "cloud suspected"));
  if (flags.tracking_suspect) chips.push(flagChip("warn", "guiding", "tracking"));
  if (!chips.length) chips.push(flagChip("ok", "check", "clean"));
  $("frame-flags").innerHTML = chips.join("");
  setPill(
    "frame-verdict",
    flags.cloud_suspect || flags.saturated || flags.tracking_suspect ? "flagged" : "clean",
    flags.cloud_suspect ? "bad" : flags.saturated || flags.tracking_suspect ? "warn" : "good"
  );

  host.innerHTML = [
    ["Target", frame.target || DASH],
    ["Filter", frame.filter || DASH],
    ["Exposure", frame.exposure_s ? `${fmt(frame.exposure_s, 0)} s` : DASH],
    // No eccentricity field exists on the frame payload; HFR ± its spread is
    // the focus-quality readout here (see plan.md §1.1).
    ["HFR", `${fmt(frame.hfr)} ± ${fmt(frame.hfr_stdev)}`],
    ["Stars", frame.stars ?? DASH],
    ["Median / max ADU", `${fmt(frame.median, 0)} / ${fmt(frame.max, 0)}`],
    ["Sensor", frame.temperature !== null && frame.temperature !== undefined ? `${fmt(frame.temperature, 1)} °C` : DASH],
    ["Captured", frame.date ? `${clock(frame.date)} · ${ago(frame.date)}` : DASH],
  ]
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`)
    .join("");
}

/* ── weather & sky ───────────────────────────────────────────────── */

let cloudChart = null;

function renderWeather() {
  const weather = state.weather || {};
  const now = weather.current;
  const summary = weather.summary || {};

  if (weather.error) {
    setPill("weather-pill", "unavailable", "warn");
    $("weather-metrics").innerHTML = `<div class="list-empty">${esc(weather.error)}</div>`;
  } else if (now) {
    setPill(
      "weather-pill",
      summary.clear_hours !== undefined ? `${summary.clear_hours}/${summary.hours}h clear` : "now",
      summary.rain_risk ? "bad" : summary.dew_risk ? "warn" : "good"
    );
    $("weather-metrics").innerHTML = [
      ["Cloud", `${fmt(now.cloud_total, 0)}%`],
      ["Low cloud", `${fmt(now.cloud_low, 0)}%`],
      ["Temp", `${fmt(now.temp_c, 1)}°`],
      ["Dew point", `${fmt(now.dewpoint_c, 1)}°`],
      ["Wind", `${fmt(now.wind_ms, 1)} m/s`],
    ]
      .map(([k, v]) => `<div class="metric"><b>${esc(v)}</b><label>${esc(k)}</label></div>`)
      .join("");
  }

  drawCloud(weather.hourly || []);

  const sky = state.sky || {};
  $("moon-info").innerHTML = sky.phase_name
    ? `${icon("moon")}<span>${esc(sky.phase_name)}, ${Math.round((sky.illumination || 0) * 100)}% lit` +
      (sky.altitude_deg !== null && sky.altitude_deg !== undefined
        ? `, ${fmt(sky.altitude_deg, 0)}° altitude (${sky.up ? "up" : "below horizon"})`
        : "") +
      (sky.precise ? "" : " · approximate") +
      "</span>"
    : "";
}

const CLOUD_CHART_HOURS = 12;

/**
 * The current hour plus the next `count - 1`, so the chart always reads as
 * "the next N hours" rather than the whole multi-day forecast Open-Meteo
 * returns. Forecast timestamps are local wall-clock strings with no zone
 * (Open-Meteo's `timezone=auto`), so a bare `Date.parse` reads them as the
 * browser's local time -- correct as long as the browser and the observing
 * site share a zone, which holds for this single-site dashboard.
 */
function upcomingHours(hourly, count) {
  if (!hourly.length) return [];
  const now = Date.now();
  let start = 0;
  for (let i = 0; i < hourly.length; i++) {
    const stamp = Date.parse(hourly[i].time);
    if (!Number.isNaN(stamp) && stamp <= now) start = i;
  }
  return hourly.slice(start, start + count);
}

// The x-axis plots plain hour indices (0..count-1), not epoch time: the
// points this chart shows are always exactly one hour apart, so an index
// domain gets uPlot's default tick spacing to land on whole hours. The
// points currently on screen live here so the tick formatters -- set up once
// at chart construction -- can still look up each index's real clock time.
let cloudPoints = [];

/** Site-local clock time for the forecast hour at this x-axis index. */
function hourLabel(index) {
  const point = cloudPoints[Math.round(index)];
  if (!point) return "";
  const date = new Date(Date.parse(point.time));
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function drawCloud(hourly) {
  const host = $("cloud-chart");
  if (!host) return;
  if (!cloudChart) {
    cloudChart = new LineChart(host, {
      legendEl: $("cloud-legend"),
      series: [
        { label: "Cloud", fill: 0.18, width: 1.6 },
        { label: "Low cloud", width: 1.6 },
        { label: "Rain", width: 1.4, dash: [4, 3] },
      ],
      yRange: [0, 100],
      xLabel: hourLabel,
      yLabel: (value) => `${Math.round(value)}%`,
      xTicks: (_p, ticks) => ticks.map(hourLabel),
      yTicks: (_p, ticks) => ticks.map((t) => `${Math.round(t)}`),
    });
  }
  cloudPoints = upcomingHours(hourly, CLOUD_CHART_HOURS);
  if (!cloudPoints.length) {
    cloudChart.empty("No forecast.");
    return;
  }
  cloudChart.update([
    cloudPoints.map((_h, i) => i),
    cloudPoints.map((h) => h.cloud_total),
    cloudPoints.map((h) => h.cloud_low),
    cloudPoints.map((h) => h.precip_prob),
  ]);
}

/* ── sequence (current step on top, full tree below) ─────────────── */

function renderSequenceMini() {
  const seq = state.sequence || {};

  if (!seq.available) {
    const message = seq.error || "No sequence loaded";
    $("dash-seq-current").textContent = message;
    if ($("dash-seq-sub")) $("dash-seq-sub").textContent = "";
    setPill("dash-seq-pill", "idle");
    $("dash-seq-tree").innerHTML = "";
    return;
  }

  setPill("dash-seq-pill", `${seq.done}/${seq.total}`, seq.current_name ? "good" : "");
  $("dash-seq-current").textContent = seq.current_name || "Between steps";
  if ($("dash-seq-sub")) $("dash-seq-sub").textContent = `${seq.done} of ${seq.total} instructions done`;

  renderSequenceTree(seq);
}

/* The whole sequence as one indent-and-connector tree, not a collapsible
   outline: the interesting question at 2am is "where in the night am I", and
   the answer is easier when finished sections are visibly above the running
   one rather than folded away. Containers are marked with their loop
   condition so the instruction list reads as the night's plan, not a flat
   run-on of take-exposure rows. A loop container is one carrying a "Loop" /
   "Repeat" condition; anything else is a plain block. */
function renderSequenceTree(seq) {
  const steps = seq.steps || [];
  if (!steps.length) {
    $("dash-seq-tree").innerHTML = `<div class="list-empty">Empty sequence.</div>`;
    return;
  }

  // Ancestor-at-depth array tells each row whether a vertical continuation
  // guide must be drawn at that level: a parent's sibling further down still
  // owns the rail under it. Rebuilding the flat list each poll is cheap for
  // the few hundred steps a sequence ever has.
  const html = [];
  const hasSiblingBelow = []; // hasSiblingBelow[d] === true → draw rail at depth d

  steps.forEach((step, i) => {
    // Look ahead for another step at an equal or shallower depth: that ends
    // this branch, so the connector terminates instead of running on.
    const next = steps[i + 1];
    const continuing = !!next && next.depth >= step.depth;
    hasSiblingBelow.length = step.depth + 1;
    hasSiblingBelow[step.depth] = continuing;

    const status = (step.status || "").toLowerCase();
    const cls = [
      "step",
      step.is_container ? "container" : "leaf",
      status === "running" ? "running" : "",
      status === "finished" ? "finished" : "",
      status === "skipped" ? "skipped" : "",
      status === "failed" ? "failed" : "",
    ].filter(Boolean).join(" ");

    const guides = [];
    for (let d = 0; d < step.depth; d++) {
      // A rail that continues past the corner of its own level.
      guides.push(
        `<span class="guide ${d < step.depth - 1 ? (hasSiblingBelow[d] ? "rail" : "void") : (continuing ? "tee" : "corner")}"></span>`
      );
    }

    const isLoop = step.is_container && (step.conditions || []).some((c) => /loop|repeat/i.test(c));
    const marker = step.is_container
      ? `<span class="step-marker ${isLoop ? "loop" : "block"}" title="${esc((step.conditions || []).join(", ") || "container")}">
           ${isLoop ? `<svg viewBox="0 0 24 24"><use href="#i-loop"/></svg>` : ""}
         </span>`
      : `<span class="step-dot"></span>`;

    const sub = isLoop ? `<span class="step-sub">${esc((step.conditions || [])[0] || "")}</span>` : "";

    html.push(
      `<div class="${cls}" data-step="${esc(step.id)}">
         ${guides.join("")}${marker}
         <span class="name">${esc(step.name)}</span>${sub}
         <span class="status">${esc(step.status || "")}</span>
       </div>`
    );
  });

  const host = $("dash-seq-tree");
  host.innerHTML = html.join("");

  // Keep the running row in view without yanking the scroll on every poll:
  // only nudge when the user hasn't scrolled it out of sight on purpose.
  if (!host.dataset.scrolled) {
    const running = host.querySelector(".step.running");
    if (running) running.scrollIntoView({ block: "nearest" });
  }
}

/* ── equipment (sidebar rail) ────────────────────────────────────── */

const DEVICE_ICONS = {
  camera: "camera", mount: "mount", focuser: "focuser", filterwheel: "filter",
  guider: "guider", flat: "flat", weather: "weather",
};

function renderEquipment() {
  const nina = state.nina || {};
  const devices = [...(nina.devices || [])];

  // Our own PHD2 socket is a connection the operator can act on, and it is not
  // the same thing as NINA's guider abstraction -- when the two disagree, that
  // disagreement is the most useful thing on the page.
  const phd2 = state.phd2 || {};
  devices.push({
    key: "phd2",
    label: "PHD2",
    icon: "guider",
    connected: !!phd2.connected,
    name: "PHD2 (direct)",
    detail: phd2.connected
      ? [phd2.app_state, phd2.profile_name].filter(Boolean).join(" · ")
      : "not reachable",
  });

  const up = devices.filter((d) => d.connected).length;
  setPill(
    "equip-summary",
    devices.length ? `${up}/${devices.length} connected` : "waiting",
    !devices.length ? "" : up === devices.length ? "good" : "warn"
  );

  const list = $("rail-equipment-list");
  if (!list) return;
  if (!devices.length) {
    list.innerHTML = `<div class="rail-list-empty">${esc(nina.error || "Waiting for NINA…")}</div>`;
    return;
  }

  list.innerHTML = devices
    .map((device) => {
      const glyph = DEVICE_ICONS[device.icon] || DEVICE_ICONS[device.key] || "guider";
      const detail = device.connected ? device.detail : device.error || device.detail || "";
      // The word next to the dot is what carries the state; the colour repeats
      // it. In night-vision mode the colour is gone and the word is not.
      return `<div class="rail-device ${device.connected ? "is-up" : "is-down"}">
        <span class="device-icon">${icon(glyph)}</span>
        <span class="device-body">
          <span class="device-name">${esc(device.name || device.label)}</span>
          <span class="device-detail">${esc(detail || device.label)}</span>
        </span>
        <span class="device-state">
          <span class="dot ${device.connected ? "live" : ""}"></span>
          ${device.connected ? "on" : "off"}
        </span>
      </div>`;
    })
    .join("");
}

/* ── the captured frame (viewer) ─────────────────────────────────── */

let dashUrl = null;
let dashShownKey = null; /* render key that produced the bytes on screen */

/**
 * Request width measured from the box the image lands in, capped at 2x for
 * retina and clamped so a collapsed layout cannot ask for a postage stamp.
 */
function dashWidth() {
  const box = $("dash-framebox");
  const css = box?.clientWidth || 600;
  const scale = Math.min(window.devicePixelRatio || 1, 2);
  return Math.max(400, Math.min(1400, Math.round(css * scale)));
}

/** A key that changes exactly when the rendered bytes would differ. */
function renderKey(preview, width) {
  return [preview.token, width, prefs.stretchBackground, prefs.stretchWhite, prefs.stretchInvert].join("|");
}

async function loadFrame({ force = false } = {}) {
  const preview = state.preview || {};
  const img = $("dash-frame");
  const empty = $("dash-frame-empty");
  if (!img) return;

  if (!preview.available || !preview.token) {
    img.removeAttribute("src");
    empty.hidden = false;
    empty.textContent = preview.reason || "Waiting for the first frame…";
    dashShownKey = null;
    return;
  }

  const width = dashWidth();
  const key = renderKey(preview, width);
  if (!force && dashShownKey === key) return;

  try {
    const response = await fetch(
      url("/api/frame/latest.png", {
        width,
        background: prefs.stretchBackground,
        white: prefs.stretchWhite,
        invert: prefs.stretchInvert ? "true" : "false",
        v: preview.token,
      }),
      { headers: authHeaders }
    );
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {
        /* the body may not be JSON */
      }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const next = URL.createObjectURL(blob);
    if (dashUrl) URL.revokeObjectURL(dashUrl);
    dashUrl = next;
    img.src = next;
    empty.hidden = true;
    dashShownKey = key;
  } catch (err) {
    img.removeAttribute("src");
    empty.hidden = false;
    empty.textContent = err.message;
    dashShownKey = null;
  }
}

function renderPreview() {
  const preview = state.preview || {};
  const frame = (state.frames || [])[0];

  const sourceLabel = !preview.available ? "no image" : preview.source === "share" ? "share" : "NINA preview";
  const source = $("frame-source");
  if (source) {
    source.textContent = sourceLabel;
    source.className = `pill ${preview.available ? "good" : ""}`;
  }

  const caption = [];
  if (preview.filename) caption.push(`<b>${esc(preview.filename)}</b>`);
  if (frame) {
    if (frame.target) caption.push(esc(frame.target));
    if (frame.filter) caption.push(`${esc(frame.filter)} · ${esc(fmt(frame.exposure_s, 0))}s`);
    if (Number.isFinite(frame.hfr)) caption.push(`HFR <b>${esc(fmt(frame.hfr))}</b>`);
    if (frame.stars !== null && frame.stars !== undefined) caption.push(`<b>${esc(frame.stars)}</b> stars`);
  }
  const host = $("dash-frame-caption");
  if (host) host.innerHTML = caption.map((part) => `<span>${part}</span>`).join("");

  loadFrame();
}

/* ── guide camera field (dashboard) ───────────────────────────────
 *
 * The dashboard is the page left open all night, so PHD2's widest available
 * view -- the lock region, up to its 255px cap -- lives here: a guide star
 * drifting past neighbours is the first visible symptom of half the failures
 * that matter. Polled only while this page is on screen, for the same reason
 * the Guiding page's tight crop is: the request shares a socket with the
 * guide steps.
 */

let dashGuideTimer = null;
let dashGuideUrl = null;

/** What the field is doing right now, in operator terms. */
function dashGuideState() {
  const phd2 = state.phd2 || {};
  const guiding = state.guiding || {};
  const activity = guiding.activity;
  const disturbed = guiding.disturbed || [];
  if (!phd2.connected) return ["offline", "bad"];
  // The runtime stamps brief states (dither, star lost) with a short expiry so
  // the word stays up long enough to read; the exclusion reasons cover the
  // longer ones (settling, paused).
  if (activity === "star lost" || disturbed.some((d) => d.startsWith("star_lost"))) return ["star lost", "bad"];
  if (activity === "dithering" || disturbed.some((d) => d.startsWith("dither"))) return ["dithering", "warn"];
  if (phd2.paused || disturbed.includes("paused")) return ["paused", "warn"];
  if (phd2.settling || disturbed.includes("settling")) return ["settling", "warn"];
  if (phd2.calibrating || disturbed.includes("calibrating")) return ["calibrating", "warn"];
  if (phd2.app_state === "Guiding") return ["guiding", "good"];
  return [phd2.app_state || "idle", ""];
}

function renderDashGuideMetrics() {
  const rms = state.guiding?.rms;
  const host = $("dash-star-metrics");
  if (!host) return;
  host.innerHTML = [
    ["SNR", rms ? fmt(rms.snr_med, 1) : DASH],
    ["HFD", rms ? `${fmt(rms.hfd_med)} px` : DASH],
    ["RMS", rms ? `${fmt(rms.rms_total)}″` : DASH],
  ]
    .map(([k, v]) => `<div class="metric"><b>${esc(v)}</b><label>${esc(k)}</label></div>`)
    .join("");

  const [label, kind] = dashGuideState();
  setPill("dash-star-state", label, kind);
}

async function pollDashGuide() {
  const phd2 = state.phd2 || {};
  const img = $("dash-guide-image");
  const empty = $("dash-guide-empty");
  if (!img) return;

  if (!phd2.connected) {
    setDashGuideEmpty("PHD2 is not connected");
    return;
  }

  // Prefer the full FITS field PHD2 writes via save_image when it exists; the
  // 63x63 live crop is the fallback while none has landed.
  const field = state.guide_image;
  if (field?.token && field.token !== img.dataset.token) {
    try {
      const response = await fetch(url("/api/guide-field.png", { width: 900, v: field.token }), { headers: authHeaders });
      if (response.ok) {
        const blob = await response.blob();
        const next = URL.createObjectURL(blob);
        if (dashGuideUrl) URL.revokeObjectURL(dashGuideUrl);
        dashGuideUrl = next;
        img.src = next;
        img.dataset.token = field.token;
        empty.hidden = true;
        return;
      }
      // A 404 here just means the dump hasn't landed yet; fall through to the
      // live crop.
    } catch (_) {
      /* network trouble: fall through to the live crop */
    }
  }

  try {
    const response = await fetch(url("/api/guide-view.png"), { headers: authHeaders });
    if (!response.ok) {
      let detail = response.status === 404 ? "Not guiding" : `Error ${response.status}`;
      try {
        const body = await response.json();
        if (body?.detail) detail = body.detail;
      } catch (_) { /* a non-JSON body still has the status */ }
      setDashGuideEmpty(detail);
      return;
    }
    const blob = await response.blob();
    const next = URL.createObjectURL(blob);
    if (dashGuideUrl) URL.revokeObjectURL(dashGuideUrl);
    dashGuideUrl = next;
    img.src = next;
    img.dataset.token = "";
    empty.hidden = true;
  } catch (_) {
    setDashGuideEmpty("No guide image");
  }
}

function setDashGuideEmpty(message) {
  const img = $("dash-guide-image");
  const empty = $("dash-guide-empty");
  if (!img || !empty) return;
  img.removeAttribute("src");
  empty.hidden = false;
  empty.textContent = message;
}

/** Poll only while the Dashboard page is on screen (same discipline as the
 *  Guiding page's star poll). */
export function syncDashStarPolling() {
  const wanted = $("page-dashboard")?.classList.contains("is-active") && document.visibilityState === "visible";
  if (wanted && !dashGuideTimer) {
    pollDashGuide();
    dashGuideTimer = setInterval(pollDashGuide, 2500);
  } else if (!wanted && dashGuideTimer) {
    clearInterval(dashGuideTimer);
    dashGuideTimer = null;
  }
}

/* ── wiring ──────────────────────────────────────────────────────── */

export function registerDashboard() {
  on(["guiding", "phd2", "frames", "sequence", "nina", "weather", "config"], "kpis", renderKpis);
  on(["phd2", "guiding", "config"], "guiding-glance", renderGuidingGlance);
  on(["preview", "frames"], "preview", renderPreview);
  on(["frames"], "frame-stats", renderFrameStats);
  on(["weather", "sky"], "weather", renderWeather);
  on(["sequence"], "sequence-mini", renderSequenceMini);
  on(["nina", "phd2"], "equipment", renderEquipment);
  on(["guiding", "phd2", "guide_image"], "dash-guide", renderDashGuideMetrics);

  decorateMarks("#dash-framebox");
  decorateMarks("#dash-guideview");

  // Scrolling the sequence tree by hand suspends the "follow the running
  // step" behaviour until it is scrolled back to where the action is.
  $("dash-seq-tree")?.addEventListener("scroll", (event) => {
    const el = event.target;
    const running = el.querySelector(".step.running");
    el.dataset.scrolled = running ? "1" : "";
  });

  document.addEventListener("visibilitychange", syncDashStarPolling);

  $("dash-frame-refresh")?.addEventListener("click", () => loadFrame({ force: true }));

  // Stretch + lightbox controls (moved from imaging.js).
  const bg = $("stretch-bg");
  const white = $("stretch-white");
  const invert = $("stretch-invert");
  if (bg && white && invert) {
    bg.value = prefs.stretchBackground;
    white.value = prefs.stretchWhite;
    invert.checked = !!prefs.stretchInvert;

    let debounce = null;
    const restretch = () => {
      prefs.stretchBackground = Number(bg.value);
      prefs.stretchWhite = Number(white.value);
      prefs.stretchInvert = invert.checked;
      savePrefs();
      clearTimeout(debounce);
      // Each change re-renders the FITS server-side, so wait for the slider to
      // stop rather than re-reading a 50MB file on every pixel of travel.
      debounce = setTimeout(() => loadFrame({ force: true }), 260);
    };
    bg.addEventListener("input", restretch);
    white.addEventListener("input", restretch);
    invert.addEventListener("change", restretch);
    $("stretch-reset")?.addEventListener("click", () => {
      bg.value = prefs.stretchBackground = 0.18;
      white.value = prefs.stretchWhite = 99.9;
      invert.checked = prefs.stretchInvert = false;
      savePrefs();
      loadFrame({ force: true });
    });
  }

  // A resize that changes the box width should re-request a sharper/softer
  // render; debounced so a drag does not hammer the FITS reader.
  const box = $("dash-framebox");
  if (box && typeof ResizeObserver !== "undefined") {
    let last = box.clientWidth;
    let timer = null;
    new ResizeObserver(() => {
      if (Math.abs(box.clientWidth - last) < 24) return;
      last = box.clientWidth;
      clearTimeout(timer);
      timer = setTimeout(() => loadFrame({ force: true }), 300);
    }).observe(box);
  }

  const lightbox = $("lightbox");
  $("dash-frame")?.addEventListener("click", (event) => {
    if (!event.target.src) return;
    $("lightbox-img").src = event.target.src;
    lightbox.hidden = false;
  });
  $("lightbox-close")?.addEventListener("click", () => (lightbox.hidden = true));
  lightbox?.addEventListener("click", (event) => {
    if (event.target === lightbox) lightbox.hidden = true;
  });
  addEventListener("keydown", (event) => {
    if (event.key === "Escape") lightbox.hidden = true;
  });
}

/** Re-fit the frame + charts; called when the page is shown. */
export function refresh({ force = false } = {}) {
  loadFrame({ force });

  // yRange may have changed in Settings while this page was hidden; the
  // uPlot range is set at construction, so rebuild if it has moved.
  const next = JSON.stringify(glanceYRange());
  if (glanceChart && next !== glanceChart._lastYRange) {
    glanceChart.plot?.destroy();
    glanceChart = null;
  }
  drawGlance(state.guiding?.graph);

  drawCloud(state.weather?.hourly || []);
}
