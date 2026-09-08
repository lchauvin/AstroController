"""
Exclusion intervals -- the algebra that makes before/after attribution honest.

Guiding RMS is only meaningful over stretches where nothing was deliberately
disturbing the mount. Dithers, settles, meridian flips, calibration runs,
autofocus and star-loss events all produce large excursions that have nothing
to do with the guiding parameters. If those samples are averaged in, a
parameter change gets credited or blamed for a dither.

Everything here is on the local `time.monotonic()` clock. That is deliberate:
PHD2 event timestamps are seconds since PHD2 started, NINA timestamps come from
a different machine's wall clock, and this process is on a third. Mixing them
for windowing produces subtly wrong exclusions that are painful to debug, so
remote timestamps are kept as metadata only and never used for arithmetic.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# Sentinel for an interval whose end is not yet known (the disturbance is
# still in progress). Comparisons work naturally against float timestamps.
OPEN_END = float("inf")


@dataclass
class Interval:
    start: float
    end: float = OPEN_END
    reason: str = ""

    @property
    def closed(self) -> bool:
        return self.end != OPEN_END

    def overlaps(self, start: float, end: float) -> bool:
        return self.start < end and start < self.end

    def contains(self, t: float) -> bool:
        return self.start <= t < self.end


class IntervalSet:
    """
    A growing set of exclusion intervals, kept sorted by start time.

    Intervals are opened by name so a later event can close the matching one
    (`SettleBegin` -> `SettleDone`), and each carries a trailing guard that is
    added when it closes -- the mount is still recovering for a while after the
    software says the disturbance ended.
    """

    def __init__(self, retain_s: float = 3600.0) -> None:
        self._intervals: list[Interval] = []
        self._open: dict[str, Interval] = {}
        self.retain_s = retain_s

    # ── mutation ───────────────────────────────────────────────────────

    def open(self, reason: str, at: Optional[float] = None) -> Interval:
        """Begin an open-ended exclusion, or extend the existing one."""
        now = time.monotonic() if at is None else at
        existing = self._open.get(reason)
        if existing is not None:
            return existing
        interval = Interval(start=now, end=OPEN_END, reason=reason)
        self._open[reason] = interval
        self._insert(interval)
        return interval

    def close(
        self,
        reason: str,
        at: Optional[float] = None,
        guard_s: float = 0.0,
    ) -> Optional[Interval]:
        """
        End an open exclusion, extending it by `guard_s`.

        Closing an exclusion that was never opened is not an error: events can
        be missed across a reconnect, and a spurious close should be ignored
        rather than crash the reader loop.
        """
        now = time.monotonic() if at is None else at
        interval = self._open.pop(reason, None)
        if interval is None:
            return None
        interval.end = now + guard_s
        return interval

    def add(
        self,
        reason: str,
        start: float,
        duration_s: float,
    ) -> Interval:
        """Add an already-bounded exclusion (e.g. 'dither + 30s')."""
        interval = Interval(start=start, end=start + duration_s, reason=reason)
        self._insert(interval)
        return interval

    def mark(self, reason: str, duration_s: float, at: Optional[float] = None) -> Interval:
        """Exclude a fixed window starting now."""
        now = time.monotonic() if at is None else at
        return self.add(reason, now, duration_s)

    def _insert(self, interval: Interval) -> None:
        self._intervals.append(interval)
        self._intervals.sort(key=lambda i: i.start)

    def close_all(self, at: Optional[float] = None) -> None:
        """
        Close every open exclusion, e.g. on disconnect.

        A disconnect means an unknown gap, so callers should also invalidate
        any open trial rather than trusting samples across the boundary.
        """
        now = time.monotonic() if at is None else at
        for reason in list(self._open):
            self.close(reason, at=now)

    def prune(self, before: Optional[float] = None) -> None:
        """Drop intervals that ended long enough ago to be irrelevant."""
        cutoff = (time.monotonic() if before is None else before) - self.retain_s
        self._intervals = [i for i in self._intervals if i.end >= cutoff]

    # ── queries ────────────────────────────────────────────────────────

    def is_excluded(self, t: float) -> bool:
        return any(i.contains(t) for i in self._intervals)

    def reason_at(self, t: float) -> Optional[str]:
        for i in self._intervals:
            if i.contains(t):
                return i.reason
        return None

    @property
    def open_reasons(self) -> tuple[str, ...]:
        return tuple(sorted(self._open))

    def overlapping(self, start: float, end: float) -> list[Interval]:
        return [i for i in self._intervals if i.overlaps(start, end)]

    def usable_seconds(self, start: float, end: float) -> float:
        """
        Length of [start, end) not covered by any exclusion.

        This is what distinguishes "5 minutes of data" from "5 minutes of
        wall-clock containing a dither and 40 seconds of settling".
        """
        if end <= start:
            return 0.0
        merged = self._merged_within(start, end)
        excluded = sum(hi - lo for lo, hi in merged)
        return max(0.0, (end - start) - excluded)

    def _merged_within(self, start: float, end: float) -> list[tuple[float, float]]:
        """Exclusion spans clipped to [start, end) and merged where they overlap."""
        clipped = sorted(
            (max(i.start, start), min(i.end, end))
            for i in self._intervals
            if i.overlaps(start, end)
        )
        merged: list[tuple[float, float]] = []
        for lo, hi in clipped:
            if hi <= lo:
                continue
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        return merged

    def filter_times(self, times: Iterable[float]) -> list[float]:
        return [t for t in times if not self.is_excluded(t)]

    def __len__(self) -> int:
        return len(self._intervals)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IntervalSet n={len(self._intervals)} open={self.open_reasons}>"


@dataclass
class ExclusionTracker:
    """
    Translates PHD2 and NINA events into exclusion intervals.

    Guard durations come from `TuningConfig` so they are tunable without
    touching this logic.
    """

    settle_guard_s: float = 5.0
    dither_guard_s: float = 30.0
    flip_guard_s: float = 300.0
    star_lost_guard_s: float = 60.0
    param_change_lag_s: float = 30.0
    intervals: IntervalSet = field(default_factory=IntervalSet)

    # ── PHD2 events ────────────────────────────────────────────────────

    def on_phd2_event(self, event: str, at: Optional[float] = None) -> None:
        now = time.monotonic() if at is None else at
        if event in ("SettleBegin", "Settling"):
            self.intervals.open("settling", at=now)
        elif event == "SettleDone":
            self.intervals.close("settling", at=now, guard_s=self.settle_guard_s)
        elif event == "GuidingDithered":
            self.intervals.add("dither", now, self.dither_guard_s)
        elif event == "StartCalibration":
            self.intervals.open("calibrating", at=now)
        elif event in ("CalibrationComplete", "CalibrationFailed"):
            self.intervals.close("calibrating", at=now, guard_s=self.settle_guard_s)
        elif event == "Paused":
            self.intervals.open("paused", at=now)
        elif event == "Resumed":
            self.intervals.close("paused", at=now, guard_s=self.settle_guard_s)
        elif event in ("StarLost", "LockPositionLost"):
            self.intervals.add("star_lost", now, self.star_lost_guard_s)
        elif event in ("GuidingStopped", "LoopingExposuresStopped"):
            self.intervals.open("not_guiding", at=now)
        elif event in ("StartGuiding", "GuideStep"):
            self.intervals.close("not_guiding", at=now)

    # ── NINA events ────────────────────────────────────────────────────

    def on_nina_event(self, event: str, at: Optional[float] = None) -> None:
        now = time.monotonic() if at is None else at
        if event == "MOUNT-BEFORE-FLIP":
            self.intervals.open("meridian_flip", at=now)
        elif event == "MOUNT-AFTER-FLIP":
            self.intervals.close("meridian_flip", at=now, guard_s=self.flip_guard_s)
        elif event == "AUTOFOCUS-STARTING":
            self.intervals.open("autofocus", at=now)
        elif event == "AUTOFOCUS-FINISHED":
            self.intervals.close("autofocus", at=now, guard_s=self.settle_guard_s)

    # ── our own actions ────────────────────────────────────────────────

    def on_param_change(self, at: Optional[float] = None) -> None:
        """A parameter change needs time to take effect before it is judged."""
        now = time.monotonic() if at is None else at
        self.intervals.add("param_change", now, self.param_change_lag_s)

    def on_disconnect(self, at: Optional[float] = None) -> None:
        """
        A disconnect is an unknown gap: close everything and exclude the
        moment itself so windows never span the boundary silently.
        """
        now = time.monotonic() if at is None else at
        self.intervals.close_all(at=now)
        self.intervals.open("disconnected", at=now)

    def on_reconnect(self, at: Optional[float] = None) -> None:
        now = time.monotonic() if at is None else at
        self.intervals.close("disconnected", at=now, guard_s=self.settle_guard_s)

    @property
    def disturbed(self) -> bool:
        """True while any disturbance is still open."""
        return bool(self.intervals.open_reasons)
