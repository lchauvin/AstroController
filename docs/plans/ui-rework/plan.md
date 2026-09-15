# UI/UX Rework — Implementation Plan

Dark-blueprint theme (Industry design system ported from AstroBlog), three pages
(Dashboard / Guiding / Settings), each fitting one desktop viewport. Red
night-vision toggle retained. No backend API changes; no build step; vanilla ES
modules + vendored uPlot.

This resolves the four open questions in `brief.md` and then lays out the work.

---

## 1. Decisions on the open questions

### 1.1 Eccentricity — **omit from the UI; do not add backend computation in this task**

*Decision:* The Dashboard "latest-image stats" panel shows HFR, stars, ADU
(median/mean/max), saturation flag — all present on `frames[0]`. Eccentricity is
**not** displayed and **not** computed.

*Rationale:*
- No `eccentricity` field exists anywhere in the frame pipeline (verified:
  `quality/` and `runtime.py` produce `hfr`, `hfr_stdev`, `stars`, `median`,
  `mean`, `max`, `flags`). The brief already notes this.
- HFR ± stdev already communicates focus quality; eccentricity would be a
  nice-to-have, not a monitoring necessity, and the brief's own constraint #2
  ("if a field is genuinely missing, plan the minimal backend addition
  separately") says to keep it out of the UI task.
- Adding it means a new backend computation (star ellipse fitting), a new field
  on the SSE frame payload, and a test — a self-contained piece of work that
  must not block the theme rework.

*Follow-up ticket (separate, not this plan):* "Compute per-frame eccentricity in
`quality/` and surface it on the frame payload + Dashboard stats." Logged in the
feature file's Tickets section.

### 1.2 One-screen fit strategy — **12-col grid → fixed rows, `100dvh`, cards size themselves, no page scroll**

*Decision:* Keep the existing `.grid { grid-template-columns: repeat(12,1fr) }`
mechanic (it is already the unit every `.span-N` class speaks) but change the
page model so a page is a *bounded* region, not a growing document:

- The outer shell fills the viewport: `.app` is `height: 100dvh`,
  `overflow: hidden`. Only the *image-bearing* frameboxes and the *list*
  containers scroll **internally** — the page itself never scrolls.
- Each page's grid gets explicit `grid-template-rows` sized in `fr`/fixed units
  that sum to the available height, and cards use `min-height: 0` + internal
  `overflow` so content clips rather than pushes the page taller.
- The heavy, variable-height content (latest frame, guide-star crop, charts) is
  given **fixed or `fr`-bounded boxes** (CSS `aspect-ratio` + a capped height),
  exactly as today's `#dash-framebox { height: 330px }` does. Charts already
  self-size via `ResizeObserver` (see `charts.js`), so they fit whatever row
  height they are given.

*Why this is credible:* the current stylesheet already proves the pattern works
(`framebox` aspect-ratio + fixed height, `list { max-height … overflow-y }`,
`table-scroll`, `ResizeObserver`-driven charts). The rework generalises that
pattern to whole pages instead of leaving it ad hoc per widget. The two pages
with the most content (Dashboard, Guiding) are enumerated below with concrete
row budgets.

*Fit budget is per-page and explicit* (see §3). If a panel genuinely cannot fit,
it is the panel that is compressed (smaller chart, capped list) — never the
page that grows.

### 1.3 Theme architecture — **new `theme.css` token sheet, port AstroBlog's Industry tokens onto a dark ground, two `data-theme` variants**

*Decision:* Introduce a dedicated token layer and split styling into three files.

New/changed files:
- **`static/theme.css`** (new) — the token sheet. Ports AstroBlog's
  `tokens.css`, re-based on a dark ground:
  - Typography: `--font-heading` (Barlow Condensed, 600, condensed uppercase
    tracked headings), `--font-body` (Barlow), `--font-mono` (ui-monospace).
  - Hairlines: `--divider: color-mix(in srgb, <ink> 16%, transparent)`, 1px.
  - Square corners: keep `--radius-sm/md/lg` tokens but panels/cards are
    rendered **unrounded** by design (Industry look). Radius tokens remain for
    the rare rounded control (pills/toggles) only.
  - Spacing scale `3.4 / 6.8 / 10.2 / 13.6 / 20.4 / 27.2 px` (note the real
    scale skips `--space-5`/`--space-7`, matching upstream).
  - `--page-max: 1280px`, `--transition: 140ms ease-out`, focus-visible 2px
    accent outline.
  - `+` registration-mark treatment for framed media (the near-black mount
    `#0c0e11` behind images becomes the blueprint "mount plate").
  - **Dark ground mapping:** Industry's light ground (`#f2f2f3`/`#e9e9ea` on
    `#1d1f20` ink) is inverted onto the existing dark ground `#0a0e17` family.
    Accent steel blue `#5980a6` is kept but lightened for dark-ground contrast
    (upstream's own accent-100..900 ramp already contains dark-appropriate
    steps; we select from it rather than inventing new hexes).
  - **Do not** copy the upstream names `--color-border` / `--color-text-dim`
    (they are undefined upstream; the brief explicitly calls this out).

  Two `data-theme` variants are defined by overriding this sheet:
  - `html[data-theme="dark"]` — the default dark blueprint.
  - `html[data-theme="night"]` — red night-vision. Keeps today's working
    behaviour: maps the whole palette onto one red hue and routes photographs
    through the `feColorMatrix` in the sprite (luminance → red, so bright stars
    stay red instead of clipping to yellow). Status is never hue-alone anywhere,
    which night mode depends on.

- **`static/style.css`** (rewritten) — layout + components, consuming only
  `theme.css` tokens. The Industry visual language (hairline borders, square
  corners, mono micro-labels, condensed uppercase tracked headings,
  registration marks) replaces the current rounded/gradient look.

- **`index.html`** — link `theme.css` before `style.css`; add the Google-Fonts
  link (see below).

*Fonts:* Barlow + Barlow Condensed come from **Google Fonts via `<link>`**, with
`ui-monospace`/system fallbacks so the page renders offline. The brief leaves
"self-host or link" to the planner; I choose **link** because the project
already loads uPlot as a vendored asset for offline use only where it is
load-bearing (charts), whereas fonts degrade gracefully to system sans. If a
fully-offline story is later required, self-hosting the two Barlow weights is a
drop-in replacement (a TODO is noted in §6). The uPlot vendoring stays as-is.

*Contrast / a11y:* the accent-on-dark step is chosen from the upstream
lightness ramp so text/status keeps ≥3:1 (aim 4.5:1 for text). Charts keep the
existing "colour + label + icon, never hue alone" rule, which is also what
makes night mode survivable.

### 1.4 File/module structure — **keep `core.js`, `charts.js`, `settings.js`; merge `imaging.js` into page modules; split `panels.js` per page**

*Decision:* The state plumbing is good and stays. The render layer is
re-organised by *page* (the new unit the brief introduces), not by widget.

| File | Fate |
|------|------|
| `js/core.js` | **Keep, unchanged in role.** Auth, SSE `/api/stream`, state store, `on/emit`, `fmt/esc`, toasts, prefs, `act()`. The SSE-driven structure is kept per brief constraint. |
| `js/charts.js` | **Keep.** `LineChart`, sparkline, progressRing, `rethemeAll`. Already token-driven via `cssVar`, so it picks up the new sheet automatically once the CSS var *names it reads* are provided by `theme.css`. |
| `js/settings.js` | **Keep, restyle only.** The settings page is "existing functionality, restyled." The data-described rendering (groups/fields/bounds) is untouched; only class names it emits are aligned to the new components. |
| `js/imaging.js` | **Dissolve.** Its two responsibilities move to the pages that now own them: frame loading/stretch/lightbox → the Dashboard viewer; guide-star polling → the Guiding page. The blob-fetch/object-URL/revoke logic and the `renderKey` caching are reused verbatim inside those modules. |
| `js/panels.js` | **Split.** Today it is every panel for 5 pages in one file. It becomes per-page render modules (below). |
| `js/main.js` | **Slim.** Routing drops to 3 pages; panel registration and the boot sequence stay. |
| `js/pages/dashboard.js` | **New.** Dashboard render functions. |
| `js/pages/guiding.js` | **New.** Guiding render functions + guide-star poller + advisor panel. |
| `js/pages/settings.js` | (settings.js stays at `js/settings.js`; only renamed if a `pages/` layout is preferred — see note) |

*Naming note:* to keep the diff reviewable and import paths stable, the new page
modules live under `js/pages/`. `settings.js` remains at `js/settings.js`
(it is not split, merely restyled) unless we decide mid-implementation to move
it for symmetry — that cosmetic choice is left to the implementer and does not
change the plan.

*Why not a full rewrite:* the SSE snapshot + per-topic render model already
decouples panels cleanly; the rework is a re-mapping of which panels exist and
how they look, not a change to how data flows. Reusing `core/charts/settings`
and the image-fetch internals minimises risk and respects "minimal changes."

---

## 2. New page structure & content map

### Page 1 — Dashboard (monitoring)
Content (from brief, all present in state):
- **Latest image + viewer** — `preview` + `frames[0]`; blob-fetched PNG with
  stretch controls (brightness / white point / invert / reset) and lightbox.
  Framed with `+` registration marks on the near-black mount plate.
- **Guiding error summary** — `guiding.rms` (total/RA/Dec, SNR), compact, plus
  a small guide trace.
- **Latest-image stats** — saturation flag, HFR ± stdev, stars, ADU
  (median/mean/max). *(Eccentricity omitted — §1.1.)*
- **Weather for the night** — `weather.current` + `weather.summary` + hourly
  cloud sparkline.
- **Moon phase** — `sky` (`phase_name`, `illumination`, `altitude_deg`, `up`).
- **Seeing** — derived client-side: `seeing″ = guiding.rms.hfd_med × phd2.pixel_scale`
  (both fields confirmed to exist). Show-if-available: rendered only when both
  operands are finite, else the cell shows the placeholder dash.
- **Sequence + current step + progression** — `sequence` (`done/total`,
  `current_name`, steps) as a **compact** panel.
- **Equipment list with status** — `nina.devices` + the direct-PHD2 entry:
  camera temp, mount tracking, current filter, etc. (rich detail already in
  `device.detail`).

### Page 2 — Guiding
- **Guide-star crop** — `phd2` star image (polled, as today).
- **Guide stats** — HFD, star SNR, star count (the brief's "HDY/HDR" reads as
  HFD; SNR + HFD + peak are what `guiding.rms`/`phd2` actually provide).
- **Current PHD2 guiding parameters** — `phd2.params` table (writes straight to
  PHD2).
- **Guiding error / RMS + graph** — `guiding.rms` + `guiding.graph` (600s).
- **Advisor advice panel with toggle** — suggest-only vs auto-apply, mapping to
  existing `POST /api/advisor/tuning {enabled, mode}`; last action, changes,
  vetoes, revert-last/revert-all.
- **Polar alignment quality** — `tppa` Az/Alt/Total error + progress + status.

### Page 3 — Settings
Existing functionality restyled: config form (groups/fields), appearance
(night toggle, preview resolution, confirm), health list, learning store.

---

## 3. Layout plan (one-screen fit)

Target desktop viewport: 1280×800 and up, `--page-max: 1280px`. Both heavy pages
use a 12-col grid with explicit row templates; `100dvh` shell; internal scroll
only inside lists/tables.

### Dashboard grid (12 cols)
Budget the viewport as: topbar (auto) + a KPI strip + a 2-row content grid.

- Row A (KPI strip, `span-12`, ~110px): guiding RMS · guider state · seeing ·
  star size · stars · cloud. (Seeing tile is new; derived per §2.)
- Row B (the media row, ~55% of remaining height):
  - `span-7` — **Latest frame** card: fixed-height framebox (`fr`-bounded,
    `aspect-ratio` fallback) + caption + compact stretch controls.
  - `span-5` — **Guiding summary** card: RMS metrics + a compact guide trace
    (bounded chart height).
- Row C (the status row, ~45% remaining):
  - `span-4` — **Frame stats** (flags + kv table).
  - `span-4` — **Weather & sky** (metrics + moon + a bounded cloud sparkline).
  - `span-4` — **Sequence** (compact: ring + current + done/total) stacked over
    **Equipment** (capped list).

Cards use `min-height: 0` and internal `overflow` so nothing pushes the page.

### Guiding grid (12 cols)
- Left `span-8`: **Guide trace** (largest element, RMS + graph + controls) over
  **parameters** summary.
- Right `span-4`: **Guide star** (square crop) over **guide stats** over
  **polar alignment** (TPPA errors + progress).
- Full-width bottom (`span-12`) or a right-column block: **Advisor** (toggle,
  last action, capped changes/vetoes lists, revert buttons).

Exact span tuning is an implementation detail; the invariant is "page height =
100dvh, no page scroll."

### Settings grid
Settings is allowed to scroll if a rig exposes many fields (its length is
data-driven and unbounded), but the common case (≤2 groups + appearance +
system) is laid out to fit: `span-6` config groups beside `span-6`
appearance/system. The one-screen mandate is applied where the content is
bounded (Dashboard, Guiding); Settings is best-effort.

---

## 4. Work breakdown (ordered tickets)

Each ticket is independently verifiable by a manual smoke (no frontend test
harness exists; pytest covers backend only — see §7).

1. **T1 · Token sheet + fonts.** Create `theme.css` (dark + night variants),
   port Industry tokens onto the dark ground, wire the Google-Fonts link +
   fallbacks, define all CSS var names `charts.js` reads. *Done when:* pages
   render with hairline borders / square corners / condensed headings / mono
   micro-labels, and toggling night mode re-maps the palette + reds the images.
2. **T2 · Shell + routing to 3 pages.** New `index.html` nav (Dashboard /
   Guiding / Settings), slim `main.js` `PAGES` map to 3 entries, remove
   Imaging/Sequence routes. *Done when:* only three pages exist and navigate.
3. **T3 · Style rewrite.** Rewrite `style.css` to consume tokens and implement
   the `100dvh` no-scroll page model + explicit grid rows. *Done when:*
   existing pages (before content re-map) fit one viewport with no page scroll.
4. **T4 · Dashboard page module.** `js/pages/dashboard.js`: latest-frame viewer
   (move + generalise `imaging.js` fetch/stretch/lightbox), guiding summary,
   frame stats (no eccentricity), weather+moon, seeing tile, compact sequence,
   equipment. *Done when:* Dashboard fits 1280×800 with all §2 items present.
5. **T5 · Guiding page module.** `js/pages/guiding.js`: guide-star crop (move
   poller), stats, params table, RMS + graph, advisor panel (toggle +
   changes/vetoes + revert), polar alignment. *Done when:* Guiding fits and the
   advisor toggle posts to `/api/advisor/tuning`.
6. **T6 · Settings restyle.** Align `settings.js` output + Settings HTML to the
   new components/tokens. *Done when:* settings save/revert path + appearance +
   health/store render in the new theme.
7. **T7 · Night-vision pass + polish.** Verify `+` registration marks on framed
   media, focus-visible outlines, transitions, and that night mode preserves
   every non-colour status channel. *Done when:* both themes are legible and
   status is never hue-only.
8. **T8 · Cleanup.** Delete `js/imaging.js` and `js/panels.js` (now emptied),
   prune dead CSS, remove unused icon defs if any. *Done when:* no dead code.

---

## 5. Data dependency check

Everything the new UI reads already exists in state (verified against
`hub.py` and the render code):

- `frames[0]`: `hfr`, `hfr_stdev`, `stars`, `median`, `mean`, `max`,
  `temperature`, `gain`, `filter`, `exposure_s`, `target`, `date`,
  `flags{saturated, cloud_suspect, tracking_suspect}`, `baseline.median_hfr`. ✓
- `guiding.rms.{rms_total, rms_ra, rms_dec, snr_med, hfd_med}`, `guiding.graph`
  (600s), `guiding.disturbed`. ✓
- `phd2.{connected, app_state, pixel_scale, params, available_params, paused,
  guiding_for_s, profile_name, exposure_ms}`. ✓
- `sequence.{available, done, total, current_name, steps, error}`. ✓
- `weather.{current, summary, hourly, error}`, `sky.{phase_name, illumination,
  altitude_deg, up, precise}`. ✓
- `nina.devices[]` (rich `detail` strings). ✓
- `advisor.{mode, enabled, last, changes, vetoes}`. ✓
- `tppa.{AzimuthError, AltitudeError, TotalError, Progress, Status, running}`. ✓
- `health[]`, `session.store`. ✓

No `eccentricity` (§1.1) — omitted. Seeing derived per §2. **No backend changes
required for this plan.**

Existing endpoints consumed (unchanged): `/api/stream`, `/api/state`,
`/api/frame/latest.png`, `/api/guide-star.png`, `/api/settings`,
`/api/advisor/tuning`, `/api/advisor/revert-last`, `/api/advisor/revert-all`,
`/api/guiding/*`, `/api/sequence/*`, `/api/tppa/start|stop`.

---

## 6. Constraints honoured & deliberate non-goals

- **No build step**; vanilla ES modules + vendored uPlot retained. Barlow fonts
  via `<link>` with graceful system fallback (self-hosting is a noted TODO, not
  part of this plan).
- **No backend API changes**; the only "missing" field (eccentricity) is
  deferred to a separate ticket.
- **SSE structure kept** (`core.js` unchanged in role).
- **Mobile/responsive out of scope.** The existing `@media` phone layout is
  removed in T3 (desktop single-screen only, per human ruling) — the rework
  targets 1280×800+.
- **No new data-acquisition features.**

---

## 7. Testing & verification

- **Backend:** unchanged; `pytest` continues to cover it (no edits expected).
- **Frontend:** no test harness exists (per brief, manual smoke acceptable).
  Each ticket lists a manual smoke as its done-condition. Final acceptance
  smoke matrix:
  1. All 3 pages load; nav switches without reload; hash routing works.
  2. Dashboard & Guiding fit 1280×800 with **no page scrollbar**; lists/images
     scroll internally only.
  3. Seeing tile shows a value when guiding + pixel scale are present; dash
     otherwise.
  4. Night toggle re-maps palette and reds photographs; status remains labelled
     (not hue-only).
  5. Settings save/discard round-trips; restart banner appears when relevant.
  6. Advisor toggle posts suggest/auto; revert buttons confirm and toast.

---

## 8. Risks

- **One-screen fit on smaller desktops (<1280px wide / <800px tall):** mitigated
  by explicit row budgets + internal-scroll list caps; the invariant is enforced
  in code review, not assumed.
- **Offline fonts:** Google-Fonts link may fail on a fully offline observatory
  PC. Mitigation: system/stack fallbacks are defined so layout never breaks;
  self-host TODO logged.
- **Token-name drift** between `theme.css` and `charts.js` (`cssVar` reads):
  mitigated by defining, in T1, every var the chart code reads and grepping for
  them.

---

## 9. Out of scope (restated)

Mobile/responsive layout; new data acquisition; backend API shape changes;
eccentricity computation (separate ticket); Settings guaranteed one-screen fit
(best-effort only, content is data-driven).
