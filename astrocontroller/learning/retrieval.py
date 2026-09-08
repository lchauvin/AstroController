"""
Retrieval and prompt rendering.

The whole job here is to hand the model a *small* amount of highly relevant
history. Two constraints shape it:

* An 8B local model has a small effective context. Everything is rendered as
  fixed-width text rows, never as JSON -- JSON roughly doubles the token cost
  for the same information, and column semantics are explained once in the
  cached system prompt rather than repeated per row.

* The model must be shown **what already failed**, not just what worked.
  Without the failure ledger and the explicit do-not-retry list, a model
  re-proposes the same dead end every single night.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..metrics.conditions import (
    ConditionVector,
    bucket_key,
    condition_distance,
    relevance,
)
from .baseline import BaselineRecommendation, _age_days
from .store import LearningStore

log = logging.getLogger(__name__)


@dataclass
class PromptBudget:
    max_chars: int = 3000
    max_epochs: int = 5
    max_changes: int = 6

    @classmethod
    def for_profile(cls, profile: str) -> "PromptBudget":
        if profile == "small":
            return cls(max_chars=1600, max_epochs=3, max_changes=4)
        return cls()


@dataclass
class ScoredEpoch:
    params: dict
    rms_total: float
    seeing: Optional[float]
    altitude: Optional[float]
    wind: Optional[float]
    minutes: float
    age_days: float
    score: float


@dataclass
class ChangeRecord:
    axis: str
    param: str
    before: float
    after: float
    rms_before: Optional[float]
    rms_after: Optional[float]
    outcome: str
    effect_sigma: Optional[float]
    seeing: Optional[float]
    confounded: bool


@dataclass
class RetrievedContext:
    bucket: str
    baseline: BaselineRecommendation
    epochs: list[ScoredEpoch] = field(default_factory=list)
    changes: list[ChangeRecord] = field(default_factory=list)
    do_not_retry: list[str] = field(default_factory=list)
    n_trials_in_bucket: int = 0


def retrieve_context(
    store: LearningStore,
    rig_id: int,
    conditions: ConditionVector,
    baseline: BaselineRecommendation,
    budget: PromptBudget,
) -> RetrievedContext:
    """Gather the most relevant prior evidence for the current conditions."""
    bucket = bucket_key(conditions)
    ctx = RetrievedContext(bucket=bucket, baseline=baseline)

    scored: list[ScoredEpoch] = []
    for row in store.epochs_for(rig_id, limit=500):
        try:
            cond = ConditionVector.from_dict(json.loads(row["cond_json"]))
            params = json.loads(row["params_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        distance = condition_distance(conditions, cond)
        age = _age_days(row["start_utc"])
        weight = relevance(distance, age, float(row["usable_seconds"] or 0.0))
        if weight <= 0:
            continue
        scored.append(
            ScoredEpoch(
                params=params,
                rms_total=float(row["rms_total"]),
                seeing=cond.seeing_arcsec,
                altitude=cond.altitude_deg,
                wind=cond.wind_ms,
                minutes=float(row["usable_seconds"] or 0.0) / 60.0,
                age_days=age,
                score=weight,
            )
        )
    scored.sort(key=lambda e: e.score, reverse=True)
    ctx.epochs = scored[: budget.max_epochs]

    rows = store.changes_for(rig_id, bucket=bucket, limit=100)
    if len(rows) < budget.max_changes:
        rows += [
            r for r in store.changes_for(rig_id, limit=100)
            if r["bucket_key"] != bucket
        ]
    ctx.n_trials_in_bucket = sum(1 for r in rows if r["bucket_key"] == bucket)

    for row in rows[: budget.max_changes]:
        try:
            cond = ConditionVector.from_dict(json.loads(row["cond_json"]))
        except (json.JSONDecodeError, TypeError):
            cond = ConditionVector()
        ctx.changes.append(
            ChangeRecord(
                axis=row["axis"],
                param=row["param"],
                before=float(row["before_value"]),
                after=float(row["applied_value"]),
                rms_before=row["before_rms_total"],
                rms_after=row["after_rms_total"],
                outcome=row["outcome"] or "pending",
                effect_sigma=row["effect_sigma"],
                seeing=cond.seeing_arcsec,
                confounded=bool(row["confounded"]),
            )
        )

    ctx.do_not_retry = _do_not_retry(store, rig_id, bucket)
    return ctx


def _do_not_retry(store: LearningStore, rig_id: int, bucket: str) -> list[str]:
    """
    Transitions tried at least twice here that never helped.

    This is the single most effective anti-thrash mechanism in the prompt: it
    converts "the model has no memory" into "the model is told, in words, that
    this exact move has already been tested and failed".
    """
    tally: dict[tuple, list[float]] = {}
    for row in store.changes_for(rig_id, bucket=bucket, limit=300):
        if row["confounded"] or row["delta_rms_total"] is None:
            continue
        key = (
            row["axis"],
            row["param"],
            round(float(row["before_value"]), 2),
            round(float(row["applied_value"]), 2),
        )
        tally.setdefault(key, []).append(float(row["delta_rms_total"]))

    out: list[str] = []
    for (axis, param, before, after), deltas in tally.items():
        if len(deltas) >= 2 and sum(deltas) / len(deltas) >= 0:
            out.append(
                f"{axis} {param} {before:g}->{after:g} "
                f"({len(deltas)}x here, never helped)"
            )
    return sorted(out)


# ── rendering ──────────────────────────────────────────────────────────


def _fmt(value: Optional[float], digits: int = 2, dash: str = "-") -> str:
    if value is None:
        return dash
    return f"{value:.{digits}f}"


def _params_line(params: dict) -> str:
    return " ".join(f"{k}={float(v):g}" for k, v in sorted(params.items()))


def render_priors(ctx: RetrievedContext, budget: PromptBudget) -> str:
    """Render retrieved evidence as compact text, truncated to the budget."""
    lines: list[str] = [f"CONDITIONS BUCKET: {ctx.bucket}"]

    b = ctx.baseline
    if b.params:
        lines.append(
            f"BEST-KNOWN ({b.confidence}, {b.n_epochs} epochs, match={b.widening}): "
            f"{_params_line(b.params)} -> median {_fmt(b.median_rms)}\" "
            f"best {_fmt(b.best_rms)}\""
        )
    else:
        lines.append("BEST-KNOWN: none yet for these conditions.")

    if ctx.epochs:
        lines.append("")
        lines.append("SIMILAR PAST RUNS  (see=arcsec alt=deg wind=m/s):")
        for e in ctx.epochs:
            lines.append(
                f"  see{_fmt(e.seeing,1)} alt{_fmt(e.altitude,0)} "
                f"wnd{_fmt(e.wind,0)} | {_params_line(e.params)} "
                f"| {e.rms_total:.2f}\" {e.minutes:.0f}min {e.age_days:.0f}d ago"
            )

    if ctx.changes:
        lines.append("")
        lines.append("PAST ADJUSTMENTS AND WHAT HAPPENED:")
        for c in ctx.changes:
            verdict = {
                "improved": "BETTER",
                "worsened": "WORSE",
                "neutral": "no change",
            }.get(c.outcome, c.outcome)
            flag = " (confounded)" if c.confounded else ""
            lines.append(
                f"  {c.axis} {c.param} {c.before:g}->{c.after:g} "
                f"see{_fmt(c.seeing,1)} | {_fmt(c.rms_before)}\"->"
                f"{_fmt(c.rms_after)}\" {verdict} "
                f"{_fmt(c.effect_sigma,1)}s{flag}"
            )

    if ctx.do_not_retry:
        lines.append("")
        lines.append("DO NOT RETRY (already tested here, did not help):")
        lines.extend(f"  {row}" for row in ctx.do_not_retry)

    text = "\n".join(lines)
    if len(text) > budget.max_chars:
        text = text[: budget.max_chars].rsplit("\n", 1)[0] + "\n  ... (truncated)"
    return text
