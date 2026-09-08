"""
Applying, recording and reverting guiding parameter changes.

This is the only code path that writes to PHD2's guiding parameters. It always
re-reads the current value immediately before writing (never trusting a cached
one), records the *readback* rather than the requested value as truth, and
opens exactly one measurement trial per change.

Reverting is deliberately easier than committing. A revert returns to a value
that is already known to have worked, so it skips cooldowns and step limits; it
still respects the safety preconditions about when the mount may be touched.
The asymmetry is intentional: believing a change needs strong evidence, undoing
one needs only weak evidence, because a bad night costs real data while a
needless revert costs nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import TuningConfig
from ..metrics.conditions import ConditionVector
from ..metrics.trials import Trial, TrialTracker
from ..phd2.client import Phd2Client, Phd2Disconnected, Phd2Error
from .guardrails import GuardContext, ProposedChange, Veto, check, resolve_bound

log = logging.getLogger(__name__)


@dataclass
class AppliedChange:
    change_id: int
    axis: str
    param: str
    before: float
    requested: float
    applied: float
    source: str
    rationale: str
    at_mono: float
    at_wall: float = field(default_factory=time.time)
    reverted: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.axis, self.param)

    def as_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "axis": self.axis,
            "param": self.param,
            "before": round(self.before, 4),
            "applied": round(self.applied, 4),
            "requested": round(self.requested, 4),
            "source": self.source,
            "rationale": self.rationale,
            "at": self.at_wall,
            "reverted": self.reverted,
        }


@dataclass
class ActuationResult:
    ok: bool
    change: Optional[AppliedChange] = None
    veto: Optional[Veto] = None
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "change": self.change.as_dict() if self.change else None,
            "veto": self.veto.as_dict() if self.veto else None,
            "error": self.error,
        }


class Actuator:
    """Owns the session's change history and the direction/freeze state."""

    def __init__(
        self,
        client: Phd2Client,
        tuning: TuningConfig,
        trials: TrialTracker,
        *,
        store=None,
        session_id: Optional[int] = None,
        rig_id: Optional[int] = None,
    ) -> None:
        self.client = client
        self.tuning = tuning
        self.trials = trials
        self.store = store
        self.session_id = session_id
        self.rig_id = rig_id

        self.applied: list[AppliedChange] = []
        self.vetoes: list[Veto] = []
        self.baseline_snapshot: Optional[dict] = None
        self.last_change_mono: Optional[float] = None
        self.last_param_change_mono: dict[tuple[str, str], float] = {}
        self.direction_lock: dict[tuple[str, str], int] = {}
        self.frozen_params: set[tuple[str, str]] = set()
        self._next_local_id = 1

    # ── session bookkeeping ────────────────────────────────────────────

    def capture_baseline(self, params: dict[tuple[str, str], float]) -> dict:
        """Snapshot the parameters as found, so 'revert all' has a target."""
        self.baseline_snapshot = {f"{a}.{p}": v for (a, p), v in params.items()}
        log.info("captured session baseline: %s", self.baseline_snapshot)
        return self.baseline_snapshot

    def changes_in_last_hour(self, now: Optional[float] = None) -> int:
        now = time.monotonic() if now is None else now
        return sum(1 for c in self.applied if now - c.at_mono <= 3600.0)

    def build_context(
        self,
        *,
        buffer,
        mode: str,
        kill_switch: bool,
        exclusions_open: tuple[str, ...] = (),
        nina_flipping: bool = False,
        nina_autofocusing: bool = False,
    ) -> GuardContext:
        return GuardContext(
            state=self.client.state,
            tuning=self.tuning,
            buffer=buffer,
            trials=self.trials,
            exclusions_open=exclusions_open,
            kill_switch=kill_switch,
            mode=mode,
            changes_this_hour=self.changes_in_last_hour(),
            changes_this_session=len(self.applied),
            last_change_mono=self.last_change_mono,
            last_param_change_mono=self.last_param_change_mono,
            frozen_params=frozenset(self.frozen_params),
            direction_lock=dict(self.direction_lock),
            nina_flipping=nina_flipping,
            nina_autofocusing=nina_autofocusing,
            baseline_snapshot=self.baseline_snapshot,
        )

    # ── applying ───────────────────────────────────────────────────────

    async def apply(
        self,
        proposal: ProposedChange,
        ctx: GuardContext,
        *,
        conditions: ConditionVector,
        buffer,
        exclusions,
        model_str: Optional[str] = None,
    ) -> ActuationResult:
        """Validate, write, verify, record, and open a measurement trial."""
        target, veto = check(ctx, proposal)
        if veto is not None:
            self.vetoes.append(veto)
            del self.vetoes[:-20]
            log.info("change vetoed (%s): %s", veto.rule, veto.detail)
            return ActuationResult(ok=False, veto=veto)

        assert target is not None

        # Re-read immediately before writing: a cached value could be stale if
        # the user changed something in PHD2's own UI a moment ago.
        try:
            current = await self.client.get_algo_param(proposal.axis, proposal.param)
        except (Phd2Error, Phd2Disconnected) as exc:
            return ActuationResult(ok=False, error=f"could not read current value: {exc}")

        bound = resolve_bound(
            self.tuning, proposal.axis, proposal.param, self.client.state.pixel_scale
        )
        if bound is not None:
            target = bound.quantize(bound.clamp(target))
        if abs(target - current) < (bound.quantum if bound else 1e-6):
            veto = Veto("no_change", "value already at the target after re-read")
            return ActuationResult(ok=False, veto=veto)

        before_stats = self.trials.measure_before(buffer, exclusions)
        if before_stats is None:
            veto = Veto(
                "no_baseline_window",
                "not enough clean guide samples to measure a before-state",
            )
            self.vetoes.append(veto)
            return ActuationResult(ok=False, veto=veto)

        try:
            applied = await self.client.set_algo_param(
                proposal.axis, proposal.param, target
            )
        except (Phd2Error, Phd2Disconnected) as exc:
            return ActuationResult(ok=False, error=f"set_algo_param failed: {exc}")

        change_id = self._record(
            axis=proposal.axis,
            param=proposal.param,
            before_value=current,
            requested_value=proposal.value,
            applied_value=applied,
            source=proposal.source,
            rationale=proposal.rationale,
            conditions=conditions,
            before_rms=before_stats.rms_total,
            before_n=before_stats.n,
            model_str=model_str,
        )

        change = AppliedChange(
            change_id=change_id,
            axis=proposal.axis,
            param=proposal.param,
            before=current,
            requested=target,
            applied=applied,
            source=proposal.source,
            rationale=proposal.rationale,
            at_mono=time.monotonic(),
        )
        self.applied.append(change)
        self.last_change_mono = change.at_mono
        self.last_param_change_mono[change.key] = change.at_mono

        # First move of a parameter this session sets its allowed direction.
        delta = applied - current
        self.direction_lock.setdefault(change.key, 1 if delta > 0 else -1)

        exclusions.add("param_change", change.at_mono, self.tuning.settle_lag_s)
        self.trials.open(
            change_id=change_id,
            axis=proposal.axis,
            param=proposal.param,
            before_value=current,
            after_value=applied,
            before=before_stats,
            conditions=conditions,
        )

        log.info(
            "applied %s.%s %.4f -> %.4f (%s): %s",
            proposal.axis, proposal.param, current, applied,
            proposal.source, proposal.rationale or "no rationale",
        )
        return ActuationResult(ok=True, change=change)

    def _record(self, **kwargs) -> int:
        if self.store is None or self.session_id is None or self.rig_id is None:
            change_id = self._next_local_id
            self._next_local_id += 1
            return change_id
        return self.store.add_change(
            session_id=self.session_id, rig_id=self.rig_id, **kwargs
        )

    # ── trial outcomes ─────────────────────────────────────────────────

    async def settle_trial(self, trial: Trial) -> Optional[ActuationResult]:
        """
        Persist a closed trial and auto-revert a clear regression.

        A parameter that measurably made things worse is also frozen for the
        rest of the session: having moved it one way and been punished, moving
        it back the other way is exactly the oscillation this system must not
        do.
        """
        if self.store is not None:
            self.store.close_change(
                trial.change_id,
                outcome=trial.outcome,
                after_rms=trial.after.rms_total if trial.after else None,
                after_n=trial.after.n if trial.after else None,
                delta=trial.delta,
                effect_sigma=trial.effect_sigma,
                confounded=trial.confounded,
            )

        if trial.outcome != "worsened" or trial.confounded or trial.delta is None:
            return None
        if trial.before.rms_total <= 0:
            return None

        regression = trial.delta / trial.before.rms_total
        if regression < self.tuning.auto_revert_ratio:
            return None

        key = (trial.axis, trial.param)
        self.frozen_params.add(key)
        log.warning(
            "auto-reverting %s.%s: RMS worsened %.0f%% (%.3f\" -> %.3f\")",
            trial.axis, trial.param, regression * 100,
            trial.before.rms_total,
            trial.after.rms_total if trial.after else float("nan"),
        )
        return await self._write_raw(
            trial.axis, trial.param, trial.before_value, "auto_revert"
        )

    # ── reverting ──────────────────────────────────────────────────────

    async def revert_last(self) -> ActuationResult:
        for change in reversed(self.applied):
            if not change.reverted:
                result = await self._write_raw(
                    change.axis, change.param, change.before, "revert"
                )
                if result.ok:
                    change.reverted = True
                    if self.store is not None:
                        self.store.mark_reverted(change.change_id, "manual")
                    self.trials.invalidate("reverted by user")
                return result
        return ActuationResult(ok=False, error="no change to revert")

    async def revert_all(self, reason: str = "manual") -> ActuationResult:
        """Restore every parameter to its value at session start."""
        if not self.baseline_snapshot:
            return ActuationResult(ok=False, error="no session baseline captured")

        failures: list[str] = []
        for name, value in self.baseline_snapshot.items():
            axis, _, param = name.partition(".")
            try:
                await self.client.set_algo_param(axis, param, float(value))
            except (Phd2Error, Phd2Disconnected) as exc:
                failures.append(f"{name}: {exc}")

        for change in self.applied:
            change.reverted = True
            if self.store is not None:
                self.store.mark_reverted(change.change_id, reason)
        self.trials.invalidate(f"revert_all ({reason})")
        self.direction_lock.clear()

        if failures:
            return ActuationResult(ok=False, error="; ".join(failures))
        log.warning("reverted all parameters to session baseline (%s)", reason)
        return ActuationResult(ok=True)

    async def _write_raw(
        self, axis: str, param: str, value: float, source: str
    ) -> ActuationResult:
        """
        Write without the budget guardrails.

        Only used to return to a previously recorded value, where cooldowns and
        step limits would be actively harmful.
        """
        try:
            applied = await self.client.set_algo_param(axis, param, value)
        except (Phd2Error, Phd2Disconnected) as exc:
            return ActuationResult(ok=False, error=str(exc))
        change = AppliedChange(
            change_id=-1,
            axis=axis,
            param=param,
            before=value,
            requested=value,
            applied=applied,
            source=source,
            rationale=f"{source} to known value",
            at_mono=time.monotonic(),
        )
        return ActuationResult(ok=True, change=change)

    def history(self) -> list[dict]:
        return [c.as_dict() for c in reversed(self.applied)]
