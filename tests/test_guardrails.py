"""
The safety boundary for automatic parameter changes.

Every precondition gets a test that isolates it: the context starts in a state
where a change is allowed, then exactly one thing is broken.
"""

from __future__ import annotations

import time

import pytest

from astrocontroller.config import ParamBound, TuningConfig
from astrocontroller.advisor.guardrails import (
    GuardContext,
    ProposedChange,
    check,
    resolve_bound,
)
from astrocontroller.metrics.guiding import GuideBuffer
from astrocontroller.metrics.trials import TrialTracker
from astrocontroller.phd2.client import Phd2State

NOW = 10_000.0


def make_ctx(**overrides) -> GuardContext:
    """A context in which a change is permitted."""
    state = Phd2State(
        connected=True,
        app_state="Guiding",
        pixel_scale=1.6,
        algo_params={("ra", "aggression"): 0.70, ("ra", "minMove"): 0.15},
        available_params={"ra": ("minMove", "hysteresis", "aggression")},
        last_rpc_ok_mono=NOW - 1.0,
        guiding_since_mono=NOW - 600.0,
    )
    for key, value in overrides.pop("state", {}).items():
        setattr(state, key, value)

    buffer = GuideBuffer()
    buffer.set_pixel_scale(1.6)
    for i in range(80):
        buffer.add_guide_step(
            {"RADistanceRaw": 0.3, "DECDistanceRaw": 0.3, "SNR": 25,
             "StarMass": 5000, "HFD": 3.0},
            at=NOW - 300 + i * 2,
        )

    ctx = GuardContext(
        state=state,
        tuning=overrides.pop("tuning", TuningConfig()),
        buffer=buffer,
        trials=overrides.pop("trials", TrialTracker()),
        kill_switch=True,
        mode="auto",
        baseline_snapshot={"ra.aggression": 0.70},
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def propose(value: float = 0.78, axis: str = "ra", param: str = "aggression"):
    return ProposedChange(axis=axis, param=param, value=value)


def test_a_reasonable_change_passes():
    value, veto = check(make_ctx(), propose(0.78), now=NOW)
    assert veto is None
    assert value == pytest.approx(0.80)  # quantised to the 0.05 step


# ── switches and mode ──────────────────────────────────────────────────


def test_suggest_mode_never_applies():
    _, veto = check(make_ctx(mode="suggest"), propose(), now=NOW)
    assert veto.rule == "mode"


def test_kill_switch_off_blocks_everything():
    _, veto = check(make_ctx(kill_switch=False), propose(), now=NOW)
    assert veto.rule == "kill_switch"


# ── PHD2 condition ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state_patch, expected",
    [
        ({"connected": False}, "phd2_disconnected"),
        ({"last_rpc_ok_mono": NOW - 120.0}, "phd2_stale"),
        ({"app_state": "Looping"}, "not_guiding"),
        ({"paused": True}, "paused"),
        ({"calibrating": True}, "calibrating"),
        ({"settling": True}, "settling"),
        ({"settle_done_mono": NOW - 1.0}, "settle_guard"),
        ({"last_dither_mono": NOW - 5.0}, "dither_guard"),
        ({"last_star_lost_mono": NOW - 10.0}, "star_lost"),
        ({"guiding_since_mono": NOW - 10.0}, "not_stable_yet"),
    ],
)
def test_phd2_state_preconditions(state_patch, expected):
    _, veto = check(make_ctx(state=state_patch), propose(), now=NOW)
    assert veto is not None and veto.rule == expected


def test_meridian_flip_blocks_changes():
    _, veto = check(make_ctx(nina_flipping=True), propose(), now=NOW)
    assert veto.rule == "meridian_flip"


def test_autofocus_blocks_changes():
    _, veto = check(make_ctx(nina_autofocusing=True), propose(), now=NOW)
    assert veto.rule == "autofocus"


def test_open_exclusion_blocks_changes():
    _, veto = check(make_ctx(exclusions_open=("dither",)), propose(), now=NOW)
    assert veto.rule == "disturbed"


# ── parameter whitelist ────────────────────────────────────────────────


def test_unknown_parameter_is_refused():
    _, veto = check(make_ctx(), propose(1.0, param="fastSwitch"), now=NOW)
    assert veto.rule == "unknown_param"


def test_parameter_not_exposed_by_the_current_algorithm_is_refused():
    # Lowpass2 exposes 'aggressiveness', not 'aggression'. Writing a parameter
    # the live algorithm does not expose is either a no-op or a surprise.
    ctx = make_ctx(state={"available_params": {"ra": ("minMove", "aggressiveness")}})
    _, veto = check(ctx, propose(0.78), now=NOW)
    assert veto.rule == "param_unavailable"


def test_frozen_parameter_is_refused():
    ctx = make_ctx(frozen_params=frozenset({("ra", "aggression")}))
    _, veto = check(ctx, propose(0.78), now=NOW)
    assert veto.rule == "frozen"


# ── value sanity ───────────────────────────────────────────────────────


def test_wildly_out_of_range_value_is_rejected_not_clamped():
    # A model asking for 8.0 on a 0.4-1.0 scale has misunderstood something;
    # silently clamping to 1.0 would hide the malfunction.
    _, veto = check(make_ctx(), propose(8.0), now=NOW)
    assert veto.rule == "out_of_range"


def test_slightly_out_of_range_value_is_clamped_into_range():
    # 1.05 clamps to the 1.00 ceiling, then the step limit trims it to 0.80.
    value, veto = check(make_ctx(), propose(1.05), now=NOW)
    assert veto is None
    assert value == pytest.approx(0.80)


def test_step_larger_than_max_delta_is_trimmed_not_refused():
    # aggression max_delta is 0.10, so 0.70 -> 0.95 becomes 0.70 -> 0.80.
    # Refusing would be a dead end: the model has no memory between ticks and
    # would propose the same jump forever.
    value, veto = check(make_ctx(), propose(0.95), now=NOW)
    assert veto is None
    assert value == pytest.approx(0.80)


def test_trimming_respects_the_direction_lock():
    ctx = make_ctx(direction_lock={("ra", "aggression"): -1})
    _, veto = check(ctx, propose(0.95), now=NOW)
    assert veto.rule == "direction_lock"


def test_no_op_change_is_refused():
    _, veto = check(make_ctx(), propose(0.70), now=NOW)
    assert veto.rule == "no_change"


def test_unknown_current_value_is_refused():
    ctx = make_ctx(state={"algo_params": {}})
    _, veto = check(ctx, propose(0.78), now=NOW)
    assert veto.rule == "no_current_value"


# ── anti-oscillation ───────────────────────────────────────────────────


def test_direction_lock_prevents_reversing_within_a_session():
    # Once aggression has moved up this session it may only keep moving up.
    # This turns a random walk into a 1-D line search, which cannot oscillate.
    ctx = make_ctx(direction_lock={("ra", "aggression"): 1})
    _, veto = check(ctx, propose(0.62), now=NOW)
    assert veto.rule == "direction_lock"

    value, veto = check(ctx, propose(0.78), now=NOW)
    assert veto is None and value == pytest.approx(0.80)


def test_only_one_trial_may_be_open():
    # With two changes in flight neither before/after comparison means
    # anything, so the second is refused.
    trials = TrialTracker()
    ctx = make_ctx(trials=trials)
    before = trials.measure_before(ctx.buffer, __import__(
        "astrocontroller.metrics.exclusions", fromlist=["IntervalSet"]
    ).IntervalSet(), at=NOW)
    trials.open(
        change_id=1, axis="ra", param="minMove",
        before_value=0.15, after_value=0.18,
        before=before, conditions=__import__(
            "astrocontroller.metrics.conditions", fromlist=["ConditionVector"]
        ).ConditionVector(), at=NOW,
    )
    _, veto = check(ctx, propose(0.78), now=NOW)
    assert veto.rule == "trial_open"


# ── budgets ────────────────────────────────────────────────────────────


def test_per_parameter_cooldown():
    ctx = make_ctx(last_param_change_mono={("ra", "aggression"): NOW - 60.0})
    _, veto = check(ctx, propose(0.78), now=NOW)
    assert veto.rule == "cooldown"


def test_global_cooldown_blocks_shotgunning_other_parameters():
    ctx = make_ctx(last_change_mono=NOW - 30.0)
    _, veto = check(ctx, propose(0.78), now=NOW)
    assert veto.rule == "global_cooldown"


def test_hourly_and_session_budgets():
    _, veto = check(make_ctx(changes_this_hour=4), propose(), now=NOW)
    assert veto.rule == "hourly_budget"
    _, veto = check(make_ctx(changes_this_session=12), propose(), now=NOW)
    assert veto.rule == "session_budget"


def test_missing_baseline_snapshot_blocks_changes():
    ctx = make_ctx()
    ctx.baseline_snapshot = None
    _, veto = check(ctx, propose(0.78), now=NOW)
    assert veto.rule == "no_baseline"


# ── bounds resolution ──────────────────────────────────────────────────


def test_arcsec_bounds_convert_with_pixel_scale():
    # minMove is configured in arcsec so the same config works on any scope.
    tuning = TuningConfig(bounds=(
        ParamBound("ra", "minMove", 0.05, 0.50, 0.05, 0.01, 600.0,
                   lo_arcsec=0.10, hi_arcsec=0.70),
    ))
    bound = resolve_bound(tuning, "ra", "minMove", pixel_scale=2.0)
    assert bound.lo == pytest.approx(0.05)   # 0.10" / 2.0 "/px
    assert bound.hi == pytest.approx(0.35)   # 0.70" / 2.0 "/px

    coarse = resolve_bound(tuning, "ra", "minMove", pixel_scale=1.0)
    assert coarse.hi == pytest.approx(0.70)


def test_bounds_fall_back_to_pixels_without_a_pixel_scale():
    tuning = TuningConfig()
    bound = resolve_bound(tuning, "ra", "minMove", pixel_scale=None)
    assert bound.lo == pytest.approx(0.05) and bound.hi == pytest.approx(0.50)
