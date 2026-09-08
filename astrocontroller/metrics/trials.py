"""
Trial tracking: deciding whether a parameter change actually helped.

Every applied change opens exactly one trial. The `before` window is computed
retrospectively from the already-full guide buffer at the moment of the change
(nothing has to be armed in advance), and the `after` window accumulates once
the change has had time to take effect.

Two guards stop this from fooling itself:

* **Significance.** A difference smaller than the combined standard error of
  the two RMS estimates is noise. Guiding RMS wanders by 10-20% on its own, so
  without this the store fills with confident nonsense.

* **Confounding.** Seeing degrades over a night. If the guide star's HFD moved
  materially between the two windows, the sky changed -- not the parameter --
  and the trial is recorded but excluded from learning. The guide star's HFD at
  ~2s cadence is a far better seeing proxy than NINA's HFR at 300s cadence.

Note the deliberate asymmetry with `advisor.actuator`: *reverting* a change
needs only weak evidence, while *believing* one needs strong evidence. A bad
night costs real data; a needless revert costs nothing.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from .conditions import ConditionVector
from .exclusions import IntervalSet
from .guiding import GuideBuffer, RmsStats

log = logging.getLogger(__name__)

Outcome = Literal["improved", "worsened", "neutral", "inconclusive", "pending"]


@dataclass
class Trial:
    """One parameter change under measurement."""

    change_id: int
    axis: str
    param: str
    before_value: float
    after_value: float
    applied_mono: float
    conditions: ConditionVector
    before: RmsStats
    after: Optional[RmsStats] = None
    outcome: Outcome = "pending"
    effect_sigma: Optional[float] = None
    delta: Optional[float] = None
    confounded: bool = False
    deadline_mono: float = 0.0

    @property
    def closed(self) -> bool:
        return self.outcome != "pending"

    def as_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "axis": self.axis,
            "param": self.param,
            "before_value": self.before_value,
            "after_value": self.after_value,
            "outcome": self.outcome,
            "rms_before": round(self.before.rms_total, 3),
            "rms_after": round(self.after.rms_total, 3) if self.after else None,
            "delta": round(self.delta, 3) if self.delta is not None else None,
            "effect_sigma": (
                round(self.effect_sigma, 2) if self.effect_sigma is not None else None
            ),
            "confounded": self.confounded,
        }


@dataclass
class TrialTracker:
    """
    Holds at most one open trial.

    That single-trial rule is not a simplification -- it is what makes
    attribution possible at all. With two changes in flight, neither
    before/after comparison means anything.
    """

    before_window_s: float = 300.0
    after_window_s: float = 300.0
    settle_lag_s: float = 30.0
    min_samples: int = 30
    min_effect_sigma: float = 1.5
    hfd_confound_ratio: float = 0.20
    deadline_multiple: float = 3.0

    open_trial: Optional[Trial] = None
    closed: list[Trial] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return self.open_trial is not None

    def measure_before(
        self,
        buffer: GuideBuffer,
        exclusions: IntervalSet,
        at: Optional[float] = None,
    ) -> Optional[RmsStats]:
        """Retrospective baseline for a change about to be applied."""
        now = time.monotonic() if at is None else at
        return buffer.window(
            now - self.before_window_s,
            now,
            exclusions=exclusions,
            min_samples=self.min_samples,
        )

    def open(
        self,
        *,
        change_id: int,
        axis: str,
        param: str,
        before_value: float,
        after_value: float,
        before: RmsStats,
        conditions: ConditionVector,
        at: Optional[float] = None,
    ) -> Trial:
        if self.open_trial is not None:
            raise RuntimeError("a trial is already open")
        now = time.monotonic() if at is None else at
        trial = Trial(
            change_id=change_id,
            axis=axis,
            param=param,
            before_value=before_value,
            after_value=after_value,
            applied_mono=now,
            conditions=conditions,
            before=before,
            deadline_mono=now
            + self.settle_lag_s
            + self.after_window_s * self.deadline_multiple,
        )
        self.open_trial = trial
        log.info(
            "trial %d opened: %s.%s %.3f -> %.3f (before rms %.3f\", n=%d)",
            change_id, axis, param, before_value, after_value,
            before.rms_total, before.n,
        )
        return trial

    def poll(
        self,
        buffer: GuideBuffer,
        exclusions: IntervalSet,
        at: Optional[float] = None,
    ) -> Optional[Trial]:
        """
        Try to close the open trial. Returns it if it closed, else None.

        Called on every advisor tick. A trial that cannot gather enough clean
        samples before its deadline closes as `inconclusive` rather than
        blocking further tuning for the rest of the night.
        """
        trial = self.open_trial
        if trial is None:
            return None

        now = time.monotonic() if at is None else at
        start = trial.applied_mono + self.settle_lag_s
        end = start + self.after_window_s
        if now < end:
            if now > trial.deadline_mono:
                return self._close(trial, "inconclusive")
            return None

        after = buffer.window(
            start, end, exclusions=exclusions, min_samples=self.min_samples
        )
        if after is None:
            if now > trial.deadline_mono:
                return self._close(trial, "inconclusive")
            # Not enough clean data yet -- slide the window forward and retry.
            return None

        trial.after = after
        trial.delta = after.rms_total - trial.before.rms_total
        trial.effect_sigma = _effect_sigma(trial.before, after)
        trial.confounded = _is_confounded(
            trial.before, after, self.hfd_confound_ratio
        )

        if trial.effect_sigma < self.min_effect_sigma:
            outcome: Outcome = "neutral"
        elif trial.delta < 0:
            outcome = "improved"
        else:
            outcome = "worsened"
        return self._close(trial, outcome)

    def invalidate(self, reason: str) -> Optional[Trial]:
        """
        Abandon the open trial -- used on disconnect, flip or profile change,
        where the gap makes any comparison meaningless.
        """
        trial = self.open_trial
        if trial is None:
            return None
        log.info("trial %d invalidated: %s", trial.change_id, reason)
        return self._close(trial, "inconclusive")

    def _close(self, trial: Trial, outcome: Outcome) -> Trial:
        trial.outcome = outcome
        self.open_trial = None
        self.closed.append(trial)
        log.info(
            "trial %d closed: %s (delta %s, sigma %s%s)",
            trial.change_id,
            outcome,
            f"{trial.delta:+.3f}\"" if trial.delta is not None else "n/a",
            f"{trial.effect_sigma:.1f}" if trial.effect_sigma is not None else "n/a",
            ", CONFOUNDED" if trial.confounded else "",
        )
        return trial


def _effect_sigma(before: RmsStats, after: RmsStats) -> float:
    """How many combined standard errors separate the two measurements."""
    denom = math.hypot(before.se_total, after.se_total)
    if denom <= 0 or math.isnan(denom):
        return 0.0
    return abs(after.rms_total - before.rms_total) / denom


def _is_confounded(before: RmsStats, after: RmsStats, ratio: float) -> bool:
    """True when the guide star's size moved enough to explain the difference."""
    a, b = before.hfd_med, after.hfd_med
    if not a or math.isnan(a) or math.isnan(b) or a <= 0:
        return False
    return abs(b - a) / a > ratio
