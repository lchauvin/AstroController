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
import { LineChart, progressRing, sparkline } from "../charts.js";
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
 * The dashboard glance chart gets its y-range from the Settings page.
 * The Guiding page keeps auto-scale so the star and its wander are always
 * visible in full; the Dashboard gets a stable, comparable axis instead.
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
      xLabel: (hours) => (hours <= 0 ? "now" : `+${Math.round(hours)}h`),
      yLabel: (value) => `${Math.round(value)}%`,
      xTicks: (_p, ticks) => ticks.map((t) => (t <= 0 ? "now" : `+${Math.round(t)}h`)),
      yTicks: (_p, ticks) => ticks.map((t) => `${Math.round(t)}`),
    });
  }
  if (!hourly.length) {
    cloudChart.empty("No forecast.");
    return;
  }
  cloudChart.update([
    hourly.map((_h, i) => i),
    hourly.map((h) => h.cloud_total),
    hourly.map((h) => h.cloud_low),
    hourly.map((h) => h.precip_prob),
  ]);
}

/* ── sequence (compact) ──────────────────────────────────────────── */

function renderSequenceMini() {
  const seq = state.sequence || {};

  if (!seq.available) {
    const message = seq.error || "No sequence loaded";
    $("dash-seq-current").textContent = message;
    if ($("dash-seq-sub")) $("dash-seq-sub").textContent = "";
    setPill("dash-seq-pill", "idle");
    $("seq-ring").innerHTML = progressRing(0, 0);
    return;
  }

  const label = `${seq.done}/${seq.total}`;
  setPill("dash-seq-pill", label, seq.current_name ? "good" : "");
  $("dash-seq-current").textContent = seq.current_name || "Between steps";
  if ($("dash-seq-sub")) $("dash-seq-sub").textContent = `${seq.done} of ${seq.total} instructions done`;
  $("seq-ring").innerHTML = progressRing(seq.done || 0, seq.total || 0);
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

/* ── wiring ──────────────────────────────────────────────────────── */

export function registerDashboard() {
  on(["guiding", "phd2", "frames", "sequence", "nina", "weather", "config"], "kpis", renderKpis);
  on(["phd2", "guiding", "config"], "guiding-glance", renderGuidingGlance);
  on(["preview", "frames"], "preview", renderPreview);
  on(["frames"], "frame-stats", renderFrameStats);
  on(["weather", "sky"], "weather", renderWeather);
  on(["sequence"], "sequence-mini", renderSequenceMini);
  on(["nina", "phd2"], "equipment", renderEquipment);

  decorateMarks("#dash-framebox");

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
