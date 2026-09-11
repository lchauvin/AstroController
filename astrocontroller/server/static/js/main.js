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
import { loadSettings, registerSettings } from "./settings.js";
import { refresh as refreshDashboard, registerDashboard } from "./pages/dashboard.js";
import { refresh as refreshGuiding, registerGuiding, syncStarPolling } from "./pages/guiding.js";

const PAGES = {
  dashboard: "Dashboard",
  guiding: "Guiding",
  settings: "Settings",
};

/* Per-page "you just became visible" nudges: a chart drawn into a hidden
   element has no width to measure, and the guide star is polled only while its
   page is on screen. */
const PAGE_HOOKS = {
  dashboard: refreshDashboard,
  guiding: () => { refreshGuiding(); syncStarPolling(); },
  settings: () => {},
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

  requestAnimationFrame(() => {
    emitAll();
    PAGE_HOOKS[page]();
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
    } else if (button.id === "night-toggle") {
      setNight(!prefs.night);
    }
    /* Parameters, advisor tuning/revert and TPPA start/stop are wired directly
       inside js/pages/guiding.js -- deliberately not here, so the delegated
       handler cannot fire them twice. */
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
registerDashboard();
registerGuiding();
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
