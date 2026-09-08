"""
The safety boundary for automatic parameter changes.

Nothing reaches PHD2 without passing every precondition here. The checks are
ordered cheapest-first and each returns a named veto so the UI can say exactly
why nothing happened -- "the advisor is quiet" and "the advisor is blocked"
must never look the same to the user at 2am.

Design notes worth stating:

* Bounds are **configuration, not model output**, and are clamped in code
  regardless of what the config or the model says.
* A proposal far outside its range is *rejected*, not clamped. A model asking
  for aggression 8.0 on a 0.4-1.0 scale has misunderstood something, and
  quietly turning that into 1.0 would hide the malfunction.
* The parameter must be present in PHD2's live `get_algo_param_names` for the
  axis. Writing a parameter the current algorithm does not expose is either a
  no-op or a surprise; neither is acceptable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..config import ParamBound, TuningConfig
from ..metrics.guiding import GuideBuffer
from ..metrics.trials import TrialTracker
from ..phd2.client import Phd2State

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Veto:
    """A refusal, with enough detail to display."""

    rule: str
    detail: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "detail": self.detail}


@dataclass
class ProposedChange:
    axis: str
    param: str
    value: float
    rationale: str = ""
    source: str = "llm"


@dataclass
class GuardContext:
    """Everything the preconditions need, gathered by the advisor loop."""

    state: Phd2State
    tuning: TuningConfig
    buffer: GuideBuffer
    trials: TrialTracker
    exclusions_open: tuple[str, ...] = ()
    kill_switch: bool = False
    """True when auto-apply is enabled by the user."""
    mode: str = "suggest"
    changes_this_hour: int = 0
    changes_this_session: int = 0
    last_change_mono: Optional[float] = None
    last_param_change_mono: dict[tuple[str, str], float] = None  # type: ignore[assignment]
    frozen_params: frozenset[tuple[str, str]] = frozenset()
    direction_lock: dict[tuple[str, str], int] = None  # type: ignore[assignment]
    nina_flipping: bool = False
    nina_autofocusing: bool = False
    baseline_snapshot: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.last_param_change_mono is None:
            self.last_param_change_mono = {}
        if self.direction_lock is None:
            self.direction_lock = {}


def resolve_bound(
    tuning: TuningConfig,
    axis: str,
    param: str,
    pixel_scale: Optional[float],
) -> Optional[ParamBound]:
    """Find the configured bound, with arcsec limits converted to pixels."""
    for bound in tuning.bounds:
        if bound.axis == axis and bound.param == param:
            return bound.resolve(pixel_scale)
    return None


def check(
    ctx: GuardContext,
    proposal: ProposedChange,
    *,
    now: Optional[float] = None,
) -> tuple[Optional[float], Optional[Veto]]:
    """
    Validate a proposal.

    Returns `(value_to_apply, None)` when it passes, or `(None, Veto)` when it
    does not. The returned value is clamped and quantised, so it may differ
    slightly from what was proposed.
    """
    now = time.monotonic() if now is None else now
    tuning = ctx.tuning
    state = ctx.state
    key = (proposal.axis, proposal.param)

    # 1-2: is automatic tuning switched on at all?
    if ctx.mode != "auto":
        return None, Veto("mode", f"tuning mode is {ctx.mode!r}, not 'auto'")
    if not ctx.kill_switch:
        return None, Veto("kill_switch", "auto-apply is switched off")

    # 3-5: is PHD2 in a state where a change is meaningful?
    if not state.connected:
        return None, Veto("phd2_disconnected", "no connection to PHD2")
    if state.last_rpc_ok_mono is None or now - state.last_rpc_ok_mono > 30.0:
        return None, Veto("phd2_stale", "no successful PHD2 call in the last 30s")
    if state.app_state != "Guiding":
        return None, Veto("not_guiding", f"PHD2 state is {state.app_state!r}")
    if state.paused:
        return None, Veto("paused", "guiding is paused")
    if state.calibrating:
        return None, Veto("calibrating", "calibration in progress")

    # 6-10: is the mount currently being disturbed?
    if state.settling:
        return None, Veto("settling", "still settling")
    if state.settle_done_mono and now - state.settle_done_mono < tuning.settle_guard_s:
        return None, Veto("settle_guard", "within the post-settle guard")
    if state.last_dither_mono and now - state.last_dither_mono < tuning.dither_guard_s:
        return None, Veto("dither_guard", "within the post-dither guard")
    if ctx.nina_flipping:
        return None, Veto("meridian_flip", "meridian flip in progress")
    if ctx.nina_autofocusing:
        return None, Veto("autofocus", "autofocus in progress")
    if (
        state.last_star_lost_mono
        and now - state.last_star_lost_mono < tuning.star_lost_guard_s
    ):
        return None, Veto("star_lost", "guide star was lost recently")
    if ctx.exclusions_open:
        return None, Veto(
            "disturbed", f"active disturbance: {', '.join(ctx.exclusions_open)}"
        )

    # 11: is this parameter one we are allowed to touch, on this algorithm?
    bound = resolve_bound(tuning, proposal.axis, proposal.param, state.pixel_scale)
    if bound is None:
        return None, Veto(
            "unknown_param", f"{proposal.axis}.{proposal.param} is not tunable"
        )
    available = state.available_params.get(proposal.axis, ())
    if available and proposal.param not in available:
        return None, Veto(
            "param_unavailable",
            f"{proposal.param} is not exposed by the current {proposal.axis} algorithm",
        )
    if key in ctx.frozen_params:
        return None, Veto("frozen", "parameter frozen for this session after a regression")

    # 12: sane request? Reject wild values rather than silently clamping them.
    span = bound.hi - bound.lo
    if proposal.value < bound.lo - span or proposal.value > bound.hi + span:
        return None, Veto(
            "out_of_range",
            f"{proposal.value:.3f} is far outside [{bound.lo:.3f}, {bound.hi:.3f}]",
        )

    current = state.algo_params.get(key)
    if current is None:
        return None, Veto("no_current_value", f"current {proposal.axis}.{proposal.param} unknown")

    target = bound.quantize(bound.clamp(proposal.value))
    delta = target - current

    # 13: an over-large step is trimmed to the limit rather than refused.
    # Refusing would be a dead end: the model has no memory between ticks, so
    # it would propose the same large jump forever and nothing would ever be
    # tuned. Trimming keeps the model's intended direction while enforcing the
    # safety limit, and the store records requested and applied separately so
    # the substitution is visible rather than silent.
    if abs(delta) > bound.max_delta + 1e-9:
        target = bound.quantize(current + bound.max_delta * (1 if delta > 0 else -1))
        target = bound.clamp(target)
        delta = target - current
        log.info(
            "trimmed %s.%s step to the %.3f limit: applying %.3f",
            proposal.axis, proposal.param, bound.max_delta, target,
        )

    # 14: after trimming, is anything left to do?
    if abs(delta) < bound.quantum:
        return None, Veto("no_change", "proposed value equals the current value")

    # Direction lock: once a parameter starts moving one way this session, it
    # may only continue that way. This turns a random walk into a 1-D line
    # search, which cannot oscillate.
    locked = ctx.direction_lock.get(key)
    if locked is not None and locked * delta < 0:
        return None, Veto(
            "direction_lock",
            f"{proposal.param} is locked to move "
            f"{'up' if locked > 0 else 'down'} this session",
        )

    # 15: exactly one trial in flight, or nothing can be attributed.
    # Checked before the budget rules because it is the more informative
    # answer to "why is nothing happening?" -- a cooldown will usually still be
    # running too, and "still measuring the last change" is what the user
    # actually wants to read.
    if ctx.trials.busy:
        return None, Veto("trial_open", "a previous change is still being measured")

    # 16-18: budget.
    last = ctx.last_param_change_mono.get(key)
    if last is not None and now - last < bound.cooldown_s:
        remaining = bound.cooldown_s - (now - last)
        return None, Veto("cooldown", f"{remaining:.0f}s left on this parameter")
    if (
        ctx.last_change_mono is not None
        and now - ctx.last_change_mono < tuning.global_cooldown_s
    ):
        remaining = tuning.global_cooldown_s - (now - ctx.last_change_mono)
        return None, Veto("global_cooldown", f"{remaining:.0f}s left before any change")
    if ctx.changes_this_hour >= tuning.max_changes_per_hour:
        return None, Veto("hourly_budget", f"{ctx.changes_this_hour} changes this hour")
    if ctx.changes_this_session >= tuning.max_changes_per_session:
        return None, Veto(
            "session_budget", f"{ctx.changes_this_session} changes this session"
        )


    # 19-20: is there enough stable data to judge the result later?
    if ctx.baseline_snapshot is None:
        return None, Veto("no_baseline", "session baseline parameters not captured")
    if (
        state.guiding_since_mono is None
        or now - state.guiding_since_mono < tuning.min_stable_s
    ):
        return None, Veto(
            "not_stable_yet",
            f"guiding for less than {tuning.min_stable_s:.0f}s",
        )
    if len(ctx.buffer) < tuning.min_samples:
        return None, Veto(
            "insufficient_data",
            f"only {len(ctx.buffer)} guide samples buffered",
        )

    return target, None
