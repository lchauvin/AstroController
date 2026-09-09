/* Every panel's render function.
 *
 * Each one is pure "state in, DOM out" and is registered against the state
 * topics it depends on, so a guide step repaints the guiding panels and
 * nothing else.
 */
"use strict";

import { $, ago, clock, cssVar, duration, esc, fmt, icon, on, prefs, state } from "./core.js";
import { LineChart, progressRing, sparkline } from "./charts.js";

const DASH = "–";

/** Target RMS comes from the config; 0.6" is the built-in default. */
const targetRms = () => state.config?.target_rms || 0.6;

/**
 * Status band for a guiding RMS.
 *
 * Relative to the configured target rather than absolute: 0.9" is excellent
 * on a 250mm refractor and dreadful on a 2m RC.
 */
function rmsStatus(total) {
  if (total === null || total === undefined) return "idle";
  const target = targetRms();
  if (total <= target * 1.15) return "good";
  if (total <= target * 2) return "warn";
  return "bad";
}

function tile({ label, value, unit = "", sub = "", status = "idle", spark = "" }) {
  return `<article class="tile is-${status}">
    <div class="tile-label">${esc(label)}</div>
    <div class="tile-value">${esc(value)}${unit ? `<span class="tile-unit">${esc(unit)}</span>` : ""}</div>
    <div class="tile-sub">${sub}</div>
    ${spark}
  </article>`;
}

function setPill(id, text, kind = "") {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.className = `pill ${kind}`;
}

function listOr(host, html, emptyText) {
  host.innerHTML = html || `<div class="list-empty">${esc(emptyText)}</div>`;
}

/* ── top bar ───────────────────────────────────────────────────────── */

function renderTopbar() {
  const rms = state.guiding?.rms;
  const seq = state.sequence;
  const cloud = state.weather?.current?.cloud_total;
  const chips = [];

  chips.push(chip("RMS", rms ? `${fmt(rms.rms_total)}″` : DASH));
  chips.push(chip("PHD2", state.phd2?.connected ? state.phd2.app_state || "?" : "offline"));
  if (seq?.available) chips.push(chip("Sequence", `${seq.done}/${seq.total}`));
  if (cloud !== undefined && cloud !== null) chips.push(chip("Cloud", `${fmt(cloud, 0)}%`));

  $("topbar-chips").innerHTML = chips.join("");
}

const chip = (label, value) =>
  `<span class="chip"><span class="chip-label">${esc(label)}</span><b>${esc(value)}</b></span>`;

/* ── dashboard tiles ───────────────────────────────────────────────── */

function renderKpis() {
  const rms = state.guiding?.rms;
  const phd2 = state.phd2 || {};
  const frame = (state.frames || [])[0];
  const seq = state.sequence || {};
  const weather = state.weather?.current;
  const graph = state.guiding?.graph;

  // The sparkline shows total excursion per sample, which is the same quantity
  // the RMS number summarises.
  const trace =
    graph?.ra?.length > 4
      ? graph.ra.map((ra, i) => Math.hypot(ra, graph.dec?.[i] ?? 0)).slice(-90)
      : [];

  const devices = state.nina?.devices || [];
  const up = devices.filter((d) => d.connected).length;

  $("kpi-tiles").innerHTML = [
    tile({
      label: "Guiding RMS",
      value: rms ? fmt(rms.rms_total) : DASH,
      unit: "″",
      sub: rms
        ? `RA ${fmt(rms.rms_ra)}  ·  Dec ${fmt(rms.rms_dec)}`
        : "not guiding",
      status: rmsStatus(rms?.rms_total),
      spark: sparkline(trace, { colour: cssVar("--series-1") }),
    }),
    tile({
      label: "Guider",
      value: phd2.connected ? phd2.app_state || "?" : "Offline",
      sub: phd2.connected
        ? phd2.guiding_for_s
          ? `guiding for ${duration(phd2.guiding_for_s)}`
          : phd2.profile_name || ""
        : "PHD2 not reachable",
      status: !phd2.connected ? "bad" : phd2.app_state === "Guiding" ? "good" : "warn",
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
      sub: weather
        ? `${fmt(weather.temp_c, 1)}°C · dew ${fmt(weather.dewpoint_c, 1)}°C`
        : state.weather?.error || "no forecast",
      status: !weather ? "idle" : weather.cloud_total > 60 ? "bad" : weather.cloud_total > 25 ? "warn" : "good",
    }),
  ].join("");

  // Sequence progress lives on its own card, with the ring.
  $("seq-ring").innerHTML = progressRing(seq.done || 0, seq.total || 0);
}

function hfrTrend(frame) {
  const median = frame.baseline?.median_hfr;
  if (!median || !frame.hfr) return `${esc(frame.filter || "?")} filter`;
  const delta = ((frame.hfr - median) / median) * 100;
  const arrow = delta > 3 ? "▲" : delta < -3 ? "▼" : "•";
  return `${arrow} ${fmt(Math.abs(delta), 0)}% vs ${esc(frame.filter || "?")} median`;
}

/* ── guiding ───────────────────────────────────────────────────────── */

let guideChart = null;
let dashGuideChart = null;

function guideSpec(legendEl) {
  return {
    legendEl,
    series: [
      { label: "RA", width: 1.8 },
      { label: "Dec", width: 1.8 },
    ],
    xLabel: (seconds) => `${Math.abs(Math.round(seconds))}s ago`,
    yLabel: (value) => `${value.toFixed(2)}″`,
    yTicks: (_plot, ticks) => ticks.map((t) => t.toFixed(1)),
    xTicks: (_plot, ticks) => ticks.map((t) => `${Math.round(t)}s`),
  };
}

function renderGuiding() {
  const phd2 = state.phd2 || {};
  const guiding = state.guiding || {};
  const rms = guiding.rms;

  const label = phd2.connected ? phd2.app_state || "?" : "disconnected";
  const kind = !phd2.connected ? "bad" : phd2.app_state === "Guiding" ? "good" : "warn";
  setPill("guide-state", label, kind);
  setPill("dash-guide-state", label, kind);

  const metrics = [
    ["RMS total", rms ? `${fmt(rms.rms_total)}″` : DASH],
    ["RA", rms ? `${fmt(rms.rms_ra)}″` : DASH],
    ["Dec", rms ? `${fmt(rms.rms_dec)}″` : DASH],
    ["SNR", rms ? fmt(rms.snr_med, 1) : DASH],
  ]
    .map(([name, value]) => `<div class="metric"><b>${esc(value)}</b><label>${esc(name)}</label></div>`)
    .join("");
  $("dash-guide-metrics").innerHTML = metrics;

  $("guide-tiles").innerHTML = [
    tile({
      label: "Total RMS",
      value: rms ? fmt(rms.rms_total) : DASH,
      unit: "″",
      sub: rms ? `${rms.n} samples over ${duration(rms.usable_seconds)}` : "not guiding",
      status: rmsStatus(rms?.rms_total),
    }),
    tile({ label: "RA RMS", value: rms ? fmt(rms.rms_ra) : DASH, unit: "″",
           sub: rms ? `peak ${fmt(rms.peak_ra)}″` : "", status: rmsStatus(rms?.rms_ra) }),
    tile({ label: "Dec RMS", value: rms ? fmt(rms.rms_dec) : DASH, unit: "″",
           sub: rms ? `peak ${fmt(rms.peak_dec)}″` : "", status: rmsStatus(rms?.rms_dec) }),
    tile({ label: "Guide star SNR", value: rms ? fmt(rms.snr_med, 1) : DASH,
           sub: rms ? `HFD ${fmt(rms.hfd_med)} px` : "",
           status: !rms ? "idle" : rms.snr_med < 10 ? "bad" : rms.snr_med < 20 ? "warn" : "good" }),
    tile({ label: "Pixel scale", value: phd2.pixel_scale ? fmt(phd2.pixel_scale) : DASH,
           unit: "″/px", sub: phd2.exposure_ms ? `${phd2.exposure_ms} ms exposure` : "" }),
    tile({ label: "Guiding for", value: phd2.guiding_for_s ? duration(phd2.guiding_for_s) : DASH,
           sub: phd2.paused ? "paused" : phd2.settling ? "settling" : phd2.calibrating ? "calibrating" : "",
           status: phd2.paused ? "warn" : phd2.guiding_for_s ? "good" : "idle" }),
  ].join("");

  const disturbed = guiding.disturbed || [];
  for (const id of ["guide-disturbed", "guide-disturbed-2"]) {
    const banner = $(id);
    if (!banner) continue;
    banner.hidden = disturbed.length === 0;
    if (disturbed.length) {
      banner.innerHTML = `${icon("alert")}<span>Measurement paused: ${esc(disturbed.join(", "))}</span>`;
    }
  }

  const pause = $("pause-button");
  if (pause) pause.textContent = phd2.paused ? "Resume" : "Pause";

  drawGuide(guiding.graph);
  renderParams(phd2);
}

function drawGuide(graph) {
  const pairs = [
    [$("guide-chart"), $("guide-legend"), (c) => (guideChart = c), () => guideChart],
    [$("dash-guide-chart"), $("dash-guide-legend"), (c) => (dashGuideChart = c), () => dashGuideChart],
  ];
  for (const [host, legend, set, get] of pairs) {
    if (!host) continue;
    let chart = get();
    if (!chart) {
      chart = new LineChart(host, guideSpec(legend));
      set(chart);
    }
    if (!graph || !graph.t || graph.t.length < 2) {
      chart.empty("Waiting for guide steps…");
      continue;
    }
    chart.update([graph.t, graph.ra, graph.dec]);
  }
}

function renderParams(phd2) {
  const params = phd2.params || {};
  const available = phd2.available_params || {};
  const rows = Object.entries(params)
    .filter(([name]) => {
      const [axis, param] = name.split(".");
      const exposed = available[axis];
      return !exposed || exposed.includes(param);
    })
    .map(([name, value]) => {
      const [axis, param] = name.split(".");
      return `<tr>
        <td>${esc(axis)} · ${esc(param)}</td>
        <td><input type="number" step="0.01" value="${esc(value)}"
              data-axis="${esc(axis)}" data-param="${esc(param)}"
              aria-label="${esc(axis)} ${esc(param)}"></td>
      </tr>`;
    })
    .join("");
  $("guide-params").innerHTML =
    rows || `<tr><td class="list-empty">No parameters read yet.</td></tr>`;
  setPill("params-pill", phd2.dec_guide_mode ? `dec ${phd2.dec_guide_mode}` : "–");
}

/* ── sequence ──────────────────────────────────────────────────────── */

function renderSequence() {
  const seq = state.sequence || {};

  if (!seq.available) {
    const message = seq.error || "No sequence loaded";
    $("seq-current").textContent = message;
    $("dash-seq-current").textContent = message;
    $("dash-seq-sub").textContent = "";
    $("seq-tree").innerHTML = "";
    setPill("seq-pill", "idle");
    setPill("dash-seq-pill", "idle");
    return;
  }

  const label = `${seq.done}/${seq.total}`;
  setPill("seq-pill", label, seq.current_name ? "good" : "");
  setPill("dash-seq-pill", label, seq.current_name ? "good" : "");

  const current = seq.current_name || "Between steps";
  $("seq-current").textContent = current;
  $("dash-seq-current").textContent = current;
  $("dash-seq-sub").textContent = `${seq.done} of ${seq.total} instructions done`;

  $("seq-tree").innerHTML = (seq.steps || [])
    .map((step) => {
      const status = (step.status || "").toLowerCase();
      const classes = [
        "step",
        step.is_container ? "container" : "",
        status.includes("running") ? "running" : "",
        status.includes("finished") ? "finished" : "",
        status.includes("failed") ? "failed" : "",
      ]
        .filter(Boolean)
        .join(" ");
      return `<div class="${classes}" style="padding-left:${8 + step.depth * 15}px">
        <span class="status">${esc(step.status || "")}</span>
        <span class="name">${esc(step.name)}</span>
      </div>`;
    })
    .join("");
}

/* ── equipment ─────────────────────────────────────────────────────── */

const DEVICE_ICONS = {
  camera: "camera", mount: "mount", focuser: "focuser", filterwheel: "filter",
  guider: "guider", rotator: "rotator", dome: "dome", switch: "switch",
  flat: "flat", weather: "weather", safety: "safety",
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

  if (!devices.length) {
    $("equipment-list").innerHTML = `<div class="list-empty">${esc(
      nina.error || "Waiting for NINA…"
    )}</div>`;
    return;
  }

  $("equipment-list").innerHTML = devices
    .map((device) => {
      const glyph = DEVICE_ICONS[device.icon] || DEVICE_ICONS[device.key] || "switch";
      const detail = device.connected ? device.detail : device.error || device.detail || "";
      // The word next to the dot is what carries the state; the colour repeats
      // it. In night-vision mode the colour is gone and the word is not.
      return `<div class="device ${device.connected ? "is-up" : "is-down"}">
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

/* ── frames ────────────────────────────────────────────────────────── */

let hfrChart = null;

function renderFrames() {
  const frames = state.frames || [];
  setPill("frames-count", frames.length ? `${frames.length} frames` : "none");

  if (!frames.length) {
    $("frame-stats").innerHTML = "";
    $("frame-flags").innerHTML = "";
    $("frame-table").innerHTML = "";
    setPill("frame-verdict", "–");
    if (hfrChart) hfrChart.empty("No frames yet.");
    return;
  }

  const frame = frames[0];
  const flags = frame.flags || {};

  const chips = [];
  if (flags.saturated) chips.push(flagChip("warn", "alert", "saturated"));
  if (flags.cloud_suspect) chips.push(flagChip("bad", "weather", "cloud suspected"));
  if (flags.tracking_suspect) chips.push(flagChip("warn", "guiding", "tracking"));
  if (!chips.length) chips.push(flagChip("ok", "check", "clean"));
  $("frame-flags").innerHTML = chips.join("");
  setPill(
    "frame-verdict",
    chips.length === 1 && !flags.saturated && !flags.cloud_suspect && !flags.tracking_suspect
      ? "clean"
      : "flagged",
    flags.cloud_suspect ? "bad" : flags.saturated || flags.tracking_suspect ? "warn" : "good"
  );

  $("frame-stats").innerHTML = [
    ["Target", frame.target || DASH],
    ["Filter", frame.filter || DASH],
    ["Exposure", frame.exposure_s ? `${fmt(frame.exposure_s, 0)} s` : DASH],
    ["HFR", `${fmt(frame.hfr)} ± ${fmt(frame.hfr_stdev)}`],
    ["Stars", frame.stars ?? DASH],
    ["Median / max ADU", `${fmt(frame.median, 0)} / ${fmt(frame.max, 0)}`],
    ["Guide RMS", frame.rms_text || DASH],
    ["Sensor", frame.temperature !== null ? `${fmt(frame.temperature, 1)} °C` : DASH],
    ["Gain", frame.gain ?? DASH],
    ["Captured", frame.date ? `${clock(frame.date)} · ${ago(frame.date)}` : DASH],
  ]
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`)
    .join("");

  $("frame-table").innerHTML =
    `<thead><tr><th>Time</th><th>Filter</th><th>HFR</th><th>Stars</th><th></th></tr></thead><tbody>` +
    frames
      .slice(0, 25)
      .map((f) => {
        const fl = f.flags || {};
        const mark = fl.cloud_suspect
          ? `<span class="flag bad">${icon("weather")}</span>`
          : fl.saturated
          ? `<span class="flag warn">${icon("alert")}</span>`
          : fl.tracking_suspect
          ? `<span class="flag warn">${icon("guiding")}</span>`
          : "";
        return `<tr>
          <td>${esc(clock(f.date))}</td>
          <td>${esc(f.filter || "?")}</td>
          <td>${esc(fmt(f.hfr))}</td>
          <td>${esc(f.stars ?? DASH)}</td>
          <td>${mark}</td>
        </tr>`;
      })
      .join("") +
    "</tbody>";

  drawHfr(frames);
}

const flagChip = (kind, glyph, text) =>
  `<span class="flag ${kind}">${icon(glyph)}${esc(text)}</span>`;

function drawHfr(frames) {
  const host = $("hfr-chart");
  if (!host) return;
  if (!hfrChart) {
    hfrChart = new LineChart(host, {
      series: [{ label: "HFR", points: true }],
      xLabel: (_x, index) => {
        const list = (state.frames || []).slice(0, 40).reverse();
        const frame = list[index];
        return frame ? `${clock(frame.date)} · ${frame.filter || "?"}` : "";
      },
      yLabel: (value) => value.toFixed(2),
      xTicks: () => [],
    });
  }
  // Newest last, so the eye reads left-to-right as time.
  const ordered = frames.slice(0, 40).reverse().filter((f) => Number.isFinite(f.hfr));
  if (ordered.length < 2) {
    hfrChart.empty("Not enough frames yet.");
    return;
  }
  hfrChart.update([ordered.map((_f, i) => i), ordered.map((f) => f.hfr)]);
}

/* ── weather ───────────────────────────────────────────────────────── */

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
        { label: "Cloud", fill: 0.18, width: 1.8 },
        { label: "Low cloud", width: 1.8 },
        { label: "Rain", width: 1.6, dash: [4, 3] },
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

/* ── advisor ───────────────────────────────────────────────────────── */

function renderAdvisor() {
  const advisor = state.advisor || {};
  const label = `${advisor.mode || "off"}${advisor.enabled ? " · armed" : ""}`;
  const kind = advisor.enabled && advisor.mode === "auto" ? "good" : advisor.mode === "off" ? "" : "warn";
  setPill("advisor-mode", label, kind);
  setPill("dash-advisor-pill", label, kind);

  const enabled = $("tuning-enabled");
  if (enabled && document.activeElement !== enabled) enabled.checked = !!advisor.enabled;
  const mode = $("tuning-mode");
  if (mode && document.activeElement !== mode) mode.value = advisor.mode || "off";

  const last = advisor.last;
  const advice = last ? `${last.action}: ${last.detail}` : "Waiting for the first tick…";
  $("advisor-last").textContent = advice;
  $("dash-advisor-last").textContent = advice;

  const changes = (advisor.changes || [])
    .map(
      (c) => `<div class="row ${c.reverted ? "is-muted" : ""}">
        <span class="tag ${esc(c.source)}">${esc(c.source)}</span>
        <span class="mono">${esc(c.axis)}.${esc(c.param)} ${esc(c.before)}→${esc(c.applied)}</span>
        <span class="grow">${esc(c.rationale || "")}</span>
      </div>`
    )
    .join("");
  listOr($("advisor-changes"), changes, "No changes yet tonight.");
  listOr($("dash-advisor-changes"), changes, "No changes yet tonight.");

  listOr(
    $("advisor-vetoes"),
    (advisor.vetoes || [])
      .map(
        (v) => `<div class="row">
          <span class="mono" style="color:var(--warn)">${esc(v.rule)}</span>
          <span class="grow">${esc(v.detail)}</span>
        </div>`
      )
      .join(""),
    "Nothing refused."
  );
}

/* ── polar alignment ───────────────────────────────────────────────── */

function renderTppa() {
  const pa = state.tppa || {};
  // The plugin reports degrees; arcminutes are what you adjust by at the mount.
  const minutes = (deg) => (deg === undefined || deg === null ? DASH : fmt(deg * 60, 1));
  $("pa-az").textContent = minutes(pa.AzimuthError);
  $("pa-alt").textContent = minutes(pa.AltitudeError);
  $("pa-total").textContent = minutes(pa.TotalError);
  $("pa-bar").style.width = `${Math.round((pa.Progress || 0) * 100)}%`;
  $("pa-status").textContent = pa.Status || (pa.running ? "Running…" : "Idle");
  $("pa-start").disabled = !!pa.running;
  $("pa-stop").disabled = !pa.running;
  setPill("pa-pill", pa.running ? "running" : "idle", pa.running ? "good" : "");
}

/* ── health ────────────────────────────────────────────────────────── */

function renderHealth() {
  const health = state.health || [];
  const down = health.filter((t) => !t.healthy).length;
  setPill(
    "health-pill",
    health.length ? (down ? `${down} down` : "all up") : "–",
    !health.length ? "" : down ? "bad" : "good"
  );

  listOr(
    $("health-list"),
    health
      .map(
        (task) => `<div class="row">
          <span class="dot ${task.healthy ? "live" : ""}"></span>
          <span class="mono">${esc(task.name)}</span>
          <span class="grow">${esc(task.last_error || (task.healthy ? "running" : "stopped"))}</span>
          <span class="tag">${task.healthy ? "up" : "down"}${task.restarts ? ` ·${task.restarts}r` : ""}</span>
        </div>`
      )
      .join(""),
    "No tasks reported."
  );

  const store = state.session?.store || {};
  listOr(
    $("store-stats"),
    Object.entries(store)
      .map(
        ([k, v]) =>
          `<div class="row"><span class="grow" style="color:var(--text-2)">${esc(k)}</span><span class="mono">${esc(v)}</span></div>`
      )
      .join(""),
    "Empty."
  );
}

/* ── registration ──────────────────────────────────────────────────── */

export function registerPanels() {
  const everything = [
    "phd2", "guiding", "sequence", "nina", "frames", "weather",
    "sky", "advisor", "tppa", "health", "session", "preview", "config",
  ];
  on(everything, "topbar", renderTopbar);
  on(["guiding", "phd2", "frames", "sequence", "nina", "weather", "config"], "kpis", renderKpis);
  on(["phd2", "guiding", "config"], "guiding", renderGuiding);
  on(["sequence"], "sequence", renderSequence);
  on(["nina", "phd2"], "equipment", renderEquipment);
  on(["frames"], "frames", renderFrames);
  on(["weather", "sky"], "weather", renderWeather);
  on(["advisor"], "advisor", renderAdvisor);
  on(["tppa"], "tppa", renderTppa);
  on(["health", "session"], "health", renderHealth);
}

export function repaintCharts() {
  drawGuide(state.guiding?.graph);
  drawHfr(state.frames || []);
  drawCloud(state.weather?.hourly || []);
}
