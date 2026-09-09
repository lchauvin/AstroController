/* The Settings page.
 *
 * The server describes the page as data -- groups, fields, kinds, bounds,
 * help text -- and this renders whatever it is given. Adding a setting is a
 * one-line change in `settings.py` and nothing here.
 *
 * Edits are staged locally and only sent on Save, so a half-typed IP address
 * never reaches a running observatory.
 */
"use strict";

import { $, api, esc, fmt, prefs, savePrefs, state, toast } from "./core.js";

let described = null;
const pending = new Map();

export async function loadSettings() {
  described = await api("/api/settings");
  renderGroups();
  renderBounds();
  publishConfig();
}

/**
 * Share the few config values the dashboard reasons about.
 *
 * The target RMS decides whether a guiding number is painted as good or bad,
 * and that judgement has to follow the rig rather than a constant baked into
 * the stylesheet.
 */
function publishConfig() {
  const values = {};
  for (const group of described.groups) {
    for (const field of group.fields) values[field.path] = field.value;
  }
  state.config = { target_rms: values["tuning.target_rms_arcsec"] || 0.6 };
}

function control(field) {
  const id = `set-${field.path.replace(/\./g, "-")}`;
  const value = pending.has(field.path) ? pending.get(field.path) : field.value;

  if (field.kind === "bool") {
    return `<label class="switch"><input type="checkbox" id="${id}" data-path="${esc(field.path)}"
      ${value ? "checked" : ""}><span></span></label>`;
  }
  if (field.kind === "choice") {
    const options = field.choices
      .map((c) => `<option value="${esc(c)}" ${c === value ? "selected" : ""}>${esc(c)}</option>`)
      .join("");
    return `<select class="select" id="${id}" data-path="${esc(field.path)}">${options}</select>`;
  }
  if (field.kind === "number") {
    return `<input type="number" id="${id}" data-path="${esc(field.path)}"
      value="${value ?? ""}"
      ${field.min !== null ? `min="${field.min}"` : ""}
      ${field.max !== null ? `max="${field.max}"` : ""}
      ${field.step !== null ? `step="${field.step}"` : ""}
      placeholder="${esc(field.placeholder)}">`;
  }
  return `<input type="text" id="${id}" data-path="${esc(field.path)}"
    value="${esc(value ?? "")}" placeholder="${esc(field.placeholder)}"
    autocomplete="off" spellcheck="false">`;
}

function renderGroups() {
  $("settings-path").textContent = described.simulated
    ? "Simulation mode (--fake) — these values point at the in-process simulator and cannot be saved"
    : described.source_path
    ? `Saving to ${described.source_path}`
    : "No config file yet — saving will create astrocontroller.toml";

  $("settings-groups").innerHTML = described.groups
    .map(
      (group) => `<section class="card span-6">
        <header class="card-head"><h2>${esc(group.title)}</h2></header>
        ${group.fields
          .map((field) => {
            const id = `set-${field.path.replace(/\./g, "-")}`;
            return `<div class="field-row ${pending.has(field.path) ? "is-dirty" : ""}"
                         data-row="${esc(field.path)}">
              <div class="field">
                <label for="${id}">${esc(field.label)}${
              field.live ? "" : `<span class="badge-restart">restart</span>`
            }</label>
                ${field.help ? `<p class="field-help">${esc(field.help)}</p>` : ""}
              </div>
              ${control(field)}
            </div>`;
          })
          .join("")}
      </section>`
    )
    .join("");

  if (described.writable === false) {
    for (const input of document.querySelectorAll("#settings-groups [data-path]")) {
      input.disabled = true;
    }
  }
  updateActions();
}

function renderBounds() {
  const rows = (described.bounds || [])
    .map(
      (b) => `<tr>
        <td>${esc(b.axis)} · ${esc(b.param)}</td>
        <td>${esc(fmt(b.lo))} – ${esc(fmt(b.hi))}</td>
        <td>±${esc(fmt(b.max_delta))}</td>
        <td>${esc(Math.round(b.cooldown_s / 60))}m</td>
      </tr>`
    )
    .join("");
  $("bounds-table").innerHTML = rows
    ? `<thead><tr><th>Parameter</th><th>Range</th><th>Max step</th><th>Cooldown</th></tr></thead><tbody>${rows}</tbody>`
    : `<tbody><tr><td class="list-empty">No bounds configured.</td></tr></tbody>`;
}

function fieldFor(path) {
  for (const group of described.groups) {
    for (const field of group.fields) if (field.path === path) return field;
  }
  return null;
}

function stage(path, raw) {
  const field = fieldFor(path);
  if (!field) return;
  let value = raw;
  if (field.kind === "number") value = raw === "" ? null : Number(raw);

  const same = JSON.stringify(value) === JSON.stringify(field.value);
  if (same) pending.delete(path);
  else pending.set(path, value);

  document
    .querySelector(`[data-row="${CSS.escape(path)}"]`)
    ?.classList.toggle("is-dirty", pending.has(path));
  updateActions();
}

function updateActions() {
  const count = pending.size;
  const locked = described?.writable === false;
  $("settings-save").disabled = count === 0 || locked;
  $("settings-revert").disabled = count === 0;
  $("settings-state").textContent = locked
    ? "read-only"
    : count
    ? `${count} unsaved change${count === 1 ? "" : "s"}`
    : "";
}

async function save() {
  const values = Object.fromEntries(pending);
  const button = $("settings-save");
  button.classList.add("is-busy");
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ values }),
    });
    pending.clear();
    described = result;
    renderGroups();
    renderBounds();
    publishConfig();
    toast(result.saved_to ? "Settings saved" : "Settings applied", "ok");

    const banner = $("settings-restart");
    const restart = result.restart_required || [];
    banner.hidden = restart.length === 0;
    if (restart.length) {
      banner.innerHTML =
        `<svg viewBox="0 0 24 24"><use href="#i-alert"/></svg>` +
        `<span>Saved. <b>${esc(restart.join(", "))}</b> ` +
        `${restart.length === 1 ? "takes" : "take"} effect when AstroController restarts.</span>`;
    }
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.classList.remove("is-busy");
  }
}

export function registerSettings() {
  const groups = $("settings-groups");
  groups.addEventListener("input", (event) => {
    const path = event.target.dataset?.path;
    if (path) stage(path, event.target.type === "checkbox" ? event.target.checked : event.target.value);
  });
  groups.addEventListener("change", (event) => {
    const path = event.target.dataset?.path;
    if (path) stage(path, event.target.type === "checkbox" ? event.target.checked : event.target.value);
  });

  $("settings-save").addEventListener("click", save);
  $("settings-revert").addEventListener("click", () => {
    pending.clear();
    renderGroups();
  });

  // Browser-local preferences: applied immediately, never sent to the server.
  const night = $("pref-night");
  night.checked = !!prefs.night;

  const width = $("pref-viewer-width");
  width.value = String(prefs.viewerWidth || 1400);
  width.addEventListener("change", () => {
    prefs.viewerWidth = Number(width.value);
    savePrefs();
    import("./imaging.js").then((m) => m.refresh({ force: true }));
  });

  const confirmBox = $("pref-confirm");
  confirmBox.checked = prefs.confirm !== false;
  confirmBox.addEventListener("change", () => {
    prefs.confirm = confirmBox.checked;
    savePrefs();
  });
}
