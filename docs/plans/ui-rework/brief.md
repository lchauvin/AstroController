# UI/UX Rework — Blueprint Theme

## Goal
Complete rework of the AstroController web UI to three pages, styled after the
sibling project AstroBlog ("Industry" engineering-blueprint design system), adapted
to a **dark ground** plus a red night-vision toggle. Each page should fit on one
screen (desktop) without scrolling.

## Human rulings (2026-09-08)
- Theme: **dark blueprint** — AstroBlog's aesthetic (hairline borders, square corners,
  mono micro-labels, condensed uppercase tracked headings, `+` registration marks on
  framed media) re-based on a dark ground, with a red night-vision toggle retained.
- Pages: exactly **Dashboard (monitoring)**, **Guiding**, **Settings**. Imaging and
  Sequence pages are removed; their content folds into Dashboard (latest image + stats,
  sequence progression compact panel).
- Layouts must fit in one viewport on desktop; no scrolling. Mobile layout explicitly
  out of scope.

## Page 1 — Dashboard (monitoring)
All of: latest image (with viewer), guiding error rate summary, latest-image stats
(saturation flag, eccentricity if available, HFR, stars, ADU stats), weather for the
night, moon phase, seeing (derive: guide-star HFD × pixel scale if no direct value),
sequence + current step + progression, equipment list with status (camera temp,
mount tracking, current filter, etc.).

## Page 2 — Guiding
Guiding image (guide-star crop), image stats (HFD/HDY... "HDR" per user — likely HFD,
star count/SNR), current PHD2 guiding parameters, guiding error / RMS + graph,
advisor advice panel with a toggle (suggest-only vs auto-apply — maps to existing
`/api/advisor/tuning` mode), polar alignment quality (TPPA errors).

## Page 3 — Settings
Existing settings functionality (config form, appearance, health, learning store),
restyled.

## What exists (from exploration)
- `astrocontroller/server/static/` — vanilla ES modules, uPlot vendored, single
  index.html with 5 pages toggled by nav, SSE `/api/stream` + `/api/state` snapshot.
- All required data already available in state: frames[0] stats+flags
  (saturation, HFR, stars — **no eccentricity field exists**; planner must decide
  show-if-available vs compute), guiding (rms, graph 600s, disturbed), weather,
  sky (moon), sequence (steps, current_id, done/total), nina.devices (rich detail),
  phd2, advisor (mode/changes/vetoes/last), tppa (Az/Alt/Total error), health.
- Seeing is derivable client-side: `guiding.rms.hfd_med * phd2.pixel_scale`.
- AstroBlog tokens (from D:\Python\AstroBlog\src\styles\tokens.css):
  accent steel blue #5980a6, Barlow + Barlow Condensed + ui-monospace, hairline 1px
  borders `color-mix(in srgb, ink 16%, transparent)`, square corners (radius ≤7px
  tokens but unrounded by design), `+` registration marks component, `--page-max:1280px`,
  spacing scale 3.4/6.8/10.2/13.6/20.4/27.2 px, near-black mount #0c0e11 behind images,
  transitions 140ms ease-out, focus-visible 2px accent outline.
  Dark ground: replace #f2f2f3/#1d1f20 with dark equivalents (existing UI dark
  #0a0e17 is a reasonable ground), keep hairline/typography/marks language.
  Do NOT copy `--color-border`/`--color-text-dim` names (undefined upstream).

## Constraints
- No build step; keep vanilla ES modules + vendored uPlot. Fonts: Barlow/Barlow
  Condensed via Google Fonts (self-host or link — planner to decide offline story).
- No backend changes intended (all data exists). If a field is genuinely missing
  (e.g. eccentricity), plan the minimal backend addition separately.
- Keep SSE-driven updates structure (core.js) unless plan argues otherwise.
- Tests: repo has pytest for backend; frontend has no test harness — manual smoke
  acceptable, note it in plan.

## Out of scope
- Mobile/responsive layout (desktop single-screen only).
- Any new data acquisition features.
- Changing backend API shapes.

## Open questions the planners must resolve
1. Eccentricity: omit, or add backend computation in quality/? (separate task area
   if added).
2. One-screen fit strategy: grid layout approach for both pages.
3. Theme architecture: token sheet ported from AstroBlog, dark + night-red variants.
4. File/module structure of the new static/ (reuse core.js/charts.js? rewrite panels?).

## Size: L
Trigger: restructuring the whole UI surface (pages removed, content re-mapped) plus a
new design-token theme system — architectural decision density high, affects every
frontend file. Dual planners.

Mode: dual. Cap: 3 rounds.
