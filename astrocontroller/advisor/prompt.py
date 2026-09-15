"""
Prompt construction for the guiding advisor.

The model's entire action space is one `set_algo_param` call on a whitelisted
parameter. It cannot dither, clear calibration, touch the sequence, or move the
mount -- those are not "discouraged in the prompt", they are simply not
reachable from the response schema, and the guardrails re-check the whitelist
independently of anything the model says.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..config import ParamBound
from ..metrics.conditions import ConditionVector
from ..metrics.guiding import RmsStats
from ..learning.retrieval import PromptBudget, RetrievedContext, render_priors

log = logging.getLogger(__name__)


SYSTEM = """\
You tune autoguiding parameters for an amateur astrophotography mount running \
PHD2. You are given the current guiding performance, the observing conditions, \
and a record of what has already been tried on this exact rig.

Guiding RMS is measured in arcseconds; lower is better. Most of the RMS on any \
given night comes from atmospheric seeing and wind, NOT from the parameters. \
Only propose a change when the evidence suggests the parameters are actually \
the limiting factor.

The parameters mean:
- aggression / aggressiveness: fraction of the measured error corrected each \
frame. Too low tracks drift sluggishly; too high overshoots and oscillates.
- hysteresis: how much of the previous correction carries forward. Higher \
smooths noisy seeing but reacts more slowly.
- minMove: deadband in pixels. Corrections smaller than this are skipped. \
Raising it stops the mount chasing seeing; too high lets real drift accumulate.

Two more diagnostics come with every RMS reading:
- oscillation: RA peak-to-RMS ratio. ~1.4 is a pure sine (periodic error), \
~1.7 is random noise, above ~2.2 means the mount is ringing (under-damped). \
Ringing with high RMS calls for LESS aggression or MORE hysteresis, not more \
aggression even though the RMS looks bad.
- RA/Dec correction time: mean guide-pulse duration per frame, in \
milliseconds. Rising alongside RMS with oscillation still low suggests real \
drift is being under-corrected (raise aggression or lower minMove); high \
values that never reduce RMS can mean backlash is eating the pulses.

Rules:
- Propose AT MOST ONE parameter change.
- Prefer no change. "Leave it alone" is usually correct, especially when RMS \
is already near the best previously recorded for these conditions.
- Never repeat anything in the DO NOT RETRY list.
- Stay within the stated bounds and step limit.

Reply with a single JSON object and nothing else, in one of these two forms:
  {"action": "none", "rationale": "<short reason>"}
  {"action": "set", "axis": "ra"|"dec", "param": "<name>", "value": <number>, \
"rationale": "<short reason>"}
The rationale must be under 200 characters."""


def _fmt(value: Optional[float], digits: int = 2, dash: str = "?") -> str:
    if value is None:
        return dash
    return f"{value:.{digits}f}"


def render_bounds(
    bounds: list[ParamBound],
    current: dict[tuple[str, str], float],
    available: dict[str, tuple[str, ...]],
) -> str:
    """Only advertise parameters the current PHD2 algorithms actually expose."""
    lines = ["TUNABLE PARAMETERS (axis param current [min,max] max_step):"]
    for b in bounds:
        exposed = available.get(b.axis)
        if exposed and b.param not in exposed:
            continue
        value = current.get((b.axis, b.param))
        if value is None:
            continue
        lines.append(
            f"  {b.axis} {b.param} {value:g} "
            f"[{b.lo:g},{b.hi:g}] max_step {b.max_delta:g}"
        )
    if len(lines) == 1:
        lines.append("  (none currently available)")
    return "\n".join(lines)


def render_conditions(cond: ConditionVector) -> str:
    parts = [
        f"seeing {_fmt(cond.seeing_arcsec, 2)}\" (guide-star HFD)",
        f"altitude {_fmt(cond.altitude_deg, 0)} deg",
        f"pier {cond.pier_side or '?'}",
        f"wind {_fmt(cond.wind_ms, 1)} m/s",
        f"cloud {_fmt(cond.cloud_pct, 0)}%",
        f"moon {_fmt(cond.moon_illum, 2)} illum",
        f"guide SNR {_fmt(cond.guide_snr, 1)}",
    ]
    if cond.target:
        parts.append(f"target {cond.target}")
    if cond.filter:
        parts.append(f"filter {cond.filter}")
    return "CONDITIONS NOW: " + ", ".join(parts)


def render_performance(stats: Optional[RmsStats]) -> str:
    if stats is None:
        return "GUIDING NOW: no clean samples."
    return (
        f"GUIDING NOW: total {stats.rms_total:.2f}\" "
        f"(RA {stats.rms_ra:.2f}\", Dec {stats.rms_dec:.2f}\"), "
        f"peak RA {stats.peak_ra:.2f}\" Dec {stats.peak_dec:.2f}\", "
        f"n={stats.n} over {stats.usable_seconds:.0f}s clean, "
        f"+/-{stats.se_total:.3f}\" standard error, "
        f"oscillation {stats.ra_oscillation:.2f}, "
        f"RA corr {stats.ra_corr_ms:.0f}ms Dec corr {stats.dec_corr_ms:.0f}ms"
    )


def build_prompt(
    *,
    conditions: ConditionVector,
    stats: Optional[RmsStats],
    bounds: list[ParamBound],
    current: dict[tuple[str, str], float],
    available: dict[str, tuple[str, ...]],
    context: RetrievedContext,
    budget: PromptBudget,
    reason: str = "",
) -> str:
    """Assemble the user message. Kept compact for small local models."""
    sections = [
        render_conditions(conditions),
        render_performance(stats),
        "",
        render_bounds(bounds, current, available),
        "",
        render_priors(context, budget),
    ]
    if reason:
        sections.extend(["", f"WHY YOU ARE BEING ASKED: {reason}"])
    sections.extend(["", "Propose at most one change, or none."])
    return "\n".join(sections)


def parse_proposal(parsed: Optional[dict]) -> tuple[Optional[dict], Optional[str]]:
    """
    Validate a parsed model reply.

    Returns `(proposal, None)` for a usable change, `(None, None)` for an
    explicit no-op, or `(None, error)` when the reply is malformed. A malformed
    reply is always a no-op -- never a retry loop.
    """
    if not isinstance(parsed, dict):
        return None, "response was not a JSON object"

    action = str(parsed.get("action", "")).strip().lower()
    if action in ("none", "no_action", "hold", ""):
        return None, None
    if action != "set":
        return None, f"unknown action {action!r}"

    axis = str(parsed.get("axis", "")).strip().lower()
    param = str(parsed.get("param", "")).strip()
    if axis not in ("ra", "dec"):
        return None, f"invalid axis {axis!r}"
    if not param:
        return None, "missing param"

    try:
        value = float(parsed.get("value"))
    except (TypeError, ValueError):
        return None, f"non-numeric value {parsed.get('value')!r}"

    rationale = str(parsed.get("rationale", "") or "")[:240]
    return {"axis": axis, "param": param, "value": value, "rationale": rationale}, None
