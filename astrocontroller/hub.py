"""
TelemetryHub -- the authoritative in-memory state and the SSE fan-out.

Two problems this solves:

* **A slow client must never stall a producer.** The PHD2 reader and the NINA
  websocket push into per-subscriber bounded queues. When a queue fills (a
  phone on bad wifi, a laptop asleep), the queue is drained and replaced with a
  single `resync` message: the client re-fetches `/api/state` and is
  immediately correct again. Producers never block and queues never grow.

* **A dropped message must never leave the UI silently wrong.** Clients get a
  full snapshot on connect and after every resync, so no client depends on
  having seen a continuous history.

High-rate topics are coalesced: guide steps arrive every 1-3s but the derived
graph payload only needs to reach the browser a few times a second.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

QUEUE_MAXSIZE = 64


@dataclass(eq=False)
class Subscriber:
    """
    One connected SSE client.

    `eq=False` keeps the default identity hash: subscribers are held in a set
    and every connection is distinct, so value equality would be both wrong and
    (since a dataclass with `eq=True` sets `__hash__ = None`) unusable.
    """

    queue: asyncio.Queue
    dropped: int = 0
    created_mono: float = field(default_factory=time.monotonic)


class TelemetryHub:
    """Current state plus a publish/subscribe bus for the browser."""

    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._coalesced: dict[str, Any] = {}
        self._coalesce_task: Optional[asyncio.Task] = None
        self.state: dict[str, Any] = {
            "nina": {"connected": False},
            "phd2": {"connected": False},
            "sequence": {"available": False, "steps": []},
            "guiding": {},
            "frames": [],
            "preview": {"available": False, "source": None},
            "weather": {},
            "sky": {},
            "advisor": {
                "mode": "off",
                "enabled": False,
                "last_advice": None,
                "changes": [],
                "vetoes": [],
            },
            "tppa": {"running": False},
            "health": [],
            "server_time": None,
        }

    # ── state ──────────────────────────────────────────────────────────

    def update(self, section: str, value: Any, *, publish: bool = True) -> None:
        """Replace a top-level section and notify subscribers."""
        self.state[section] = value
        if publish:
            self.publish(section, value)

    def merge(self, section: str, patch: dict, *, publish: bool = True) -> None:
        """Shallow-merge into a section (for partial updates)."""
        current = self.state.get(section)
        if not isinstance(current, dict):
            current = {}
        current.update(patch)
        self.state[section] = current
        if publish:
            self.publish(section, current)

    def snapshot(self) -> dict:
        self.state["server_time"] = time.time()
        return self.state

    # ── pub/sub ────────────────────────────────────────────────────────

    def subscribe(self) -> Subscriber:
        sub = Subscriber(queue=asyncio.Queue(maxsize=QUEUE_MAXSIZE))
        self._subscribers.add(sub)
        log.debug("SSE subscriber added (%d total)", len(self._subscribers))
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)
        log.debug("SSE subscriber removed (%d left)", len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, topic: str, payload: Any) -> None:
        """Push an update to every subscriber, dropping to resync if needed."""
        if not self._subscribers:
            return
        message = {"type": topic, "data": payload}
        for sub in list(self._subscribers):
            self._offer(sub, message)

    @staticmethod
    def _offer(sub: Subscriber, message: dict) -> None:
        try:
            sub.queue.put_nowait(message)
        except asyncio.QueueFull:
            # The client is too slow to follow the stream. Throw away the
            # backlog and tell it to re-fetch, rather than blocking a producer
            # or letting it silently fall behind.
            _drain(sub.queue)
            sub.dropped += 1
            try:
                sub.queue.put_nowait({"type": "resync", "data": None})
            except asyncio.QueueFull:  # pragma: no cover - just drained
                pass

    def publish_coalesced(self, topic: str, payload: Any) -> None:
        """
        Hold the latest value for `topic`, flushed by the coalescer.

        Used for guide-step-driven payloads: the browser gains nothing from
        more than a few updates per second, and the queue budget is better
        spent on discrete events.
        """
        self._coalesced[topic] = payload

    async def run_coalescer(self, interval: float = 0.25) -> None:
        """Long-lived task: flush coalesced topics at a fixed rate."""
        while True:
            await asyncio.sleep(interval)
            if not self._coalesced:
                continue
            pending, self._coalesced = self._coalesced, {}
            for topic, payload in pending.items():
                self.publish(topic, payload)


def _drain(queue: asyncio.Queue) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def sse_format(message: dict) -> str:
    """Encode one message as a Server-Sent Event frame."""
    return f"data: {json.dumps(message, default=_json_default)}\n\n"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)
