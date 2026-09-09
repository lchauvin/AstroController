/* Entry point: routing, global controls, and the wiring between them.
 *
 * Everything below is one-time setup. Once `connect()` runs, the SSE feed
 * drives the panels and nothing here is on the hot path.
 */
"use strict";

import {
  $,
  act,
  api,
  applyTheme,
  connect,
  emitAll,
  prefs,
  savePrefs,
  state,
  toast,
} from "./core.js";
import { rethemeAll } from "./charts.js";
import { registerPanels } from "./panels.js";
import { registerImaging, refresh as refreshFrame, syncStarPolling } from "./imaging.js";
import { loadSettings, registerSettings } from "./settings.js";

const PAGES = {
  dashboard: "Dashboard",
  imaging: "Imaging",
  guiding: "Guiding",
  sequence: "Sequence",
  settings: "Settings",
};

/* ── routing ───────────────────────────────────────────────────────── */

function show(page) {
  if (!PAGES[page]) page = "dashboard";
  for (const name of Object.keys(PAGES)) {
    $(`page-${name}`).classList.toggle("is-active", name === page);
  }
  for (const button of document.querySelectorAll(".nav-item")) {
    button.classList.toggle("is-active", button.dataset.page === page);
  }
  $("page-title").textContent = PAGES[page];
  prefs.page = page;
  savePrefs();
  if (location.hash.slice(1) !== page) history.replaceState(null, "", `#${page}`);

  // A chart drawn into a hidden element has no width to measure, so any panel
  // that just became visible needs a nudge.
  requestAnimationFrame(() => {
    emitAll();
    refreshFrame();
    syncStarPolling();
  });
}

/* ── controls ──────────────────────────────────────────────────────── */

function registerControls() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;

    if (button.dataset.page) {
      show(button.dataset.page);
    } else if (button.dataset.seq) {
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
      setNight(!prefs.night);
    }
  });

  $("tuning-enabled").addEventListener("change", (event) => {
    act(null, "/api/advisor/tuning", { body: JSON.stringify({ enabled: event.target.checked }) });
  });

  $("tuning-mode").addEventListener("change", (event) => {
    act(null, "/api/advisor/tuning", { body: JSON.stringify({ mode: event.target.value }) });
  });

  // A parameter edit goes straight to PHD2, so it commits on blur or Enter
  // rather than on every keystroke.
  $("guide-params").addEventListener("change", (event) => {
    const input = event.target;
    if (input.tagName !== "INPUT") return;
    act(null, "/api/guiding/param", {
      body: JSON.stringify({
        axis: input.dataset.axis,
        param: input.dataset.param,
        value: parseFloat(input.value),
      }),
      okMessage: `${input.dataset.axis}.${input.dataset.param} set`,
    });
  });

  $("pref-night").addEventListener("change", (event) => setNight(event.target.checked));

  addEventListener("hashchange", () => show(location.hash.slice(1)));
}

function setNight(on) {
  prefs.night = !!on;
  savePrefs();
  applyTheme();
  // Chart colours are read from CSS custom properties at build time.
  rethemeAll();
}

function startClock() {
  const tick = () => {
    $("clock").textContent = new Date().toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  };
  tick();
  setInterval(tick, 20000);
}

/* ── boot ──────────────────────────────────────────────────────────── */

applyTheme();
registerPanels();
registerImaging();
registerSettings();
registerControls();
startClock();
show(location.hash.slice(1) || prefs.page);

// Settings are not on the telemetry stream: they change rarely and the page
// needs them before the first frame is judged good or bad.
loadSettings()
  .then(() => emitAll())
  .catch((err) => toast(`Could not load settings: ${err.message}`, "err"));

// One immediate snapshot so the page is populated even if the stream is slow
// to open, then the stream takes over.
api("/api/state")
  .then((snapshot) => {
    Object.assign(state, snapshot || {});
    emitAll();
  })
  .catch(() => {
    /* the stream will fill it in; a 503 here just means the runtime is starting */
  })
  .finally(connect);
