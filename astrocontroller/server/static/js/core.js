/* Plumbing shared by every panel: auth, the state store, the SSE feed,
 * formatting, toasts, browser-local preferences.
 *
 * The server opens each SSE connection with a full snapshot and re-sends one
 * whenever a client falls behind, so nothing here ever reconciles a partial
 * history -- it renders whatever it was last given.
 */
"use strict";

export const $ = (id) => document.getElementById(id);
export const el = (sel, root = document) => root.querySelector(sel);
export const els = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ── auth ──────────────────────────────────────────────────────────── */

// A token is required when the server is not on loopback. EventSource cannot
// set headers, so it travels as a query parameter; fetch() uses the header.
export const TOKEN = new URLSearchParams(location.search).get("token") || "";
const authQuery = TOKEN ? `token=${encodeURIComponent(TOKEN)}` : "";
const authHeaders = TOKEN ? { "X-Auth-Token": TOKEN } : {};

/** Append the token (and any extra params) to a path. */
export function url(path, params = {}) {
  const query = new URLSearchParams(params);
  if (TOKEN) query.set("token", TOKEN);
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}

export async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders, ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {
      /* a non-JSON error body is still an error */
    }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ── state ─────────────────────────────────────────────────────────── */

export const state = {};

const listeners = new Map();

/**
 * Subscribe to one or more state topics.
 *
 * Panels render independently and one throwing must never blank the rest of
 * the dashboard: losing the weather forecast is an annoyance, losing the
 * guiding readout because of it is not.
 */
export function on(topics, name, fn) {
  for (const topic of [].concat(topics)) {
    if (!listeners.has(topic)) listeners.set(topic, []);
    listeners.get(topic).push({ name, fn });
  }
}

export function emit(topic) {
  for (const { name, fn } of listeners.get(topic) || []) {
    try {
      fn(state);
    } catch (err) {
      console.error(`panel "${name}" failed on "${topic}":`, err);
    }
  }
}

export function emitAll() {
  for (const topic of listeners.keys()) emit(topic);
}

// Topics whose payload several panels derive from under a different name.
const ALIASES = { phd2: ["guiding"], sky: ["weather"], guiding: ["phd2"] };

export function connect() {
  const source = new EventSource(url("/api/stream"));
  const dot = $("conn-dot");
  const text = $("conn-text");

  source.onopen = () => {
    dot.classList.add("live");
    text.textContent = "live";
  };
  source.onerror = () => {
    dot.classList.remove("live");
    text.textContent = "reconnecting…";
    // EventSource reconnects on its own; that is why SSE was chosen over a
    // websocket for a dashboard left open all night on a phone.
  };
  source.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (message.type === "snapshot") {
      Object.assign(state, message.data || {});
      emitAll();
      return;
    }
    state[message.type] = message.data;
    emit(message.type);
    for (const alias of ALIASES[message.type] || []) emit(alias);
  };
}

/* ── formatting ────────────────────────────────────────────────────── */

const DASH = "–";

export function fmt(value, digits = 2, dash = DASH) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) {
    return dash;
  }
  return Number(value).toFixed(digits);
}

export function esc(text) {
  return String(text ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

export function icon(name, className = "") {
  return `<svg viewBox="0 0 24 24" class="${className}"><use href="#i-${name}"/></svg>`;
}

/** "3m ago" / "just now" -- short enough for a table cell. */
export function ago(iso) {
  if (!iso) return DASH;
  const then = Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (Number.isNaN(then)) return DASH;
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function clock(iso) {
  if (!iso) return DASH;
  const match = String(iso).match(/(\d{2}:\d{2})/);
  return match ? match[1] : DASH;
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return DASH;
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${total % 60}s`;
  return `${total}s`;
}

/** Read a CSS custom property, so charts follow the theme. */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ── toasts ────────────────────────────────────────────────────────── */

export function toast(message, kind = "") {
  const stack = $("toasts");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  const mark = kind === "err" ? "alert" : kind === "ok" ? "check" : "";
  node.innerHTML = `${mark ? icon(mark) : ""}<span>${esc(message)}</span>`;
  stack.appendChild(node);
  setTimeout(() => node.remove(), kind === "err" ? 7000 : 3200);
}

/* ── preferences (this browser only) ───────────────────────────────── */

const DEFAULT_PREFS = {
  night: false,
  page: "dashboard",
  viewerWidth: 1400,
  confirm: true,
  stretchBackground: 0.18,
  stretchWhite: 99.9,
  stretchInvert: false,
  viewerAuto: true,
};

function readPrefs() {
  try {
    return { ...DEFAULT_PREFS, ...JSON.parse(localStorage.getItem("astro.prefs") || "{}") };
  } catch (_) {
    return { ...DEFAULT_PREFS };
  }
}

export const prefs = readPrefs();

export function savePrefs() {
  try {
    localStorage.setItem("astro.prefs", JSON.stringify(prefs));
  } catch (_) {
    /* private browsing; the UI still works, it just forgets */
  }
}

export function applyTheme() {
  document.documentElement.dataset.theme = prefs.night ? "night" : "dark";
  const button = $("night-toggle");
  if (button) button.setAttribute("aria-pressed", String(!!prefs.night));
  const box = $("pref-night");
  if (box) box.checked = !!prefs.night;
}

/* ── actions ───────────────────────────────────────────────────────── */

/** POST a control endpoint with confirmation, busy state and a toast. */
export async function act(button, path, options = {}) {
  const question = button?.dataset.confirm;
  if (question && prefs.confirm && !confirm(question)) return null;
  button?.classList.add("is-busy");
  try {
    const result = await api(path, { method: "POST", ...options });
    toast(options.okMessage || "Done", "ok");
    return result;
  } catch (err) {
    toast(err.message, "err");
    return null;
  } finally {
    button?.classList.remove("is-busy");
  }
}
