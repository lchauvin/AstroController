/* AstroController UI.
 *
 * One EventSource feeds every panel. The server opens each connection with a
 * full snapshot and re-sends one whenever a client falls behind, so this code
 * never has to reconcile a partial history -- it just renders whatever it is
 * given.
 */
"use strict";

const $ = (id) => document.getElementById(id);

// A token is required when the server is not on loopback. EventSource cannot
// set headers, so it travels as a query parameter; fetch() uses the header.
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const authQuery = TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : "";
const authHeaders = TOKEN ? { "X-Auth-Token": TOKEN } : {};

let state = {};
let guideChart = null;
let cloudChart = null;

/* ── plumbing ──────────────────────────────────────────────────────── */

function toast(message, kind = "") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), kind === "err" ? 6000 : 3000);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders, ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function connect() {
  const src = new EventSource(`/api/stream${authQuery}`);
  src.onopen = () => $("conn-dot").classList.add("live");
  src.onerror = () => {
    $("conn-dot").classList.remove("live");
    // EventSource reconnects on its own; that is why SSE was chosen over a
    // websocket for a dashboard left open all night on a phone.
  };
  src.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === "snapshot") {
      state = message.data || {};
      renderAll();
    } else {
      state[message.type] = message.data;
      renderTopic(message.type);
    }
  };
}

// Each panel renders independently. One panel throwing must never blank the
// rest of the dashboard -- losing the weather forecast is an annoyance, losing
// the guiding readout because of it is not acceptable.
function safely(name, fn) {
  try {
    fn();
  } catch (err) {
    console.error(`panel "${name}" failed to render:`, err);
  }
}

const PANELS = {
  header: renderHeader,
  sequence: renderSequence,
  guiding: renderGuiding,
  advisor: renderAdvisor,
  frames: renderFrames,
  weather: renderWeather,
  tppa: renderTppa,
  health: renderHealth,
};

function renderAll() {
  for (const [name, fn] of Object.entries(PANELS)) safely(name, fn);
}

function renderTopic(topic) {
  safely("header", renderHeader);
  const name = { phd2: "guiding", sky: "weather" }[topic] || topic;
  if (PANELS[name]) safely(name, PANELS[name]);
}

const fmt = (value, digits = 2, dash = "–") =>
  value === null || value === undefined || Number.isNaN(value)
    ? dash
    : Number(value).toFixed(digits);

/* ── header ────────────────────────────────────────────────────────── */

function renderHeader() {
  const rms = state.guiding?.rms;
  $("hdr-rms").textContent = rms ? `${fmt(rms.rms_total)}″` : "–";
  $("hdr-phd2").textContent = state.phd2?.connected ? (state.phd2.app_state || "?") : "off";
  const seq = state.sequence;
  $("hdr-seq").textContent = seq?.available ? `${seq.done}/${seq.total}` : "–";
}

/* ── sequence ──────────────────────────────────────────────────────── */

function renderSequence() {
  const seq = state.sequence || {};
  const pill = $("seq-progress");

  if (!seq.available) {
    $("seq-current").textContent = seq.error || "No sequence loaded";
    $("seq-tree").innerHTML = "";
    pill.textContent = "idle";
    pill.className = "pill";
    return;
  }

  pill.textContent = `${seq.done}/${seq.total}`;
  pill.className = "pill" + (seq.current_name ? " good" : "");
  $("seq-current").textContent = seq.current_name || "Between steps";

  $("seq-tree").innerHTML = (seq.steps || [])
    .map((step) => {
      const status = (step.status || "").toLowerCase();
      const classes = [
        "step",
        step.is_container ? "container" : "",
        status.includes("running") ? "running" : "",
        status.includes("finished") ? "finished" : "",
        status.includes("failed") ? "failed" : "",
      ].join(" ");
      const indent = 8 + step.depth * 14;
      return `<div class="${classes}" style="padding-left:${indent}px">
        <span class="status">${esc(step.status || "")}</span>
        <span class="name">${esc(step.name)}</span>
      </div>`;
    })
    .join("");
}

/* ── guiding ───────────────────────────────────────────────────────── */

function renderGuiding() {
  const phd2 = state.phd2 || {};
  const guiding = state.guiding || {};
  const rms = guiding.rms;

  const pill = $("guide-state");
  pill.textContent = phd2.connected ? (phd2.app_state || "?") : "disconnected";
  pill.className =
    "pill " + (!phd2.connected ? "bad" : phd2.app_state === "Guiding" ? "good" : "warn");

  $("rms-total").textContent = rms ? fmt(rms.rms_total) : "–";
  $("rms-ra").textContent = rms ? fmt(rms.rms_ra) : "–";
  $("rms-dec").textContent = rms ? fmt(rms.rms_dec) : "–";
  $("guide-snr").textContent = rms ? fmt(rms.snr_med, 1) : "–";

  const disturbed = guiding.disturbed || [];
  const banner = $("guide-disturbed");
  banner.hidden = disturbed.length === 0;
  if (disturbed.length) {
    banner.textContent = `Measurement paused: ${disturbed.join(", ")}`;
  }

  drawGuideChart(guiding.graph);
  renderParams(phd2);
}

function chartsAvailable(el) {
  if (typeof uPlot !== "undefined") return true;
  if (el && !el.dataset.noChart) {
    el.dataset.noChart = "1";
    el.innerHTML = `<div class="muted small">Charts unavailable (uPlot did not load).</div>`;
  }
  return false;
}

function drawGuideChart(graph) {
  if (!graph || !graph.t || graph.t.length < 2) return;
  const el = $("guide-chart");
  if (!chartsAvailable(el)) return;
  const data = [graph.t, graph.ra, graph.dec];

  if (!guideChart) {
    const css = getComputedStyle(document.documentElement);
    guideChart = new uPlot(
      {
        width: el.clientWidth || 600,
        height: 190,
        cursor: { show: false },
        legend: { show: false },
        scales: { x: { time: false } },
        axes: [
          { stroke: css.getPropertyValue("--muted"), grid: { stroke: css.getPropertyValue("--line") } },
          {
            stroke: css.getPropertyValue("--muted"),
            grid: { stroke: css.getPropertyValue("--line") },
            label: "arcsec",
          },
        ],
        series: [
          { label: "s ago" },
          { label: "RA", stroke: css.getPropertyValue("--accent"), width: 1.4 },
          { label: "Dec", stroke: css.getPropertyValue("--warn"), width: 1.4 },
        ],
      },
      data,
      el
    );
    addEventListener("resize", () => guideChart?.setSize({ width: el.clientWidth, height: 190 }));
  } else {
    guideChart.setData(data);
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
        <td>${esc(axis)} ${esc(param)}</td>
        <td><input type="number" step="0.01" value="${value}"
              data-axis="${esc(axis)}" data-param="${esc(param)}"></td>
      </tr>`;
    })
    .join("");
  $("guide-params").innerHTML = rows || `<tr><td class="muted">No parameters read yet.</td></tr>`;
}

/* ── advisor ───────────────────────────────────────────────────────── */

function renderAdvisor() {
  const advisor = state.advisor || {};
  const pill = $("advisor-mode");
  pill.textContent = `${advisor.mode || "off"}${advisor.enabled ? " · armed" : ""}`;
  pill.className = "pill " + (advisor.enabled && advisor.mode === "auto" ? "good" : "");

  const enabled = $("tuning-enabled");
  if (document.activeElement !== enabled) enabled.checked = !!advisor.enabled;
  const mode = $("tuning-mode");
  if (document.activeElement !== mode) mode.value = advisor.mode || "off";

  const last = advisor.last;
  $("advisor-last").textContent = last
    ? `${last.action}: ${last.detail}`
    : "Waiting for the first tick…";

  const changes = advisor.changes || [];
  $("advisor-changes").innerHTML = changes.length
    ? changes
        .map(
          (c) => `<div class="change ${c.reverted ? "reverted" : ""}">
            <span class="tag ${esc(c.source)}">${esc(c.source)}</span>
            <span class="what">${esc(c.axis)}.${esc(c.param)} ${c.before}→${c.applied}</span>
            <span class="why">${esc(c.rationale || "")}</span>
          </div>`
        )
        .join("")
    : `<div class="muted">None yet.</div>`;

  const vetoes = advisor.vetoes || [];
  $("advisor-vetoes").innerHTML = vetoes.length
    ? vetoes
        .map(
          (v) => `<div class="veto">
            <span class="rule">${esc(v.rule)}</span>
            <span class="why">${esc(v.detail)}</span>
          </div>`
        )
        .join("")
    : `<div class="muted">None.</div>`;
}

/* ── frames ────────────────────────────────────────────────────────── */

function renderFrames() {
  const frames = state.frames || [];
  if (!frames.length) {
    $("frame-head").textContent = "No frames yet";
    return;
  }
  const f = frames[0];
  const flags = f.flags || {};

  $("frame-head").textContent =
    `${f.target || "—"} · ${f.filter || "—"} · ${fmt(f.exposure_s, 0)}s`;

  const chips = [];
  if (flags.saturated) chips.push(`<span class="flag sat">saturated</span>`);
  if (flags.cloud_suspect) chips.push(`<span class="flag cloud">cloud suspected</span>`);
  if (flags.tracking_suspect) chips.push(`<span class="flag track">tracking</span>`);
  if (!chips.length) chips.push(`<span class="flag ok">clean</span>`);
  $("frame-flags").innerHTML = chips.join("");

  $("frame-stats").innerHTML = [
    ["HFR", `${fmt(f.hfr)} ± ${fmt(f.hfr_stdev)}`],
    ["Stars", f.stars ?? "–"],
    ["Median / Max", `${fmt(f.median, 0)} / ${fmt(f.max, 0)}`],
    ["Guide RMS", f.rms_text || "–"],
    ["Sensor", `${fmt(f.temperature, 1)} °C`],
  ]
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`)
    .join("");

  $("frame-list").innerHTML = frames
    .slice(0, 12)
    .map((frame) => {
      const fl = frame.flags || {};
      const mark = fl.cloud_suspect ? "☁" : fl.saturated ? "▲" : fl.tracking_suspect ? "~" : "·";
      return `<div class="frame-row">
        <span class="what">${esc(mark)} ${esc(frame.filter || "?")}</span>
        <span class="what">HFR ${fmt(frame.hfr)}</span>
        <span class="what">${frame.stars ?? "–"}★</span>
        <span class="when">${esc((frame.date || "").replace("T", " ").slice(5, 16))}</span>
      </div>`;
    })
    .join("");
}

/* ── weather ───────────────────────────────────────────────────────── */

function renderWeather() {
  const weather = state.weather || {};
  const now = weather.current;
  const summary = weather.summary || {};

  if (weather.error) {
    $("weather-now").textContent = `Forecast unavailable: ${weather.error}`;
  } else if (now) {
    $("weather-now").textContent =
      `${fmt(now.cloud_total, 0)}% cloud (${fmt(now.cloud_low, 0)}% low) · ` +
      `${fmt(now.temp_c, 1)}°C, dew point ${fmt(now.dewpoint_c, 1)}°C · ` +
      `wind ${fmt(now.wind_ms, 1)} m/s`;
  }

  const risks = [];
  if (summary.rain_risk) risks.push(`<span class="flag cloud">rain risk</span>`);
  if (summary.dew_risk) risks.push(`<span class="flag sat">dew risk</span>`);
  if (summary.clear_hours !== undefined) {
    risks.push(`<span class="flag ok">${summary.clear_hours}h clear of ${summary.hours}</span>`);
  }
  $("weather-risks").innerHTML = risks.join("");

  drawCloudChart(weather.hourly || []);

  const sky = state.sky || {};
  $("moon-info").textContent = sky.phase_name
    ? `Moon: ${sky.phase_name}, ${Math.round((sky.illumination || 0) * 100)}% lit` +
      (sky.altitude_deg !== null && sky.altitude_deg !== undefined
        ? `, ${fmt(sky.altitude_deg, 0)}° altitude (${sky.up ? "up" : "below horizon"})`
        : "") +
      (sky.precise ? "" : " (approximate)")
    : "";
}

function drawCloudChart(hourly) {
  if (!hourly.length) return;
  const el = $("cloud-chart");
  if (!chartsAvailable(el)) return;
  const xs = hourly.map((_, i) => i);
  const data = [
    xs,
    hourly.map((h) => h.cloud_total),
    hourly.map((h) => h.cloud_low),
    hourly.map((h) => h.precip_prob),
  ];

  if (!cloudChart) {
    const css = getComputedStyle(document.documentElement);
    cloudChart = new uPlot(
      {
        width: el.clientWidth || 400,
        height: 120,
        cursor: { show: false },
        legend: { show: false },
        scales: { x: { time: false }, y: { range: [0, 100] } },
        axes: [
          { stroke: css.getPropertyValue("--muted"), grid: { stroke: css.getPropertyValue("--line") } },
          { stroke: css.getPropertyValue("--muted"), grid: { stroke: css.getPropertyValue("--line") } },
        ],
        series: [
          { label: "h" },
          { label: "cloud", stroke: css.getPropertyValue("--muted"), fill: "rgba(139,147,167,.2)" },
          { label: "low", stroke: css.getPropertyValue("--bad"), width: 1.4 },
          { label: "rain", stroke: css.getPropertyValue("--accent"), width: 1.2, dash: [4, 3] },
        ],
      },
      data,
      el
    );
    addEventListener("resize", () => cloudChart?.setSize({ width: el.clientWidth, height: 120 }));
  } else {
    cloudChart.setData(data);
  }
}

/* ── polar alignment ───────────────────────────────────────────────── */

function renderTppa() {
  const pa = state.tppa || {};
  // The plugin reports errors in degrees; arcminutes are what you actually
  // adjust by at the mount.
  const min = (deg) => (deg === undefined || deg === null ? "–" : fmt(deg * 60, 1));
  $("pa-az").textContent = min(pa.AzimuthError);
  $("pa-alt").textContent = min(pa.AltitudeError);
  $("pa-total").textContent = min(pa.TotalError);
  $("pa-bar").style.width = `${Math.round((pa.Progress || 0) * 100)}%`;
  $("pa-status").textContent = pa.Status || (pa.running ? "Running…" : "Idle");
  $("pa-start").disabled = !!pa.running;
  $("pa-stop").disabled = !pa.running;
}

/* ── health ────────────────────────────────────────────────────────── */

function renderHealth() {
  const health = state.health || [];
  $("health-list").innerHTML = health
    .map(
      (task) => `<div class="health-row">
        <span class="name">${esc(task.name)}</span>
        <span class="state ${task.healthy ? "up" : "down"}">
          ${task.healthy ? "up" : "down"}${task.restarts ? ` ·${task.restarts}r` : ""}
        </span>
        <span class="why muted">${esc(task.last_error || "")}</span>
      </div>`
    )
    .join("");

  const store = state.session?.store || {};
  $("store-stats").innerHTML = Object.entries(store)
    .map(
      ([k, v]) =>
        `<div class="health-row"><span class="name">${esc(k)}</span><span class="state">${v}</span></div>`
    )
    .join("");
}

/* ── actions ───────────────────────────────────────────────────────── */

async function act(button, path, options) {
  const confirmText = button?.dataset.confirm;
  if (confirmText && !confirm(confirmText)) return;
  button?.classList.add("busy");
  try {
    await api(path, { method: "POST", ...options });
    toast("Done", "ok");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button?.classList.remove("busy");
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;

  if (button.dataset.seq) {
    act(button, `/api/sequence/${button.dataset.seq}`);
  } else if (button.dataset.guide) {
    const action = button.dataset.guide;
    if (action === "dither") {
      act(button, "/api/guiding/dither", { body: JSON.stringify({ pixels: 3.0 }) });
    } else if (action === "pause") {
      const paused = state.phd2?.paused ? "false" : "true";
      act(button, `/api/guiding/pause?paused=${paused}`);
    } else {
      act(button, `/api/guiding/${action}`);
    }
  } else if (button.id === "revert-last") {
    act(button, "/api/advisor/revert-last");
  } else if (button.id === "revert-all") {
    act(button, "/api/advisor/revert-all");
  } else if (button.id === "pa-start") {
    act(button, "/api/tppa/start", { body: JSON.stringify({}) });
  } else if (button.id === "pa-stop") {
    act(button, "/api/tppa/stop");
  } else if (button.id === "night-toggle") {
    const on = document.documentElement.getAttribute("data-night") === "1";
    document.documentElement.setAttribute("data-night", on ? "0" : "1");
    try { localStorage.setItem("night", on ? "0" : "1"); } catch (_) {}
    guideChart = cloudChart = null;
    document.querySelectorAll(".chart").forEach((c) => (c.innerHTML = ""));
    renderAll();
  }
});

$("tuning-enabled").addEventListener("change", (event) => {
  act(null, "/api/advisor/tuning", {
    body: JSON.stringify({ enabled: event.target.checked }),
  });
});

$("tuning-mode").addEventListener("change", (event) => {
  act(null, "/api/advisor/tuning", { body: JSON.stringify({ mode: event.target.value }) });
});

$("guide-params").addEventListener("change", (event) => {
  const input = event.target;
  if (input.tagName !== "INPUT") return;
  act(null, "/api/guiding/param", {
    body: JSON.stringify({
      axis: input.dataset.axis,
      param: input.dataset.param,
      value: parseFloat(input.value),
    }),
  });
});

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

try {
  if (localStorage.getItem("night") === "1") {
    document.documentElement.setAttribute("data-night", "1");
  }
} catch (_) {}

connect();
