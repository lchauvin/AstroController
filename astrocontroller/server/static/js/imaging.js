/* The two live images: the last captured frame, and PHD2's guide star.
 *
 * Both are fetched as blobs rather than assigned straight to `img.src`. It
 * costs a few lines and buys three things: a 404 arrives as a readable message
 * instead of a broken-image glyph, the response headers (which frame, which
 * source) are available, and the old object URL can be revoked so a dashboard
 * left open all night does not accumulate decoded bitmaps.
 */
"use strict";

import { $, TOKEN, esc, fmt, on, prefs, savePrefs, state, url } from "./core.js";

const authHeaders = TOKEN ? { "X-Auth-Token": TOKEN } : {};

/* ── the captured frame ────────────────────────────────────────────── */

/**
 * Width to request for the dashboard thumbnail.
 *
 * Measured from the box it lands in rather than fixed: a 900px PNG for a
 * 320px phone card is bytes paid for over a hotspot and never seen. Capped at
 * 2x for retina and clamped so a collapsed layout cannot ask for a postage
 * stamp.
 */
function dashWidth() {
  const box = $("dash-framebox");
  const css = box?.clientWidth || 600;
  const scale = Math.min(window.devicePixelRatio || 1, 2);
  return Math.max(400, Math.min(1400, Math.round(css * scale)));
}

const shown = { dash: null, viewer: null };
const objectUrls = { dash: null, viewer: null };

function stretchParams(width) {
  return {
    width,
    background: prefs.stretchBackground,
    white: prefs.stretchWhite,
    invert: prefs.stretchInvert ? "true" : "false",
  };
}

/** A key that changes exactly when the rendered bytes would differ. */
function renderKey(preview, width) {
  return [
    preview.token,
    width,
    prefs.stretchBackground,
    prefs.stretchWhite,
    prefs.stretchInvert,
  ].join("|");
}

async function loadFrame(target, imgId, emptyId, width) {
  const preview = state.preview || {};
  const img = $(imgId);
  const empty = $(emptyId);
  if (!img) return;

  if (!preview.available || !preview.token) {
    img.removeAttribute("src");
    empty.hidden = false;
    empty.textContent = preview.reason || "Waiting for the first frame…";
    shown[target] = null;
    return;
  }

  const key = renderKey(preview, width);
  if (shown[target] === key) return;

  const spinner = $("viewer-spinner");
  if (target === "viewer" && spinner) spinner.hidden = false;
  try {
    const response = await fetch(url("/api/frame/latest.png", { ...stretchParams(width), v: preview.token }), {
      headers: authHeaders,
    });
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
    if (objectUrls[target]) URL.revokeObjectURL(objectUrls[target]);
    objectUrls[target] = next;
    img.src = next;
    empty.hidden = true;
    shown[target] = key;
  } catch (err) {
    img.removeAttribute("src");
    empty.hidden = false;
    empty.textContent = err.message;
    shown[target] = null;
  } finally {
    if (target === "viewer" && spinner) spinner.hidden = true;
  }
}

function renderPreview() {
  const preview = state.preview || {};
  const frame = (state.frames || [])[0];

  const sourceLabel = !preview.available
    ? "no image"
    : preview.source === "share"
    ? "share"
    : "NINA preview";
  for (const id of ["frame-source", "viewer-source"]) {
    const node = $(id);
    if (node) {
      node.textContent = sourceLabel;
      node.className = `pill ${preview.available ? "good" : ""}`;
    }
  }

  const caption = [];
  if (preview.filename) caption.push(`<b>${esc(preview.filename)}</b>`);
  if (frame) {
    if (frame.target) caption.push(esc(frame.target));
    if (frame.filter) caption.push(`${esc(frame.filter)} · ${esc(fmt(frame.exposure_s, 0))}s`);
    if (Number.isFinite(frame.hfr)) caption.push(`HFR <b>${esc(fmt(frame.hfr))}</b>`);
    if (frame.stars !== null && frame.stars !== undefined) caption.push(`<b>${esc(frame.stars)}</b> stars`);
  }
  for (const id of ["dash-frame-caption", "viewer-caption"]) {
    const node = $(id);
    if (node) node.innerHTML = caption.map((part) => `<span>${part}</span>`).join("");
  }

  refresh();
}

/**
 * Reload whichever images are on screen and out of date.
 *
 * "Auto" governs the viewer only. Somebody who froze the viewer to study one
 * sub still wants the dashboard thumbnail to keep up -- and a viewer that has
 * never loaded is shown once regardless, so the page is not blank.
 */
export function refresh({ force = false } = {}) {
  if (force) shown.dash = shown.viewer = null;
  loadFrame("dash", "dash-frame", "dash-frame-empty", dashWidth());
  const wanted = force || prefs.viewerAuto !== false || shown.viewer === null;
  if (isVisible("page-imaging") && wanted) {
    loadFrame("viewer", "viewer-frame", "viewer-empty", Number(prefs.viewerWidth) || 1400);
  }
}

const isVisible = (id) => $(id)?.classList.contains("is-active");

/* ── the guide star ────────────────────────────────────────────────── */

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
  const wanted = isVisible("page-guiding") && document.visibilityState === "visible";
  if (wanted && !starTimer) {
    pollStar();
    starTimer = setInterval(pollStar, 2500);
  } else if (!wanted && starTimer) {
    clearInterval(starTimer);
    starTimer = null;
  }
}

/* ── wiring ────────────────────────────────────────────────────────── */

export function registerImaging() {
  on(["preview", "frames"], "preview", renderPreview);

  $("viewer-refresh").addEventListener("click", () => refresh({ force: true }));

  const auto = $("viewer-auto");
  auto.checked = prefs.viewerAuto !== false;
  auto.addEventListener("change", () => {
    prefs.viewerAuto = auto.checked;
    savePrefs();
    if (auto.checked) refresh();
  });

  const bg = $("stretch-bg");
  const white = $("stretch-white");
  const invert = $("stretch-invert");
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
    // stop moving rather than re-reading a 50MB file on every pixel of travel.
    debounce = setTimeout(() => refresh({ force: true }), 260);
  };
  bg.addEventListener("input", restretch);
  white.addEventListener("input", restretch);
  invert.addEventListener("change", restretch);

  $("stretch-reset").addEventListener("click", () => {
    bg.value = prefs.stretchBackground = 0.18;
    white.value = prefs.stretchWhite = 99.9;
    invert.checked = prefs.stretchInvert = false;
    savePrefs();
    refresh({ force: true });
  });

  // Lightbox: the dashboard thumbnail and the viewer both open full size.
  const lightbox = $("lightbox");
  const openLightbox = (src) => {
    if (!src) return;
    $("lightbox-img").src = src;
    lightbox.hidden = false;
  };
  $("dash-frame").addEventListener("click", (event) => openLightbox(event.target.src));
  $("viewer-full").addEventListener("click", () => openLightbox($("viewer-frame").src));
  $("viewer-frame").addEventListener("click", (event) => openLightbox(event.target.src));
  $("lightbox-close").addEventListener("click", () => (lightbox.hidden = true));
  lightbox.addEventListener("click", (event) => {
    if (event.target === lightbox) lightbox.hidden = true;
  });
  addEventListener("keydown", (event) => {
    if (event.key === "Escape") lightbox.hidden = true;
  });

  document.addEventListener("visibilitychange", syncStarPolling);
}
