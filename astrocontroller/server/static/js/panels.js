/* Cross-page renderers not tied to a single page.
 *
 * The Dashboard lives in js/pages/dashboard.js and the Guiding page in
 * js/pages/guiding.js. What remains here is the topbar (present on every page)
 * and the Settings health/store panels.
 *
 * Each renderer is pure "state in, DOM out", registered against the topics it
 * depends on, so one topic update repaints only its own panels.
 */
"use strict";

import { $, esc, fmt, on, state } from "./core.js";
import { DASH, chip, listOr, setPill } from "./widgets.js";

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

  const host = $("topbar-chips");
  if (host) host.innerHTML = chips.join("");
}

/* ── health & learning store (Settings page) ─────────────────────── */

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
  on(["health", "session"], "health", renderHealth);
}
