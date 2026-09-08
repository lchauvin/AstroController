"""
The telemetry hub: state fan-out, and what happens when a client falls behind.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from astrocontroller.hub import QUEUE_MAXSIZE, TelemetryHub, sse_format


def test_subscribers_are_distinct_even_when_identical():
    # Subscribers live in a set; a dataclass with value equality would make
    # two fresh subscribers collide (and would not be hashable at all).
    hub = TelemetryHub()
    a, b = hub.subscribe(), hub.subscribe()
    assert a is not b
    assert hub.subscriber_count == 2
    hub.unsubscribe(a)
    assert hub.subscriber_count == 1


def test_publish_reaches_every_subscriber():
    hub = TelemetryHub()
    a, b = hub.subscribe(), hub.subscribe()
    hub.publish("guiding", {"rms": 0.5})
    assert a.queue.get_nowait() == {"type": "guiding", "data": {"rms": 0.5}}
    assert b.queue.get_nowait() == {"type": "guiding", "data": {"rms": 0.5}}


def test_update_stores_state_and_publishes():
    hub = TelemetryHub()
    sub = hub.subscribe()
    hub.update("phd2", {"connected": True})
    assert hub.state["phd2"] == {"connected": True}
    assert sub.queue.get_nowait()["type"] == "phd2"


def test_merge_patches_without_replacing():
    hub = TelemetryHub()
    hub.update("nina", {"connected": True, "equipment": {}})
    hub.merge("nina", {"error": "boom"})
    assert hub.state["nina"] == {"connected": True, "equipment": {}, "error": "boom"}


def test_a_slow_client_gets_a_resync_rather_than_stalling_producers():
    # A phone on bad wifi must never block the PHD2 reader, and must never be
    # left silently out of date.
    hub = TelemetryHub()
    sub = hub.subscribe()
    for i in range(QUEUE_MAXSIZE + 10):
        hub.publish("guiding", {"n": i})

    assert sub.dropped == 1
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait())
    # The backlog is replaced by a single resync instruction.
    assert drained[0] == {"type": "resync", "data": None}


def test_publishing_with_no_subscribers_is_harmless():
    hub = TelemetryHub()
    hub.publish("guiding", {"rms": 0.5})   # must not raise


async def test_coalescer_collapses_high_rate_topics():
    # Guide steps arrive every 1-3s but the browser gains nothing from more
    # than a few updates a second.
    hub = TelemetryHub()
    sub = hub.subscribe()
    task = asyncio.create_task(hub.run_coalescer(interval=0.05))
    try:
        for i in range(20):
            hub.publish_coalesced("guiding", {"n": i})
        await asyncio.sleep(0.15)
        messages = []
        while not sub.queue.empty():
            messages.append(sub.queue.get_nowait())
        assert len(messages) == 1              # only the latest survives
        assert messages[0]["data"] == {"n": 19}
    finally:
        task.cancel()


def test_sse_frames_are_well_formed():
    frame = sse_format({"type": "guiding", "data": {"rms": 0.5}})
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert json.loads(frame[6:].strip())["type"] == "guiding"


def test_sse_encodes_objects_that_know_how_to_serialise_themselves():
    class Thing:
        def as_dict(self):
            return {"ok": True}

    assert json.loads(sse_format({"type": "x", "data": Thing()})[6:])["data"] == {
        "ok": True
    }


def test_snapshot_has_every_panel_the_ui_expects():
    hub = TelemetryHub()
    snapshot = hub.snapshot()
    for key in ("nina", "phd2", "sequence", "guiding", "frames",
                "weather", "sky", "advisor", "tppa", "health"):
        assert key in snapshot
    assert snapshot["server_time"] is not None
