/* The Guiding page.
 *
 * Live guiding under the microscope: the guide star crop, the guiding error
 * and RMS over the last ten minutes, the PHD2 parameters (editable), the
 * advisor that suggests -- or applies -- tuning changes, and the polar
 * alignment quality. Renderers are "state in, DOM out" against the topics they
 * read; the star crop is the exception, polled on a timer because it is a
 * synchronous round trip into PHD2 (see pollStar).
 */
"use strict";

import { $, TOKEN, act, duration, esc, fmt, icon, on, state, toast, url } from "../core.js";
import { LineChart } from "../charts.js";
import { DASH, decorateMarks, listOr, rmsStatus, setPill, tile } from "../widgets.js";

const authHeaders = TOKEN ? { "X-Auth-Token": TOKEN } : {};

/**
 * Status band for the RA oscillation ratio (peak-to-RMS).
 *
 * 1.4 = pure sine, 1.7 = Gaussian noise, 2.0+ = under-damped oscillation.
 * Above ~2.2 the mount is ringing.
 */
function oscStatus(ratio) {
  if (ratio > 2.2) return "ringing — mount is under-damped";
  if (ratio > 1.8) return "mildly resonant";
  return "stable / damped";
}

/* ── summary tiles ───────────────────────────────────────────────── */

function renderTiles() {
  const phd2 = state.phd2 || {};
  const rms = state.guiding?.rms;
  const host = $("guide-tiles");
  if (!host) return;
  host.innerHTML = [
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
    tile({ label: "RA corrections", value: rms ? fmt(rms.ra_corr_ms, 0) : DASH,
           unit: "ms",
           sub: rms ? `RA guide pulse avg` : "not guiding",
           status: !rms ? "idle" : rms.ra_corr_ms > 800 ? "warn" : rms.ra_corr_ms > 400 ? "warn" : "good" }),
    tile({ label: "Dec corrections", value: rms ? fmt(rms.dec_corr_ms, 0) : DASH,
           unit: "ms",
           sub: rms ? `Dec guide pulse avg` : "not guiding",
           status: !rms ? "idle" : rms.dec_corr_ms > 800 ? "warn" : "good" }),
    tile({ label: "RA oscillation", value: rms ? fmt(rms.ra_oscillation) : DASH,
           sub: rms ? oscStatus(rms.ra_oscillation) : "not guiding",
           status: !rms ? "idle" : rms.ra_oscillation > 2.2 ? "warn" : "good" }),
    tile({ label: "Guide star SNR", value: rms ? fmt(rms.snr_med, 1) : DASH,
           sub: rms ? `HFD ${fmt(rms.hfd_med)} px` : "",
           status: !rms ? "idle" : rms.snr_med < 10 ? "bad" : rms.snr_med < 20 ? "warn" : "good" }),
    tile({ label: "Pixel scale", value: phd2.pixel_scale ? fmt(phd2.pixel_scale) : DASH,
           unit: "″/px", sub: phd2.exposure_ms ? `${phd2.exposure_ms} ms exposure` : "" }),
    tile({ label: "Guiding for", value: phd2.guiding_for_s ? duration(phd2.guiding_for_s) : DASH,
           sub: phd2.paused ? "paused" : phd2.settling ? "settling" : phd2.calibrating ? "calibrating" : "",
           status: phd2.paused ? "warn" : phd2.guiding_for_s ? "good" : "idle" }),
  ].join("");
}

/* ── guide trace ─────────────────────────────────────────────────── */

let guideChart = null;

function renderTrace() {
  const phd2 = state.phd2 || {};
  const guiding = state.guiding || {};

  const label = phd2.connected ? phd2.app_state || "?" : "disconnected";
  const kind = !phd2.connected ? "bad" : phd2.app_state === "Guiding" ? "good" : "warn";
  setPill("guide-state", label, kind);

  const disturbed = guiding.disturbed || [];
  const banner = $("guide-disturbed-2");
  if (banner) {
    banner.hidden = disturbed.length === 0;
    if (disturbed.length) {
      banner.innerHTML = `${icon("alert")}<span>Measurement paused: ${esc(disturbed.join(", "))}</span>`;
    }
  }

  const pause = $("pause-button");
  if (pause) pause.textContent = phd2.paused ? "Resume" : "Pause";

  drawTrace(guiding.graph);
}

function drawTrace(graph) {
  const host = $("guide-chart");
  if (!host) return;
  if (!guideChart) {
    guideChart = new LineChart(host, {
      legendEl: $("guide-legend"),
      series: [
        { label: "RA", width: 1.6 },
        { label: "Dec", width: 1.6 },
      ],
      xLabel: (seconds) => `${Math.abs(Math.round(seconds))}s ago`,
      yLabel: (value) => `${value.toFixed(2)}″`,
      yTicks: (_p, ticks) => ticks.map((t) => t.toFixed(1)),
      xTicks: (_p, ticks) => ticks.map((t) => `${Math.round(t)}s`),
    });
  }
  if (!graph || !graph.t || graph.t.length < 2) {
    guideChart.empty("Waiting for guide steps…");
    return;
  }
  guideChart.update([graph.t, graph.ra, graph.dec]);
}

/* ── parameters (editable) ───────────────────────────────────────── */

function renderParams() {
  const phd2 = state.phd2 || {};
  const params = phd2.params || {};
  const host = $("guide-params");
  if (!host) return;
  const rows = Object.entries(params)
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
  host.innerHTML = rows || `<tr><td class="list-empty">No parameters read yet.</td></tr>`;
  setPill("params-pill", phd2.dec_guide_mode ? `dec ${phd2.dec_guide_mode}` : "–");
}

/* ── advisor ─────────────────────────────────────────────────────── */

function renderAdvisor() {
  const advisor = state.advisor || {};
  const label = `${advisor.mode || "off"}${advisor.enabled ? " · armed" : ""}`;
  const kind = advisor.enabled && advisor.mode === "auto" ? "good" : advisor.mode === "off" ? "" : "warn";
  setPill("advisor-mode", label, kind);

  const enabled = $("tuning-enabled");
  if (enabled && document.activeElement !== enabled) enabled.checked = !!advisor.enabled;
  const mode = $("tuning-mode");
  if (mode && document.activeElement !== mode) mode.value = advisor.mode || "off";

  const last = advisor.last;
  if ($("advisor-last")) $("advisor-last").textContent = last ? `${last.action}: ${last.detail}` : "Waiting for the first tick…";

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

/* ── polar alignment (TPPA) ──────────────────────────────────────── */

function renderTppa() {
  const pa = state.tppa || {};
  // The plugin reports degrees; arcminutes are what you adjust by at the mount.
  const minutes = (deg) => (deg === undefined || deg === null ? DASH : fmt(deg * 60, 1));
  if ($("pa-az")) $("pa-az").textContent = minutes(pa.AzimuthError);
  if ($("pa-alt")) $("pa-alt").textContent = minutes(pa.AltitudeError);
  if ($("pa-total")) $("pa-total").textContent = minutes(pa.TotalError);
  if ($("pa-bar")) $("pa-bar").style.width = `${Math.round((pa.Progress || 0) * 100)}%`;
  if ($("pa-status")) $("pa-status").textContent = pa.Status || (pa.running ? "Running…" : "Idle");
  if ($("pa-start")) $("pa-start").disabled = !!pa.running;
  if ($("pa-stop")) $("pa-stop").disabled = !pa.running;
  setPill("pa-pill", pa.running ? "running" : "idle", pa.running ? "good" : "");
}

/* ── the guide star crop ─────────────────────────────────────────── */

let starTimer = null;
let starUrl = null;

async function pollStar() {
  const phd2 = state.phd2 || {};
  const img = $("star-image");
  const empty = $("star-empty");
  if (!img) return;

  if (!phd2.connected) {
    setStarEmpty("PHD2 is not connected");
    return;
  }
  try {
    const response = await fetch(url("/api/guide-star.png", { size: 31 }), { headers: authHeaders });
    if (!response.ok) {
      setStarEmpty(response.status === 404 ? "No star selected" : `Error ${response.status}`);
      return;
    }
    const blob = await response.blob();
    const next = URL.createObjectURL(blob);
    if (starUrl) URL.revokeObjectURL(starUrl);
    starUrl = next;
    img.src = next;
    empty.hidden = true;

    const peak = Number(response.headers.get("X-Star-Peak"));
    const pill = $("star-pill");
    if (pill) {
      pill.textContent = Number.isFinite(peak) ? `peak ${Math.round(peak)}` : "live";
      pill.className = "pill good";
    }
  } catch (_) {
    setStarEmpty("No star");
  }
}

function setStarEmpty(message) {
  const img = $("star-image");
  const empty = $("star-empty");
  if (!img || !empty) return;
  img.removeAttribute("src");
  empty.hidden = false;
  empty.textContent = message;
  const pill = $("star-pill");
  if (pill) {
    pill.textContent = "idle";
    pill.className = "pill";
  }
}

/**
 * Poll only while the Guiding page is on screen.
 *
 * `get_star_image` is a synchronous round trip to PHD2 on the same socket the
 * guide steps arrive on; polling it from a background tab all night would be
 * rude to the thing whose job is guiding.
 */
export function syncStarPolling() {
  const wanted = $("page-guiding")?.classList.contains("is-active") && document.visibilityState === "visible";
  if (wanted && !starTimer) {
    pollStar();
    starTimer = setInterval(pollStar, 2500);
  } else if (!wanted && starTimer) {
    clearInterval(starTimer);
    starTimer = null;
  }
}

/* ── wiring & controls ───────────────────────────────────────────── */

export function registerGuiding() {
  on(["phd2", "guiding", "config"], "guide-tiles", renderTiles);
  on(["phd2", "guiding"], "guide-trace", renderTrace);
  on(["phd2"], "guide-params", renderParams);
  on(["advisor"], "advisor", renderAdvisor);
  on(["tppa"], "tppa", renderTppa);

  decorateMarks("#page-guiding .starbox");

  // A parameter edit goes straight to PHD2, so it commits on blur or Enter
  // rather than on every keystroke.
  $("guide-params")?.addEventListener("change", (event) => {
    const input = event.target;
    if (input.tagName !== "INPUT") return;
    act(null, "/api/guiding/param", {
      body: JSON.stringify({ axis: input.dataset.axis, param: input.dataset.param, value: parseFloat(input.value) }),
      okMessage: `${input.dataset.axis}.${input.dataset.param} set`,
    });
  });

  $("tuning-enabled")?.addEventListener("change", (event) => {
    act(null, "/api/advisor/tuning", { body: JSON.stringify({ enabled: event.target.checked }) });
  });
  $("tuning-mode")?.addEventListener("change", (event) => {
    act(null, "/api/advisor/tuning", { body: JSON.stringify({ mode: event.target.value }) });
  });
  $("revert-last")?.addEventListener("click", (e) => act(e.currentTarget, "/api/advisor/revert-last"));
  $("revert-all")?.addEventListener("click", (e) => act(e.currentTarget, "/api/advisor/revert-all"));

  $("pa-start")?.addEventListener("click", (e) => act(e.currentTarget, "/api/tppa/start", { body: JSON.stringify({}) }));
  $("pa-stop")?.addEventListener("click", (e) => act(e.currentTarget, "/api/tppa/stop"));

  // The guide buttons carry data-guide and are handled by the delegated click
  // listener in main.js; only the star crop needs a poller toggle here.
  document.addEventListener("visibilitychange", syncStarPolling);
}

/** Re-fit the trace after the page is shown (a hidden chart has no width). */
export function refresh() {
  drawTrace(state.guiding?.graph);
}
