"""
End-to-end actuation: a real Phd2Client against the fake PHD2 server, a real
SQLite store, and the real guardrails.

This is the path that actually writes to a mount, so it is exercised against
the real wire protocol rather than mocks.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from astrocontroller.advisor.actuator import Actuator
from astrocontroller.advisor.guardrails import ProposedChange
from astrocontroller.config import TuningConfig
from astrocontroller.fake import FakePhd2Server, SimState
from astrocontroller.learning.store import LearningStore
from astrocontroller.metrics.conditions import ConditionVector
from astrocontroller.metrics.exclusions import ExclusionTracker
from astrocontroller.metrics.guiding import GuideBuffer
from astrocontroller.metrics.trials import TrialTracker
from astrocontroller.phd2.client import Phd2Client


@pytest.fixture
async def rig(tmp_path):
    """A connected client, a filled guide buffer, and a real store."""
    sim = SimState()
    server = FakePhd2Server(sim)
    port = await server.start()

    client = Phd2Client("127.0.0.1", instance=port - 4400 + 1, rpc_timeout=2.0)
    task = asyncio.create_task(client.run())
    for _ in range(60):
        await asyncio.sleep(0.05)
        if client.state.algo_params:
            break

    # Anchor to the real monotonic clock: measure_before() windows backwards
    # from time.monotonic(), so synthetic timestamps starting at zero would
    # leave the before-window empty.
    now = time.monotonic()
    buffer = GuideBuffer(retain_s=1e9)
    buffer.set_pixel_scale(client.state.pixel_scale or 1.6)
    for i in range(200):
        buffer.add_guide_step(
            {"RADistanceRaw": 0.3, "DECDistanceRaw": 0.3, "SNR": 25,
             "StarMass": 5000, "HFD": 2.0},
            at=now - 280.0 + i * 1.4,
        )

    store = LearningStore(tmp_path / "actuator.db")
    rig_id = store.ensure_rig(mount="Sim", pixel_scale=1.6)
    session_id = store.start_session(rig_id)

    tuning = TuningConfig(mode="auto", enabled=True, min_samples=20,
                          min_stable_s=0.0)
    trials = TrialTracker(min_samples=20, before_window_s=300)
    actuator = Actuator(client, tuning, trials, store=store,
                        session_id=session_id, rig_id=rig_id)
    actuator.capture_baseline(client.state.algo_params)

    exclusions = ExclusionTracker()

    yield {
        "client": client, "buffer": buffer, "store": store,
        "actuator": actuator, "trials": trials, "exclusions": exclusions,
        "sim": sim, "rig_id": rig_id, "session_id": session_id,
    }

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, ConnectionError):
        pass
    await server.stop()
    store.close()


def context(rig, **kw):
    return rig["actuator"].build_context(
        buffer=rig["buffer"], mode="auto", kill_switch=True, **kw
    )


async def test_apply_writes_to_phd2_and_records_the_change(rig):
    actuator, client, store = rig["actuator"], rig["client"], rig["store"]
    client.state.guiding_since_mono = time.monotonic() - 600

    result = await actuator.apply(
        ProposedChange("ra", "aggression", 0.78, "test", "llm"),
        context(rig),
        conditions=ConditionVector(seeing_arcsec=2.0, altitude_deg=60),
        buffer=rig["buffer"],
        exclusions=rig["exclusions"].intervals,
        model_str="ollama/llama3.1:8b",
    )

    assert result.ok, result.veto or result.error
    assert rig["sim"].aggression == pytest.approx(0.80)   # reached the simulator

    rows = store.changes_for(rig["rig_id"], closed_only=False)
    assert len(rows) == 1
    row = rows[0]
    assert row["axis"] == "ra" and row["param"] == "aggression"
    assert row["before_value"] == pytest.approx(0.70)
    assert row["applied_value"] == pytest.approx(0.80)
    assert row["outcome"] == "pending"
    assert row["model_str"] == "ollama/llama3.1:8b"


async def test_requested_and_applied_are_both_recorded_when_trimmed(rig):
    # The step limit substitutes a smaller change than proposed; the store must
    # show what was asked for as well as what happened.
    actuator, client, store = rig["actuator"], rig["client"], rig["store"]
    client.state.guiding_since_mono = time.monotonic() - 600

    result = await actuator.apply(
        ProposedChange("ra", "aggression", 0.98, "big jump", "llm"),
        context(rig),
        conditions=ConditionVector(),
        buffer=rig["buffer"],
        exclusions=rig["exclusions"].intervals,
    )
    assert result.ok
    row = store.changes_for(rig["rig_id"], closed_only=False)[0]
    assert row["requested_value"] == pytest.approx(0.98)
    assert row["applied_value"] == pytest.approx(0.80)


async def test_apply_opens_exactly_one_trial(rig):
    actuator, client = rig["actuator"], rig["client"]
    client.state.guiding_since_mono = time.monotonic() - 600

    await actuator.apply(
        ProposedChange("ra", "aggression", 0.78),
        context(rig),
        conditions=ConditionVector(),
        buffer=rig["buffer"],
        exclusions=rig["exclusions"].intervals,
    )
    assert rig["trials"].busy

    second = await actuator.apply(
        ProposedChange("ra", "minMove", 0.18),
        context(rig),
        conditions=ConditionVector(),
        buffer=rig["buffer"],
        exclusions=rig["exclusions"].intervals,
    )
    assert not second.ok and second.veto.rule == "trial_open"


async def test_a_vetoed_proposal_never_reaches_phd2(rig):
    actuator, client = rig["actuator"], rig["client"]
    client.state.guiding_since_mono = time.monotonic() - 600
    before = rig["sim"].aggression

    result = await actuator.apply(
        ProposedChange("ra", "aggression", 42.0),   # nonsense
        context(rig),
        conditions=ConditionVector(),
        buffer=rig["buffer"],
        exclusions=rig["exclusions"].intervals,
    )
    assert not result.ok and result.veto.rule == "out_of_range"
    assert rig["sim"].aggression == before


async def test_revert_last_restores_the_previous_value(rig):
    actuator, client = rig["actuator"], rig["client"]
    client.state.guiding_since_mono = time.monotonic() - 600

    await actuator.apply(
        ProposedChange("ra", "aggression", 0.78),
        context(rig),
        conditions=ConditionVector(),
        buffer=rig["buffer"],
        exclusions=rig["exclusions"].intervals,
    )
    assert rig["sim"].aggression == pytest.approx(0.80)

    result = await actuator.revert_last()
    assert result.ok
    assert rig["sim"].aggression == pytest.approx(0.70)
    assert actuator.applied[-1].reverted
    assert not rig["trials"].busy   # the measurement is void


async def test_revert_all_restores_the_session_baseline(rig):
    actuator, client, sim = rig["actuator"], rig["client"], rig["sim"]
    client.state.guiding_since_mono = time.monotonic() - 600

    await actuator.apply(
        ProposedChange("ra", "aggression", 0.78),
        context(rig),
        conditions=ConditionVector(),
        buffer=rig["buffer"],
        exclusions=rig["exclusions"].intervals,
    )
    sim.min_move_px = 0.44   # something else drifted too

    result = await actuator.revert_all()
    assert result.ok
    assert sim.aggression == pytest.approx(0.70)
    assert sim.min_move_px == pytest.approx(0.15)


async def test_direction_lock_is_set_by_the_first_move(rig):
    actuator, client = rig["actuator"], rig["client"]
    client.state.guiding_since_mono = time.monotonic() - 600

    await actuator.apply(
        ProposedChange("ra", "aggression", 0.78),
        context(rig),
        conditions=ConditionVector(),
        buffer=rig["buffer"],
        exclusions=rig["exclusions"].intervals,
    )
    assert actuator.direction_lock[("ra", "aggression")] == 1


async def test_readback_is_recorded_as_truth(rig):
    # PHD2 clamps silently, so what it stored is what gets recorded.
    client = rig["client"]
    stored = await client.set_algo_param("ra", "minMove", 0.33)
    assert stored == pytest.approx(0.33)
    assert client.state.algo_params[("ra", "minMove")] == pytest.approx(0.33)
    assert rig["sim"].min_move_px == pytest.approx(0.33)
