"""
The advisor tick: decide whether to do anything, and if so, what.

The ordering here is the whole design. The LLM is *not* the first thing
consulted and is never the thing that writes to PHD2:

    1. gates        -- mode, kill switch, guiding actually happening
    2. trial upkeep -- close and record any measurement in flight
    3. conditions   -- build the condition vector from current telemetry
    4. baseline     -- a well-supported prior beats the current settings?
                       apply it deterministically and stop. No model call.
    5. actionable?  -- RMS already near the best ever seen here? hold.
    6. LLM          -- only for the frontier: no prior, or an anomaly
    7. guardrails   -- validate, clamp, apply, open a trial

On a well-explored night steps 4 and 5 answer everything and the model is
never called at all. That is deliberate: guiding RMS is dominated by seeing
and wind rather than parameters, so statistics carry the convergence and the
model is reserved for genuinely new territory. The feature still works with
the LLM disabled entirely.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import Config
from ..learning.baseline import (
    BaselineRecommendation,
    best_known_params,
    explore_probability,
)
from ..learning.retrieval import PromptBudget, retrieve_context, RetrievedContext
from ..metrics.conditions import ConditionVector
from ..metrics.guiding import RmsStats
from .actuator import Actuator
from .guardrails import ProposedChange, Veto
from .llm import LlmError, call_llm, extract_json
from .prompt import SYSTEM, build_prompt, parse_proposal

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    """What the advisor did (or declined to do) this tick, for the UI."""

    action: str
    """'idle' | 'hold' | 'baseline' | 'llm' | 'vetoed' | 'error'"""
    detail: str = ""
    proposal: Optional[dict] = None
    veto: Optional[Veto] = None
    rms: Optional[float] = None
    bucket: str = ""
    baseline: Optional[dict] = None
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "detail": self.detail,
            "proposal": self.proposal,
            "veto": self.veto.as_dict() if self.veto else None,
            "rms": round(self.rms, 3) if self.rms is not None else None,
            "bucket": self.bucket,
            "baseline": self.baseline,
            "at": self.at,
        }


class Advisor:
    """Owns the tuning policy for one session."""

    def __init__(
        self,
        config: Config,
        actuator: Actuator,
        *,
        store=None,
        rig_id: Optional[int] = None,
        session_id: Optional[int] = None,
    ) -> None:
        self.config = config
        self.actuator = actuator
        self.store = store
        self.rig_id = rig_id
        self.session_id = session_id

        self.enabled = config.tuning.enabled
        """Runtime kill switch, toggleable from the UI."""
        self.mode = config.tuning.mode
        self.last_result: Optional[TickResult] = None
        self.last_llm_call_mono: Optional[float] = None
        self.budget = PromptBudget.for_profile(config.llm.context_profile)

    # ── the tick ───────────────────────────────────────────────────────

    async def tick(
        self,
        *,
        conditions: ConditionVector,
        stats: Optional[RmsStats],
        buffer,
        exclusions,
        nina_flipping: bool = False,
        nina_autofocusing: bool = False,
    ) -> TickResult:
        tuning = self.config.tuning
        client = self.actuator.client

        # 2. Close any measurement in flight, and act on a regression.
        closed = self.actuator.trials.poll(buffer, exclusions)
        if closed is not None:
            await self.actuator.settle_trial(closed)

        # 1. Gates. These are cheap and the common case.
        if self.mode == "off":
            return self._record(TickResult("idle", "tuning is off"))
        if not client.state.connected:
            return self._record(TickResult("idle", "PHD2 not connected"))
        if client.state.app_state != "Guiding":
            return self._record(
                TickResult("idle", f"PHD2 is {client.state.app_state}")
            )
        if stats is None:
            return self._record(TickResult("idle", "no clean guiding samples yet"))
        if self.actuator.trials.busy:
            return self._record(
                TickResult("idle", "measuring the previous change", rms=stats.rms_total)
            )

        ctx = self.actuator.build_context(
            buffer=buffer,
            mode=self.mode,
            kill_switch=self.enabled,
            exclusions_open=exclusions.open_reasons,
            nina_flipping=nina_flipping,
            nina_autofocusing=nina_autofocusing,
        )

        # 3-4. Deterministic path.
        baseline = self._baseline(conditions)
        result = await self._try_baseline(
            baseline, conditions, ctx, buffer, exclusions, stats
        )
        if result is not None:
            return self._record(result)

        # 5. Is anything actually worth changing?
        target = self._target_rms(baseline)
        if stats.rms_total <= target:
            return self._record(
                TickResult(
                    "hold",
                    f"{stats.rms_total:.2f}\" is at or below the "
                    f"{target:.2f}\" target for these conditions",
                    rms=stats.rms_total,
                    bucket=baseline.bucket,
                    baseline=baseline.as_dict(),
                )
            )

        # 6-7. Frontier: ask the model.
        return self._record(
            await self._try_llm(
                baseline, conditions, ctx, buffer, exclusions, stats, target
            )
        )

    # ── steps ──────────────────────────────────────────────────────────

    def _baseline(self, conditions: ConditionVector) -> BaselineRecommendation:
        if self.store is None or self.rig_id is None:
            return BaselineRecommendation(bucket="", confidence="none")
        return best_known_params(
            self.store,
            self.rig_id,
            conditions,
            min_support=self.config.tuning.min_prior_support,
        )

    def _target_rms(self, baseline: BaselineRecommendation) -> float:
        """
        The RMS above which it is worth intervening.

        Anchored to the best previously achieved in these conditions rather
        than an absolute number: 0.8" may be excellent in poor seeing and
        terrible in good seeing.
        """
        tuning = self.config.tuning
        if baseline.best_rms:
            return max(
                tuning.target_rms_arcsec,
                baseline.best_rms * tuning.actionable_rms_ratio,
            )
        return tuning.target_rms_arcsec

    async def _try_baseline(
        self,
        baseline: BaselineRecommendation,
        conditions: ConditionVector,
        ctx,
        buffer,
        exclusions,
        stats: RmsStats,
    ) -> Optional[TickResult]:
        """Move toward known-good settings without consulting the model."""
        if not baseline.usable:
            return None

        current = self.actuator.client.state.algo_params
        drift = [
            (axis, param, value)
            for name, value in baseline.params.items()
            for axis, _, param in [name.partition(".")]
            if current.get((axis, param)) is not None
            and abs(current[(axis, param)] - float(value)) > 1e-6
        ]
        if not drift:
            return None

        # Occasionally explore instead, decaying with accumulated experience so
        # a well-understood bucket eventually stops being fiddled with at all.
        n_trials = self._trials_in_bucket(baseline.bucket)
        if random.random() < explore_probability(
            n_trials,
            base=self.config.tuning.explore_base_probability,
            decay=self.config.tuning.explore_decay_trials,
        ):
            log.debug("exploring rather than snapping to baseline")
            return None

        axis, param, value = drift[0]
        proposal = ProposedChange(
            axis=axis,
            param=param,
            value=float(value),
            rationale=(
                f"best-known for {baseline.bucket} "
                f"({baseline.n_epochs} epochs, {baseline.confidence})"
            ),
            source="baseline",
        )
        result = await self.actuator.apply(
            proposal, ctx, conditions=conditions, buffer=buffer, exclusions=exclusions
        )
        if result.ok:
            return TickResult(
                "baseline",
                f"applied best-known {axis}.{param} = {result.change.applied:g}",
                proposal=result.change.as_dict(),
                rms=stats.rms_total,
                bucket=baseline.bucket,
                baseline=baseline.as_dict(),
            )
        return TickResult(
            "vetoed",
            result.veto.detail if result.veto else (result.error or "refused"),
            veto=result.veto,
            rms=stats.rms_total,
            bucket=baseline.bucket,
            baseline=baseline.as_dict(),
        )

    def _trials_in_bucket(self, bucket: str) -> int:
        if self.store is None or self.rig_id is None or not bucket:
            return 0
        return len(self.store.changes_for(self.rig_id, bucket=bucket, limit=100))

    async def _try_llm(
        self,
        baseline: BaselineRecommendation,
        conditions: ConditionVector,
        ctx,
        buffer,
        exclusions,
        stats: RmsStats,
        target: float,
    ) -> TickResult:
        now = time.monotonic()
        min_gap = self.config.llm.min_seconds_between_calls
        if self.last_llm_call_mono and now - self.last_llm_call_mono < min_gap:
            remaining = min_gap - (now - self.last_llm_call_mono)
            return TickResult(
                "idle",
                f"model cooldown, {remaining:.0f}s remaining",
                rms=stats.rms_total,
                bucket=baseline.bucket,
            )

        context = self._context(conditions, baseline)
        prompt = build_prompt(
            conditions=conditions,
            stats=stats,
            bounds=list(self.config.tuning.bounds),
            current=self.actuator.client.state.algo_params,
            available=self.actuator.client.state.available_params,
            context=context,
            budget=self.budget,
            reason=(
                f"RMS {stats.rms_total:.2f}\" exceeds the {target:.2f}\" "
                "target for these conditions"
            ),
        )

        self.last_llm_call_mono = now
        try:
            reply = await asyncio.to_thread(
                call_llm,
                self.config.llm.model,
                SYSTEM,
                prompt,
                max_tokens=self.config.llm.max_tokens,
                timeout_s=self.config.llm.timeout_s,
                ollama_url=self.config.llm.ollama_url,
            )
        except LlmError as exc:
            self._audit(verdict="error", raw=str(exc), prompt_chars=len(prompt))
            return TickResult("error", f"model call failed: {exc}", rms=stats.rms_total)

        parsed = extract_json(reply.text)
        proposal_dict, error = parse_proposal(parsed)

        if error:
            # A malformed reply is always a no-op, never a retry loop.
            self._audit(
                verdict="parse_error", raw=reply.text, parsed=parsed,
                latency_ms=reply.latency_ms, prompt_chars=reply.prompt_chars,
            )
            return TickResult(
                "error", f"unusable model response: {error}", rms=stats.rms_total
            )

        if proposal_dict is None:
            rationale = (parsed or {}).get("rationale", "") if parsed else ""
            self._audit(
                verdict="no_action", raw=reply.text, parsed=parsed,
                latency_ms=reply.latency_ms, prompt_chars=reply.prompt_chars,
            )
            return TickResult(
                "hold",
                f"model advises no change: {rationale}"[:200],
                rms=stats.rms_total,
                bucket=baseline.bucket,
                baseline=baseline.as_dict(),
            )

        proposal = ProposedChange(
            axis=proposal_dict["axis"],
            param=proposal_dict["param"],
            value=proposal_dict["value"],
            rationale=proposal_dict["rationale"],
            source="llm",
        )

        if self.mode != "auto" or not self.enabled:
            self._audit(
                verdict="suggested", raw=reply.text, parsed=parsed,
                latency_ms=reply.latency_ms, prompt_chars=reply.prompt_chars,
            )
            return TickResult(
                "llm",
                "suggestion only (auto-apply is off)",
                proposal=proposal_dict,
                rms=stats.rms_total,
                bucket=baseline.bucket,
                baseline=baseline.as_dict(),
            )

        result = await self.actuator.apply(
            proposal,
            ctx,
            conditions=conditions,
            buffer=buffer,
            exclusions=exclusions,
            model_str=self.config.llm.model,
        )
        self._audit(
            verdict="applied" if result.ok else f"vetoed:{result.veto.rule if result.veto else 'error'}",
            raw=reply.text,
            parsed=parsed,
            latency_ms=reply.latency_ms,
            prompt_chars=reply.prompt_chars,
        )

        if result.ok:
            return TickResult(
                "llm",
                f"applied {proposal.axis}.{proposal.param} = "
                f"{result.change.applied:g}: {proposal.rationale}",
                proposal=proposal_dict,
                rms=stats.rms_total,
                bucket=baseline.bucket,
                baseline=baseline.as_dict(),
            )
        return TickResult(
            "vetoed",
            result.veto.detail if result.veto else (result.error or "refused"),
            proposal=proposal_dict,
            veto=result.veto,
            rms=stats.rms_total,
            bucket=baseline.bucket,
            baseline=baseline.as_dict(),
        )

    def _context(
        self, conditions: ConditionVector, baseline: BaselineRecommendation
    ) -> RetrievedContext:
        if self.store is None or self.rig_id is None:
            return RetrievedContext(bucket=baseline.bucket, baseline=baseline)
        return retrieve_context(
            self.store, self.rig_id, conditions, baseline, self.budget
        )

    def _audit(
        self,
        *,
        verdict: str,
        raw: Optional[str] = None,
        parsed: Optional[dict] = None,
        latency_ms: Optional[int] = None,
        prompt_chars: Optional[int] = None,
    ) -> None:
        if self.store is None:
            return
        self.store.add_advice(
            self.session_id,
            model_str=self.config.llm.model,
            verdict=verdict,
            raw_response=raw,
            parsed=parsed,
            latency_ms=latency_ms,
            prompt_chars=prompt_chars,
        )

    def _record(self, result: TickResult) -> TickResult:
        """
        Store and log the tick outcome.

        Logged deliberately: "the advisor is quiet" and "the advisor is
        blocked" look identical from the outside, and working out which is
        which from a dashboard at 2am is miserable. Idle ticks are DEBUG so a
        normal night stays readable; anything that changed something, refused
        something, or broke is INFO or louder.
        """
        previous = self.last_result
        self.last_result = result

        if result.action == "error":
            log.warning("advisor: %s", result.detail)
        elif result.action in ("baseline", "llm", "vetoed"):
            log.info("advisor %s: %s", result.action, result.detail)
        elif previous is None or previous.action != result.action or (
            previous.detail != result.detail
        ):
            # Only log a change of state, or an idle loop fills the log.
            log.debug("advisor %s: %s", result.action, result.detail)
        return result

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "model": self.config.llm.model,
            "last": self.last_result.as_dict() if self.last_result else None,
            "changes": self.actuator.history(),
            "vetoes": [v.as_dict() for v in self.actuator.vetoes[-8:]],
            "frozen": [f"{a}.{p}" for a, p in sorted(self.actuator.frozen_params)],
            "baseline_snapshot": self.actuator.baseline_snapshot,
        }
