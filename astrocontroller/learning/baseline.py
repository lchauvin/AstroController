"""
Deterministic "best known parameters" lookup.

This runs *before* the LLM, and on a well-explored night it answers the whole
question on its own. That ordering is the point: guiding RMS is dominated by
seeing and wind rather than by parameters, so a model asked to freely correlate
telemetry with outcomes will confidently learn noise. Statistics carry the
convergence; the model is reserved for the frontier where there is no prior.

Scoring uses a **shrunk weighted mean**, so a single lucky twenty-minute epoch
cannot outrank a setting with hours of evidence behind it.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from ..metrics.conditions import (
    ConditionVector,
    bucket_key,
    condition_distance,
    relevance,
    seeing_bin,
)
from .store import LearningStore

log = logging.getLogger(__name__)

Confidence = Literal["none", "weak", "moderate", "strong"]

SHRINKAGE_K = 2.0
"""Pseudo-observations pulling a candidate toward the rig-wide median."""

UNCERTAINTY_PENALTY = 0.10
"""
Arcseconds of pessimism added per unit of 1/sqrt(support).

Shrinkage alone is not enough. A single lucky twenty-minute epoch at 0.40" is
still pulled to something better than a well-supported 0.60", so it would win
on the shrunk mean despite resting on almost no evidence. Penalising by
1/sqrt(support) makes the comparison pessimistic about thinly-supported
candidates, which is the behaviour we want when the answer will be applied to
a real mount unattended.
"""

# Widening ladder, applied in order until support is sufficient. Each step must
# be a strict superset of the one before, or the search can narrow instead of
# widening and skip evidence it already knows about.
WIDENING_STEPS = ("exact", "seeing_adjacent", "any_wind", "any_altitude", "rig_global")

CONFIDENCE_BY_WIDENING: dict[str, Confidence] = {
    "exact": "strong",
    "seeing_adjacent": "moderate",
    "any_wind": "moderate",
    "any_altitude": "weak",
    "rig_global": "weak",
}


@dataclass
class Candidate:
    params: dict[str, float]
    params_hash: str
    support: float = 0.0
    """Summed relevance across contributing epochs."""
    weighted_rms: float = 0.0
    best_rms: float = math.inf
    n_epochs: int = 0
    total_seconds: float = 0.0
    epoch_ids: list[int] = field(default_factory=list)

    def score(self, prior_rms: Optional[float]) -> float:
        """Pessimistic shrunk weighted mean RMS; lower is better."""
        if self.support <= 0:
            return math.inf
        prior = prior_rms if prior_rms is not None else self.weighted_rms / self.support
        shrunk = (self.weighted_rms + SHRINKAGE_K * prior) / (self.support + SHRINKAGE_K)
        return shrunk + UNCERTAINTY_PENALTY / math.sqrt(self.support)


@dataclass
class BaselineRecommendation:
    params: dict[str, float] = field(default_factory=dict)
    """Keyed 'axis.param' -> value."""
    support: float = 0.0
    n_epochs: int = 0
    median_rms: Optional[float] = None
    best_rms: Optional[float] = None
    confidence: Confidence = "none"
    widening: str = "exact"
    epoch_ids: list[int] = field(default_factory=list)
    bucket: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.params) and self.confidence in ("moderate", "strong")

    def as_dict(self) -> dict:
        return {
            "params": self.params,
            "support": round(self.support, 2),
            "n_epochs": self.n_epochs,
            "median_rms": round(self.median_rms, 3) if self.median_rms else None,
            "best_rms": round(self.best_rms, 3) if self.best_rms else None,
            "confidence": self.confidence,
            "widening": self.widening,
            "bucket": self.bucket,
        }


def _age_days(iso: str) -> float:
    try:
        stamp = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0)


def _adjacent_seeing_buckets(bucket: str) -> set[str]:
    """The same bucket with the seeing bin shifted one step either way."""
    order = ["A", "B", "C", "D"]
    parts = dict(p.split(":", 1) for p in bucket.split("|"))
    current = parts.get("see", "?")
    if current not in order:
        return {bucket}
    idx = order.index(current)
    out = set()
    for neighbour in (idx - 1, idx, idx + 1):
        if 0 <= neighbour < len(order):
            copy = dict(parts)
            copy["see"] = order[neighbour]
            out.add("|".join(f"{k}:{v}" for k, v in copy.items()))
    return out


def _matches(row_bucket: str, target: str, widening: str) -> bool:
    """
    Does a stored bucket qualify at this widening step?

    Each step must admit everything the previous step did. `any_wind` and
    `any_altitude` therefore keep the *adjacent* seeing tolerance rather than
    demanding an exact seeing match -- otherwise they would be narrower than
    `seeing_adjacent` and the ladder would skip evidence it had already found.
    """
    if widening == "exact":
        return row_bucket == target
    if widening == "rig_global":
        return True

    adjacent = _adjacent_seeing_buckets(target)
    if widening == "seeing_adjacent":
        return row_bucket in adjacent

    parts = dict(p.split(":", 1) for p in row_bucket.split("|"))
    want = dict(p.split(":", 1) for p in target.split("|"))
    seeing_ok = any(
        parts.get("see") == dict(p.split(":", 1) for p in b.split("|")).get("see")
        for b in adjacent
    )
    if not seeing_ok:
        return False
    if widening == "any_wind":
        return all(parts.get(k) == want.get(k) for k in ("alt", "pier"))
    if widening == "any_altitude":
        return parts.get("pier") == want.get("pier")
    return False


def best_known_params(
    store: LearningStore,
    rig_id: int,
    conditions: ConditionVector,
    *,
    min_support: float = 3.0,
    max_epochs: int = 500,
) -> BaselineRecommendation:
    """
    Find the parameter set with the best evidence for conditions like these.

    Widens the search progressively when the exact bucket is too sparse, and
    reports which step it needed -- the caller uses that to decide how much to
    trust the answer.
    """
    target = bucket_key(conditions)
    rows = store.epochs_for(rig_id, limit=max_epochs)
    if not rows:
        return BaselineRecommendation(bucket=target, confidence="none")

    prior_rms = store.global_median_rms(rig_id)

    for widening in WIDENING_STEPS:
        pool = [r for r in rows if _matches(r["bucket_key"], target, widening)]
        if not pool:
            continue

        candidates: dict[str, Candidate] = {}
        for row in pool:
            try:
                cond = ConditionVector.from_dict(json.loads(row["cond_json"]))
                params = json.loads(row["params_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            weight = relevance(
                condition_distance(conditions, cond),
                _age_days(row["start_utc"]),
                float(row["usable_seconds"] or 0.0),
            )
            if weight <= 0:
                continue

            cand = candidates.get(row["params_hash"])
            if cand is None:
                cand = Candidate(params=params, params_hash=row["params_hash"])
                candidates[row["params_hash"]] = cand
            cand.support += weight
            cand.weighted_rms += weight * float(row["rms_total"])
            cand.best_rms = min(cand.best_rms, float(row["rms_total"]))
            cand.n_epochs += 1
            cand.total_seconds += float(row["usable_seconds"] or 0.0)
            cand.epoch_ids.append(int(row["epoch_id"]))

        if not candidates:
            continue

        total_support = sum(c.support for c in candidates.values())
        if total_support < min_support and widening != WIDENING_STEPS[-1]:
            continue

        winner = min(candidates.values(), key=lambda c: c.score(prior_rms))
        confidence: Confidence = (
            CONFIDENCE_BY_WIDENING[widening]
            if total_support >= min_support
            else "none"
        )
        return BaselineRecommendation(
            params=winner.params,
            support=winner.support,
            n_epochs=winner.n_epochs,
            median_rms=winner.weighted_rms / winner.support if winner.support else None,
            best_rms=None if math.isinf(winner.best_rms) else winner.best_rms,
            confidence=confidence,
            widening=widening,
            epoch_ids=winner.epoch_ids,
            bucket=target,
        )

    return BaselineRecommendation(bucket=target, confidence="none")


def explore_probability(
    n_trials_in_bucket: int,
    *,
    base: float = 0.30,
    decay: float = 8.0,
    floor: float = 0.02,
) -> float:
    """
    Chance of trying something new rather than holding the best-known setting.

    Decays with accumulated experience in the bucket, so a well-explored
    condition converges to "leave it alone" instead of fidgeting forever. This
    is what makes the system settle down over a season.
    """
    if decay <= 0:
        return floor
    return max(floor, min(base, base * math.exp(-n_trials_in_bucket / decay)))
