/* The Guiding page.
 *
 * Live guiding under the microscope: the guiding error and correction traces
 * over the last ten minutes, the guide star crop, the PHD2 parameters
 * (editable), and the advisor that suggests -- or applies -- tuning changes.
 * Renderers are "state in, DOM out" against the topics they read; the star
 * crop is the exception, polled on a timer because it is a synchronous round
 * trip into PHD2 (see pollStar).
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

// Fixed, zero-centred bounds for both trace charts. A dither is several
// arcseconds -- an order of magnitude past normal guiding error -- and an
// auto-scaled axis stretches to fit it, squashing the real error down to a
// flat line for the ten minutes it takes to scroll off. Clipping the dither
// instead keeps the axis meaningful for the error the panel exists to show.
const ERROR_RANGE_ARCSEC = 3.0;
const CORR_RANGE_MS = 1200;

// Both charts share this key so hovering either one moves both crosshairs:
// error and correction are the same guide steps, split into two charts
// because a shared axis made the busier one (corrections) drown the other.
const TRACE_SYNC_KEY = "guide-trace";

let errorChart = null;
let corrChart = null;

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

  drawTrace(guiding.graph);
}

function drawTrace(graph) {
  drawErrorChart(graph);
  drawCorrChart(graph);
}

function drawErrorChart(graph) {
  const host = $("guide-chart");
  if (!host) return;
  if (!errorChart) {
    errorChart = new LineChart(host, {
      legendEl: $("guide-legend"),
      series: [
        { label: "RA", width: 1.6 },
        { label: "Dec", width: 1.6 },
      ],
      // Fixed and symmetric about zero -- see ERROR_RANGE_ARCSEC above -- so
      // the trace never rescales itself out from under the operator.
      yRange: [-ERROR_RANGE_ARCSEC, ERROR_RANGE_ARCSEC],
      syncKey: TRACE_SYNC_KEY,
      xLabel: (seconds) => `${Math.abs(Math.round(seconds))}s ago`,
      yLabel: (value) => `${value.toFixed(2)}″`,
      yTicks: (_p, ticks) => ticks.map((t) => t.toFixed(1)),
      xTicks: (_p, ticks) => ticks.map((t) => `${Math.round(t)}s`),
    });
  }
  if (!graph || !graph.t || graph.t.length < 2) {
    errorChart.empty("Waiting for guide steps…");
    return;
  }
  errorChart.update([graph.t, graph.ra, graph.dec]);
}

function drawCorrChart(graph) {
  const host = $("guide-corr-chart");
  if (!host) return;
  if (!corrChart) {
    corrChart = new LineChart(host, {
      legendEl: $("guide-corr-legend"),
      // Signed bars, PHD2-style: each names its ±1 direction via `sign`. The
      // "dir" series are pure sign carriers, not drawn or listed themselves.
      series: [
        { label: "RA dir", hidden: true },
        { label: "RA corr", colour: "--series-1", scale: "y", isBar: true, sign: "RA dir" },
        { label: "Dec dir", hidden: true },
        { label: "Dec corr", colour: "--series-2", scale: "y", isBar: true, sign: "Dec dir" },
      ],
      // Fixed and symmetric about zero -- see CORR_RANGE_MS above -- so a
      // West pulse hangs as far below as an East pulse rises above.
      yRange: [-CORR_RANGE_MS, CORR_RANGE_MS],
      syncKey: TRACE_SYNC_KEY,
      xLabel: (seconds) => `${Math.abs(Math.round(seconds))}s ago`,
      yLabel: (value) => `${value.toFixed(0)} ms`,
      yTicks: (_p, ticks) => ticks.map((t) => `${Math.round(t)}ms`),
      xTicks: (_p, ticks) => ticks.map((t) => `${Math.round(t)}s`),
    });
  }
  if (!graph || !graph.t || graph.t.length < 2) {
    corrChart.empty("Waiting for guide steps…");
    return;
  }
  corrChart.update([graph.t, graph.ra_dir || [], graph.ra_corr_ms || [], graph.dec_dir || [], graph.dec_corr_ms || []]);
}

/* ── parameters (editable) ───────────────────────────────────────── */

let paramsEditingUntil = 0;

function renderParams() {
  const phd2 = state.phd2 || {};
  const params = phd2.params || {};
  const host = $("guide-params");
  if (!host) return;
  // Rebuilding the table mid-edit would tear the field the user is typing
  // into (and the keystrokes in it) out from under them, so pause renders for
  // a short window after the last keystroke. A self-expiring timer rather
  // than an "is anything in here still focused" check: a click on ordinary
  // page text doesn't blur a focused input, so that check could freeze the
  // table on stale values indefinitely instead of just during an edit.
  if (Date.now() < paramsEditingUntil) return;
  const rows = Object.entries(params)
    .map(([name, value]) => {
      const [axis, param] = name.split(".");
      // PHD2's own Advanced Settings dialog shows aggression as 0-100%; the
      // RPC value underneath is always the same 0.0-1.0 fraction regardless.
      // This is purely a display/edit-box convenience -- everything sent to
      // PHD2 (and everything the advisor reads and bounds-checks) stays the
      // raw fraction, converted back at submit time in registerGuiding().
      const isPercent = param === "aggression" || param === "aggressiveness";
      const displayValue = fmt(isPercent ? value * 100 : value, 2);
      return `<tr>
        <td>${esc(axis)} · ${esc(param)}</td>
        <td><input type="number" step="${isPercent ? "1" : "0.01"}" value="${esc(displayValue)}"
              data-axis="${esc(axis)}" data-param="${esc(param)}" data-percent="${isPercent ? "1" : "0"}"
              aria-label="${esc(axis)} ${esc(param)}"></td>
      </tr>`;
    })
    .join("");
  host.innerHTML = rows || `<tr><td class="list-empty">No parameters read yet.</td></tr>`;
  setPill("params-pill", phd2.dec_guide_mode ? `dec ${phd2.dec_guide_mode}` : "–");
}

/* ── advisor ─────────────────────────────────────────────────────── */

// Friendly labels for the raw codes the backend uses internally. Anything
// missing here falls back to `humanize()` rather than showing the bare code.
const ADVISOR_ACTION_LABEL = {
  idle: "Idle",
  hold: "Holding",
  baseline: "Baseline",
  llm: "Model",
  vetoed: "Refused",
  error: "Error",
};

const ADVISOR_SOURCE_LABEL = {
  baseline: "Baseline",
  llm: "Model",
  manual: "Manual",
};

const VETO_LABEL = {
  mode: "Tuning mode",
  kill_switch: "Arm switch off",
  phd2_disconnected: "PHD2 disconnected",
  phd2_stale: "PHD2 stale",
  not_guiding: "Not guiding",
  paused: "Guiding paused",
  calibrating: "Calibrating",
  settling: "Settling",
  settle_guard: "Settle guard",
  dither_guard: "Dither guard",
  meridian_flip: "Meridian flip",
  autofocus: "Autofocus",
  star_lost: "Star lost",
  disturbed: "Disturbed",
  unknown_param: "Unknown parameter",
  param_unavailable: "Parameter unavailable",
  frozen: "Parameter frozen",
  out_of_range: "Out of range",
  no_current_value: "No current value",
  direction_lock: "Direction locked",
  trial_open: "Measuring",
  cooldown: "Cooldown",
  global_cooldown: "Global cooldown",
  hourly_budget: "Hourly budget",
  session_budget: "Session budget",
  no_baseline: "No baseline",
  no_baseline_window: "No baseline window",
  not_stable_yet: "Not stable yet",
  insufficient_data: "Insufficient data",
  no_change: "No change needed",
};

/** Fallback for any code missing from the maps above: "not_guiding" -> "Not guiding". */
function humanize(code) {
  const text = String(code || "").replace(/_/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

/** Capitalize the first letter, leaving backend/LLM punctuation as given. */
function cap(text) {
  const trimmed = String(text || "").trim();
  return trimmed ? trimmed[0].toUpperCase() + trimmed.slice(1) : "";
}

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
  if ($("advisor-last")) {
    if (!last) {
      $("advisor-last").textContent = "Waiting for the first tick…";
    } else if (last.action === "llm" && last.proposal && !/^applied /i.test(last.detail || "")) {
      // A suggestion that was not applied: show the proposal itself, not just
      // "suggestion only". This is the whole point of Suggest mode.
      const p = last.proposal;
      $("advisor-last").innerHTML = `${esc(cap(last.detail))} — would set <b>${esc(p.axis)}.${esc(p.param)} = ${esc(fmt(p.value))}</b>`;
    } else {
      const actionLabel = ADVISOR_ACTION_LABEL[last.action] || humanize(last.action);
      $("advisor-last").innerHTML = `<span class="tag ${esc(last.action)}">${esc(actionLabel)}</span> ${esc(cap(last.detail))}`;
    }
  }

  const changes = (advisor.changes || [])
    .map(
      (c) => `<div class="row ${c.reverted ? "is-muted" : ""}">
        <span class="tag ${esc(c.source)}">${esc(ADVISOR_SOURCE_LABEL[c.source] || humanize(c.source))}</span>
        <span class="mono">${esc(c.axis)}.${esc(c.param)} ${esc(c.before)}→${esc(c.applied)}</span>
        <span class="grow">${esc(cap(c.rationale))}</span>
      </div>`
    )
    .join("");
  listOr($("advisor-changes"), changes, "No changes yet tonight.");

  listOr(
    $("advisor-vetoes"),
    (advisor.vetoes || [])
      .map(
        (v) => `<div class="row">
          <span class="tag vetoed">${esc(VETO_LABEL[v.rule] || humanize(v.rule))}</span>
          <span class="grow">${esc(cap(v.detail))}</span>
        </div>`
      )
      .join(""),
    "Nothing refused."
  );
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

  decorateMarks("#page-guiding .starbox");

  // Arms renderParams()'s edit grace window the instant a field is focused
  // (not just on the first keystroke) so a render landing in the gap between
  // clicking in and typing can't destroy the field before anything is typed,
  // then keeps extending it on every keystroke for a slow typist.
  $("guide-params")?.addEventListener("focusin", (event) => {
    if (event.target.tagName === "INPUT") paramsEditingUntil = Date.now() + 3000;
  });
  $("guide-params")?.addEventListener("input", () => {
    paramsEditingUntil = Date.now() + 3000;
  });

  // A parameter edit goes straight to PHD2, so it commits on blur or Enter
  // rather than on every keystroke.
  $("guide-params")?.addEventListener("change", (event) => {
    const input = event.target;
    if (input.tagName !== "INPUT") return;
    paramsEditingUntil = 0; // committed -- no reason to keep the table frozen
    const entered = parseFloat(input.value);
    // Percent fields are display/edit-box only (see renderParams) -- PHD2 and
    // the advisor always get the raw 0.0-1.0 fraction back.
    const value = input.dataset.percent === "1" ? entered / 100 : entered;
    act(null, "/api/guiding/param", {
      body: JSON.stringify({ axis: input.dataset.axis, param: input.dataset.param, value }),
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

  // The guide buttons carry data-guide and are handled by the delegated click
  // listener in main.js; only the star crop needs a poller toggle here.
  document.addEventListener("visibilitychange", syncStarPolling);
}

/** Re-fit the trace after the page is shown (a hidden chart has no width). */
export function refresh() {
  drawTrace(state.guiding?.graph);
}
