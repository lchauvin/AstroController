# AstroController

A web dashboard for monitoring and remotely operating an astrophotography rig
running **N.I.N.A.** and **PHD2**, with an optional LLM advisor that tunes
guiding parameters within hard safety limits and learns which settings work in
which conditions across sessions.

Designed to run on a **separate machine on the LAN** from the observatory PC.

```
  OBSERVATORY PC                    THIS MACHINE                  BROWSER
 ┌──────────────────┐             ┌────────────────────┐        ┌─────────┐
 │ NINA             │◄─REST───────┤ AstroController    │        │         │
 │  + Advanced API  │◄─WS /socket─┤   :3005            │──SSE──►│ phone / │
 │  + TPPA plugin   │◄─WS /tppa───┤                    │        │ laptop  │
 │      :1888       │             │  SQLite: what has  │        │         │
 │ PHD2  :4400      │◄─TCP JSON───┤  worked before     │        └─────────┘
 └──────────────────┘             └────────────────────┘
```

## What it does

**Monitor** — the NINA sequence tree with the running step highlighted;
per-frame HFR, star count, saturation and cloud flags; live PHD2 guiding in
arcseconds; weather and moon for the night; connection health for everything.

**Control** — start/stop/skip/reset the sequence; start/stop/dither/pause
guiding, clear calibration, edit guiding parameters by hand; run polar
alignment with live azimuth/altitude error.

**Advise and adjust** — every minute the advisor decides whether the guiding
parameters are worth changing, and if so applies exactly one change inside
configured bounds. Every change is measured, recorded and revertible.

**Learn** — the conditions and outcome of every change and every stable
stretch are stored, so on later nights the system converges on settings that
already worked rather than re-exploring from scratch.

## Requirements

On the observatory PC:

- **N.I.N.A.** with the [Advanced API](https://github.com/christian-photo/ninaAPI)
  plugin (install it from NINA's plugin manager). Default port 1888.
- **PHD2** with **Tools → Enable Server** switched on. Port 4400.
- Optional: the **TPPA** plugin (≥ 2.2.4.1) for the polar alignment panel.
  Without it that panel simply stays idle.

On this machine: Python 3.12 (provisioned automatically by `uv`).

## Security — read this before binding to the network

**Neither NINA's Advanced API nor PHD2 has any authentication.** Both listen on
every network interface. Anyone who can reach port 1888 or 4400 can move your
mount.

- Treat the observatory LAN as the trust boundary. **Never port-forward 1888 or
  4400 to the internet.** For access from outside the house use a VPN or
  Tailscale.
- AstroController defaults to `127.0.0.1`. To reach it from your phone, set
  `[server].host = "0.0.0.0"` **and** a token in `ASTROCONTROLLER_TOKEN` — it
  refuses to start on a non-loopback address without one, because the UI can
  stop a running sequence.

## Quick start

```powershell
cd D:\Python\AstroController
copy astrocontroller.toml.example astrocontroller.toml
copy .env.example .env
# edit astrocontroller.toml: set [nina].host and [phd2].host

uv run astrocontroller --check     # verify both are reachable
uv run astrocontroller             # http://localhost:3005
```

To reach it from a phone on the LAN:

```powershell
$env:ASTROCONTROLLER_TOKEN = python -c "import secrets; print(secrets.token_hex(16))"
uv run astrocontroller --host 0.0.0.0
# then open http://<this-machine>:3005/?token=<the token>
```

### Try it without a telescope

```powershell
uv run astrocontroller --fake
```

This runs a simulated NINA and PHD2 in-process: guide steps with drifting
seeing and periodic error, dithers, occasional star loss, a live sequence, and
image statistics. The simulated mount responds to the guiding parameters, so
the advisor has something real to react to. Use it to explore the UI in
daylight — and to reproduce timing and reconnection behaviour, which is
miserable to debug at 2am in a field.

## How the tuning works

The order matters, and the LLM is neither first nor in control:

1. **Gates** — mode, kill switch, PHD2 actually guiding.
2. **Trial upkeep** — close and record any measurement in flight.
3. **Baseline** — if a well-supported prior exists for conditions like
   tonight's, apply it deterministically. *No model call.*
4. **Hold** — if RMS is already near the best ever recorded for these
   conditions, do nothing.
5. **LLM** — only for the frontier: no prior, or an anomaly.
6. **Guardrails** — validate, clamp, apply, open a measurement trial.

On a well-explored night steps 3 and 4 answer everything and the model is never
called. That is deliberate. Guiding RMS is dominated by seeing and wind rather
than by parameters, so a model left to freely correlate telemetry with outcomes
will confidently learn noise. Statistics carry the convergence; the model
handles genuinely new territory. **The feature still works with the LLM
disabled entirely.**

### What stops it doing something stupid

- Bounds are configuration, clamped in code. A proposal far outside its range
  is *rejected*, not clamped — that signals a malfunction, not a preference.
- One measurement trial at a time. With two changes in flight, neither
  before/after comparison means anything.
- **Direction lock**: once a parameter moves one way in a session it may only
  keep moving that way, turning a random walk into a line search that cannot
  oscillate. A measured regression freezes that parameter for the night.
- **Confound rejection**: if the guide star's HFD moved materially between the
  before and after windows, the sky changed rather than the parameter, and the
  result is excluded from learning.
- **Significance**: a difference smaller than the combined standard error of
  the two RMS estimates is "neutral", not an improvement.
- Nothing is measured across a dither, settle, meridian flip, autofocus,
  calibration or star loss.
- Auto-revert on regression, plus "revert last" and "revert all" in the UI.
- The model's entire action space is one `set_algo_param` call on a
  whitelisted parameter. It cannot dither, clear calibration, touch the
  sequence, or move the mount.

### What it learns

The primary unit of evidence is the **epoch** — a stretch during which the
parameters did not change: *"these settings held for 34 minutes in 2.1″ seeing
at 60° altitude and delivered 0.61″ RMS."* Those are dense and cheap. Parameter
changes contribute a complementary ledger of what was tried and what happened,
especially the failures — without those, a model re-proposes the same dead end
every night.

Conditions are bucketed by **seeing, altitude, wind and pier side** only. Moon,
cloud, filter and target are recorded for ranking but kept out of the bucket
key: including them would create thousands of permanently single-sample buckets
and nothing would ever converge. It is also defensible physically — moon and
cloud affect guiding only through guide-star SNR, which is captured directly,
and the imaging filter is out of the guide path.

Everything is keyed by rig fingerprint. `minMove` is in pixels and `aggression`
depends on mount mechanics, so transferring between rigs would be actively
harmful.

## Configuration

All settings live in `astrocontroller.toml` (see
`astrocontroller.toml.example`, which documents every key). Secrets live in
`.env`. Unknown keys are rejected at startup.

The LLM is selected with a `provider/model-id` string:

| provider | example | key |
|---|---|---|
| `ollama` | `ollama/llama3.1:8b` | none |
| `openrouter` | `openrouter/anthropic/claude-sonnet-5` | `OPENROUTER_API_KEY` |
| `openai` | `openai/gpt-4o-mini` | `OPENAI_API_KEY` |
| `anthropic` | `anthropic/claude-opus-5` | `ANTHROPIC_API_KEY` |

Install the optional extras you need:

```powershell
uv sync --extra llm        # ollama / openrouter / openai
uv sync --extra anthropic
uv sync --extra sky        # precise moon altitude and separation
uv sync --extra fits       # deep FITS analysis, needs [images].share_path
```

Without the `sky` extra the moon phase falls back to a low-precision
calculation and the panel says so.

## Suggested rollout

1. Run in `mode = "suggest"` for a few nights and read the advisor log against
   the guiding graph. Nothing is applied.
2. When the suggestions look sensible, set `mode = "auto"` and arm the switch
   in the UI, starting with `max_changes_per_hour = 2`.
3. Check the audit trail: every change shows before/after RMS and can be
   reverted with one click.

## Development

```powershell
uv run pytest              # unit tests, no hardware
uv run astrocontroller --fake
```

Tests cover the exclusion algebra and RMS windowing, PHD2 line framing and RPC
correlation (against a fake server speaking the real protocol), sequence-tree
flattening, the NINA response envelope, every guardrail precondition, and the
HTTP surface including the token guard.

## Layout

```
astrocontroller/
  config.py          strict TOML loading; unknown keys are errors
  runtime.py         owns every background task and the shared state
  supervise.py       restart-with-backoff for long-lived tasks
  hub.py             current state + bounded, lossy SSE fan-out
  fake.py            simulated NINA and PHD2 for --fake
  nina/              REST client, websocket listener, sequence tree, TPPA
  phd2/              asyncio client multiplexing events and id-matched RPC
  metrics/           guide buffer, exclusion intervals, trials, conditions
  quality/           per-frame flags from NINA's own statistics
  weather/           Open-Meteo forecast, locally computed moon
  learning/          SQLite store, retrieval, deterministic baseline
  advisor/           guardrails, actuator, LLM providers, prompt, policy
  server/            FastAPI app + vanilla-JS UI (no build step)
```
