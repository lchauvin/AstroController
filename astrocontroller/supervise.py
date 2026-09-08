"""
Task supervision.

NINA and PHD2 *will* restart mid-night, the observatory PC will drop off the
wifi, and Open-Meteo will occasionally time out. Reconnection is a normal code
path here, not an error path: every long-lived task runs under `Supervisor`,
which restarts it with jittered exponential backoff and records its health so
the UI can show what is currently broken.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

TaskFactory = Callable[[], Awaitable[None]]


class Backoff:
    """Exponential backoff with jitter that resets after a healthy run."""

    def __init__(
        self,
        initial: float = 1.0,
        factor: float = 2.0,
        maximum: float = 60.0,
        jitter: float = 0.25,
        reset_after_s: float = 60.0,
    ) -> None:
        self.initial = initial
        self.factor = factor
        self.maximum = maximum
        self.jitter = jitter
        self.reset_after_s = reset_after_s
        self._current = initial

    def next_delay(self) -> float:
        delay = self._current
        self._current = min(self.maximum, self._current * self.factor)
        spread = delay * self.jitter
        return max(0.0, delay + random.uniform(-spread, spread))

    def note_run(self, duration_s: float) -> None:
        """A task that stayed up long enough is treated as recovered."""
        if duration_s >= self.reset_after_s:
            self._current = self.initial

    def reset(self) -> None:
        self._current = self.initial


@dataclass
class TaskHealth:
    name: str
    running: bool = False
    healthy: bool = False
    restarts: int = 0
    last_error: Optional[str] = None
    last_started_mono: Optional[float] = None
    last_failure_mono: Optional[float] = None
    connected_since_mono: Optional[float] = None
    manages_connection: bool = False
    """True for tasks that own a socket and report their own health."""

    def as_dict(self) -> dict:
        now = time.monotonic()
        uptime = (
            now - self.connected_since_mono
            if self.connected_since_mono is not None
            else None
        )
        return {
            "name": self.name,
            "running": self.running,
            "healthy": self.healthy,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "uptime_s": round(uptime, 1) if uptime is not None else None,
        }


@dataclass
class Supervisor:
    """Owns every long-lived background task for the process lifetime."""

    health: dict[str, TaskHealth] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def spawn(
        self,
        name: str,
        factory: TaskFactory,
        *,
        backoff: Optional[Backoff] = None,
        manages_connection: bool = False,
    ) -> asyncio.Task:
        """
        Start `factory()` under supervision. Restarts until cancelled.

        `manages_connection` distinguishes the two kinds of task. A task that
        owns a socket reports its own health via `mark_healthy` once it has
        actually connected; a plain internal loop is healthy simply by virtue
        of running, and would otherwise sit at "down" forever in the UI.
        """
        if name in self._tasks and not self._tasks[name].done():
            raise RuntimeError(f"task {name!r} is already running")
        entry = self.health.setdefault(name, TaskHealth(name=name))
        entry.manages_connection = manages_connection
        task = asyncio.create_task(
            self._run_forever(name, factory, backoff or Backoff(), entry),
            name=f"supervised:{name}",
        )
        self._tasks[name] = task
        return task

    async def _run_forever(
        self,
        name: str,
        factory: TaskFactory,
        backoff: Backoff,
        entry: TaskHealth,
    ) -> None:
        while True:
            started = time.monotonic()
            entry.running = True
            entry.last_started_mono = started
            if not entry.manages_connection:
                entry.healthy = True
                entry.connected_since_mono = started
            try:
                await factory()
                # A clean return is still unexpected for a forever-task.
                log.warning("task %s returned unexpectedly; restarting", name)
                entry.last_error = "task returned unexpectedly"
            except asyncio.CancelledError:
                entry.running = False
                entry.healthy = False
                entry.connected_since_mono = None
                log.info("task %s cancelled", name)
                raise
            except Exception as exc:  # noqa: BLE001 - supervisor is the boundary
                entry.last_error = f"{type(exc).__name__}: {exc}"
                entry.last_failure_mono = time.monotonic()
                log.warning("task %s failed: %s", name, entry.last_error)
                log.debug("task %s traceback", name, exc_info=True)

            entry.running = False
            entry.healthy = False
            entry.connected_since_mono = None
            entry.restarts += 1
            backoff.note_run(time.monotonic() - started)
            delay = backoff.next_delay()
            log.info("restarting task %s in %.1fs", name, delay)
            await asyncio.sleep(delay)

    def mark_healthy(self, name: str) -> None:
        """Called by a task once it has actually connected."""
        entry = self.health.setdefault(name, TaskHealth(name=name))
        if not entry.healthy:
            entry.connected_since_mono = time.monotonic()
        entry.healthy = True
        entry.last_error = None

    def mark_unhealthy(self, name: str, reason: str) -> None:
        entry = self.health.setdefault(name, TaskHealth(name=name))
        entry.healthy = False
        entry.connected_since_mono = None
        entry.last_error = reason

    def snapshot(self) -> list[dict]:
        return [h.as_dict() for h in self.health.values()]

    async def shutdown(self) -> None:
        """Cancel every task and wait for them to unwind."""
        for task in self._tasks.values():
            task.cancel()
        for name, task in self._tasks.items():
            with contextlib.suppress(asyncio.CancelledError):
                await task
            log.debug("task %s stopped", name)
        self._tasks.clear()
