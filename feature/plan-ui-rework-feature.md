# ui-rework: Blueprint-theme UI, 3-page restructure

Status: implemented

## What and why
Rebuild the AstroController web UI as three pages — Dashboard (monitoring), Guiding,
Settings — styled after AstroBlog's engineering-blueprint design system, adapted to a
dark ground with a red night-vision toggle. Imaging and Sequence pages are removed;
their content folds into Dashboard. Each page fits a single desktop viewport without
scrolling. Motivation: cleaner, better organized, easier to read monitoring during
imaging sessions.

## Scope
- New design token sheet (dark blueprint + night variant), new index.html shell.
- Dashboard: latest image, guiding error summary, frame stats/flags, weather, moon,
  seeing (derived), sequence progression, equipment status.
- Guiding: guide-star image, stats, PHD2 params, RMS + graph, advisor with
  suggest/auto toggle, polar alignment quality.
- Settings: existing config/appearance/health content, restyled.
- Possibly: minimal backend eccentricity computation (separate task) — TBD in plan.

## Out of scope
Mobile layout; new data features; API shape changes.

## Status log
| Date | Status | Note |
|---|---|---|
| 2026-09-08 | planning | Brief written; dual planners round 1 dispatched. |
| 2026-09-08 | planned | Round 1 plan written to docs/plans/ui-rework/plan.md; open questions resolved (see below). |
| 2026-09-08 | in progress | T1 done: theme.css token sheet (dark+night), Google Fonts wired, legacy tokens stripped (smoke-tested under --fake). |
| 2026-09-08 | in progress | T2 done: nav+PAGES slimmed to Dashboard/Guiding/Settings; Imaging/Sequence <main> retired; PA card moved to Guiding; renderers guarded. |
| 2026-09-08 | in progress | T3 done: style.css rewritten on Industry tokens (square corners, hairlines, condensed headings, registration marks); 100dvh no-scroll shell, cards min-height:0, settings scrolls. |
| 2026-09-08 | in progress | T4 done: js/pages/dashboard.js (KPIs + seeing tile, guiding glance, frame stats w/o eccentricity, weather/moon, sequence ring, equipment, viewer+lightbox from imaging.js) + js/widgets.js shared helpers; Dashboard HTML rebuilt to 3-row grid; panels.js trimmed to topbar/guiding/advisor/health. |
| 2026-09-08 | in progress | T5 done: js/pages/guiding.js (star crop + visibility-gated poller, summary tiles, trace graph, params, advisor w/ suggest-auto toggle + revert, TPPA polar). Controls moved out of main.js into the module; panels.js trimmed to topbar + settings health/store. |
| 2026-09-08 | in progress | T6 done: Settings restyle confirmed (tokens propagate via T1/T3); settings grid sizes to content + top-aligns; pref-viewer-width now re-renders the Dashboard viewer (was dangling since T2). |
| 2026-09-08 | in progress | T7 done: 4-corner registration marks on framed media (::before/::after + widgets.decorateMarks injects the other two); night-mode colour-only-status audit clean (device dots keep on/off text). |
| 2026-09-08 | implemented | T8 done: deleted imaging.js; panels.js kept as cross-page (topbar + settings health/store); pruned 6 unused sprite icons (image/skip/reset/expand/menu/star). All modules parse, every icon ref resolves, all assets serve under --fake. |
| 2026-09-08 | implemented | Fix: Settings sections squished/overlapped — flex-column page couldn't constrain its grid child (min-height:auto), so minmax rows squeezed. Settings now display:block page that scrolls itself, block grids at natural height. Browser-verified (headless Edge): Settings no overlaps & page scrolls, Dashboard/Guiding hold no-scroll invariant at 1600x900. |
| 2026-09-08 | implemented | Fix: Settings cross-column gaps above sections — 12-col row grid aligns cards by row, so a card waits for its row's tallest neighbour (row symmetry). Switched settings grids to multi-column masonry (column-width:620px, break-inside:avoid); dynamic config cards now span-12. Browser-verified: zero overlaps, uniform ~19px in-column gaps. |
| 2026-09-08 | implemented | Fix: Settings System cropped + no scroll + gap over Appearance. Root causes: (a) base `.page.is-active{overflow:hidden}` (later in file) beat `#page-settings.is-active{overflow-y:auto}` at equal specificity -> raised to `#page-settings.page.is-active`; (b) `column-width:620px` gave single column -> 560px. Browser-verified at 1600x900 (2 cols) & 1280x800 (1 col): scroll works, System bottom visible, no overlaps. |
| 2026-09-08 | implemented | Fix: guide-star image blank. `.starbox` (flex-column card child, aspect-ratio:1) collapsed to 0-width because an aspect-ratio box in a flex column gets width from content, not from height. Pinned `width:min(260px,100%)` + flex:none. Browser-verified: star paints 258x258 in both dark & night. |
| 2026-09-08 | implemented | Seeing tile relabelled "Guide HFD" (honest: HFD x pixel_scale is a focus/scale product on a coarse 6.45"/px guider, not true sky seeing). |

## Decisions taken on the human's behalf
- **Eccentricity omitted.** No backend `eccentricity` field exists; Dashboard
  shows HFR/stars/ADU/saturation instead. Backend eccentricity computation is
  deferred to a separate ticket (not part of the UI rework).
- **One-screen fit:** keep the 12-col grid but make pages bounded regions —
  `100dvh` shell with `overflow:hidden`, explicit grid rows, cards get
  `min-height:0` and only their internal lists/images scroll. Charts self-size
  via the existing ResizeObserver.
- **Theme:** new `static/theme.css` token sheet ports AstroBlog's Industry
  tokens onto the dark `#0a0e17` ground with `data-theme="dark"` and red
  `data-theme="night"` variants; `style.css` rewritten to consume it. Barlow +
  Barlow Condensed via Google Fonts `<link>` with system fallbacks (self-host
  is a noted TODO, offline-degrades gracefully). uPlot stays vendored.
- **Module structure:** keep `core.js` / `charts.js` / `settings.js`; dissolve
  `imaging.js` into the page modules that now own its images; split `panels.js`
  into `js/pages/dashboard.js` and `js/pages/guiding.js`; slim `main.js`.
- Full plan + ticket breakdown (T1–T8) in docs/plans/ui-rework/plan.md.

## Tickets
- T1–T8 — implementation tickets for the UI rework (see plan.md §4).
- Separate (not this plan): compute per-frame eccentricity in `quality/` and
  surface it on the frame payload + Dashboard stats.
- TODO: self-host Barlow + Barlow Condensed if a fully-offline story is needed.
