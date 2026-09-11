/* Small presentational helpers shared by the page modules.
 *
 * These used to live inside panels.js. They moved out when the panels were
 * split per page (js/pages/*), so the Dashboard and Guiding modules share the
 * same tile/pill/list vocabulary without depending on each other.
 */
"use strict";

import { $, esc, fmt, icon, state } from "./core.js";

export const DASH = "–";

/** Target RMS comes from the config; 0.6" is the built-in default. */
export const targetRms = () => state.config?.target_rms || 0.6;

/**
 * Status band for a guiding RMS.
 *
 * Relative to the configured target rather than absolute: 0.9" is excellent
 * on a 250mm refractor and dreadful on a 2m RC.
 */
export function rmsStatus(total) {
  if (total === null || total === undefined) return "idle";
  const target = targetRms();
  if (total <= target * 1.15) return "good";
  if (total <= target * 2) return "warn";
  return "bad";
}

export function tile({ label, value, unit = "", sub = "", status = "idle", spark = "" }) {
  return `<article class="tile is-${status}">
    <div class="tile-label">${esc(label)}</div>
    <div class="tile-value">${esc(value)}${unit ? `<span class="tile-unit">${esc(unit)}</span>` : ""}</div>
    <div class="tile-sub">${sub}</div>
    ${spark}
  </article>`;
}

export function setPill(id, text, kind = "") {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.className = `pill ${kind}`;
}

export function listOr(host, html, emptyText) {
  if (!host) return;
  host.innerHTML = html || `<div class="list-empty">${esc(emptyText)}</div>`;
}

export const chip = (label, value) =>
  `<span class="chip"><span class="chip-label">${esc(label)}</span><b>${esc(value)}</b></span>`;

export const flagChip = (kind, glyph, text) =>
  `<span class="flag ${kind}">${icon(glyph)}${esc(text)}</span>`;

/* Registration marks: the blueprint `+` on framed media. A box supplies two
   corners via ::before/::after; this adds the other two once. */
export function decorateMarks(...selectors) {
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (!el || el.querySelector(".corner")) continue;
    for (const pos of ["tr", "bl"]) {
      const mark = document.createElement("i");
      mark.className = `corner ${pos}`;
      mark.setAttribute("aria-hidden", "true");
      el.appendChild(mark);
    }
  }
}
