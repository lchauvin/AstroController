"""
The learning store, condition bucketing, retrieval and the deterministic
baseline -- the parts that decide whether the system converges or thrashes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from astrocontroller.learning.baseline import (
    best_known_params,
    explore_probability,
)
from astrocontroller.learning.retrieval import (
    PromptBudget,
    render_priors,
    retrieve_context,
)
from astrocontroller.learning.store import (
    EpochRecord,
    LearningStore,
    params_hash,
    rig_fingerprint,
)
from astrocontroller.metrics.conditions import (
    ConditionVector,
    bucket_key,
    condition_distance,
    relevance,
    seeing_bin,
)


@pytest.fixture
def store(tmp_path) -> LearningStore:
    s = LearningStore(tmp_path / "test.db")
    yield s
    s.close()


def cond(seeing=2.0, alt=60.0, wind=2.0, pier="West", **kw) -> ConditionVector:
    return ConditionVector(
        seeing_arcsec=seeing, altitude_deg=alt, wind_ms=wind, pier_side=pier, **kw
    )


def add_epoch(store, rig, session, params, rms, *, conditions=None, seconds=900.0,
              start=None):
    c = conditions or cond()
    return store.add_epoch(
        EpochRecord(
            session_id=session,
            rig_id=rig,
            start_utc=(start or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            end_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            params=params,
            bucket_key=bucket_key(c),
            close_reason="test",
            usable_seconds=seconds,
            n_samples=int(seconds / 2),
            rms_total=rms,
            rms_ra=rms * 0.7,
            rms_dec=rms * 0.7,
            guide_hfd_med=c.seeing_arcsec,
            altitude_med=c.altitude_deg,
            conditions=c.as_dict(),
        )
    )


# ── bucketing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [(1.0, "A"), (2.0, "B"), (3.0, "C"), (5.0, "D"), (None, "?")],
)
def test_seeing_bins(value, expected):
    assert seeing_bin(value) == expected


def test_bucket_key_shape():
    assert bucket_key(cond(2.0, 70, 1.0, "West")) == "see:B|alt:H|wind:L|pier:W"


def test_bucket_key_excludes_moon_cloud_and_filter():
    # Keeping these out of the key is what stops the store fragmenting into
    # thousands of permanently single-sample buckets.
    dark = cond(moon_illum=0.0, cloud_pct=0, filter="Ha", target="M31")
    bright = cond(moon_illum=0.95, cloud_pct=80, filter="OIII", target="M42")
    assert bucket_key(dark) == bucket_key(bright)


def test_pier_side_is_in_the_key():
    # Dec backlash genuinely behaves differently either side of the meridian.
    assert bucket_key(cond(pier="East")) != bucket_key(cond(pier="West"))


def test_distance_grows_with_difference():
    base = cond(2.0, 60, 2.0)
    near = condition_distance(base, cond(2.2, 62, 2.5))
    far = condition_distance(base, cond(4.5, 25, 12.0))
    assert near < far


def test_missing_dimensions_are_skipped_not_treated_as_equal():
    # Renormalising by the weight actually used stops a sparse record looking
    # deceptively similar to everything.
    full = condition_distance(cond(2.0, 60, 2.0), cond(4.0, 60, 2.0))
    sparse = condition_distance(
        ConditionVector(seeing_arcsec=2.0), ConditionVector(seeing_arcsec=4.0)
    )
    assert sparse == pytest.approx(full * 0 + sparse)  # computed, not inf
    assert condition_distance(ConditionVector(), ConditionVector()) == float("inf")


def test_relevance_decays_with_age_and_distance():
    fresh = relevance(0.1, 1.0, 900.0)
    old = relevance(0.1, 400.0, 900.0)
    distant = relevance(3.0, 1.0, 900.0)
    brief = relevance(0.1, 1.0, 60.0)
    assert fresh > old and fresh > distant and fresh > brief


# ── store basics ───────────────────────────────────────────────────────


def test_rig_fingerprint_is_stable_and_scale_tolerant():
    a = rig_fingerprint("EQ6", "ASI120", "ASI2600", 1624, 1.60)
    b = rig_fingerprint("eq6", "asi120", "asi2600", 1624, 1.61)
    c = rig_fingerprint("EQ6", "ASI174", "ASI2600", 1624, 1.60)
    assert a == b       # case and tiny scale differences do not fork the rig
    assert a != c       # a different guide camera does


def test_params_hash_ignores_float_noise():
    assert params_hash({"ra.aggression": 0.70}) == params_hash(
        {"ra.aggression": 0.700001}
    )
    assert params_hash({"ra.aggression": 0.70}) != params_hash(
        {"ra.aggression": 0.80}
    )


def test_session_and_epoch_roundtrip(store):
    rig = store.ensure_rig(mount="EQ6", guide_camera="ASI120", pixel_scale=1.6)
    assert store.ensure_rig(mount="EQ6", guide_camera="ASI120", pixel_scale=1.6) == rig

    session = store.start_session(rig, baseline_params={"ra.aggression": 0.7})
    add_epoch(store, rig, session, {"ra.aggression": 0.7}, 0.65)

    assert store.stats()["epochs"] == 1
    assert store.baseline_params(session) == {"ra.aggression": 0.7}


def test_change_lifecycle(store):
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    change = store.add_change(
        session_id=session, rig_id=rig, source="llm",
        axis="ra", param="aggression",
        before_value=0.70, requested_value=0.80, applied_value=0.80,
        conditions=cond(), rationale="test", before_rms=0.72, before_n=90,
    )
    store.close_change(
        change, outcome="improved", after_rms=0.63, after_n=95,
        delta=-0.09, effect_sigma=2.6, confounded=False,
    )
    rows = store.changes_for(rig)
    assert len(rows) == 1 and rows[0]["outcome"] == "improved"


# ── baseline ───────────────────────────────────────────────────────────


def test_no_baseline_without_evidence(store):
    rig = store.ensure_rig(mount="EQ6")
    rec = best_known_params(store, rig, cond())
    assert rec.confidence == "none" and not rec.params


def test_baseline_picks_the_best_supported_settings(store):
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    good = {"ra.aggression": 0.80}
    bad = {"ra.aggression": 0.50}
    for _ in range(4):
        add_epoch(store, rig, session, good, 0.55)
        add_epoch(store, rig, session, bad, 0.95)

    rec = best_known_params(store, rig, cond())
    assert rec.params == good
    assert rec.confidence in ("moderate", "strong")
    assert rec.usable


def test_a_single_lucky_epoch_cannot_outrank_sustained_evidence(store):
    # Shrinkage toward the rig-wide median is what prevents one lucky
    # twenty-minute run from redefining "best known".
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    solid = {"ra.aggression": 0.80}
    for _ in range(6):
        add_epoch(store, rig, session, solid, 0.60, seconds=1200)
    add_epoch(store, rig, session, {"ra.aggression": 0.45}, 0.40, seconds=310)

    rec = best_known_params(store, rig, cond())
    assert rec.params == solid


def test_search_widens_when_the_exact_bucket_is_sparse(store):
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    # Evidence recorded in 3.0" seeing (bucket C)...
    other = cond(seeing=3.0)
    for _ in range(8):
        add_epoch(store, rig, session, {"ra.aggression": 0.6}, 0.9, conditions=other)

    # ...queried at 2.4" (bucket B) finds it by widening to the adjacent bin.
    rec = best_known_params(store, rig, cond(seeing=2.4))
    assert rec.params == {"ra.aggression": 0.6}
    assert rec.widening != "exact"
    assert rec.confidence in ("moderate", "weak")


def test_sparse_evidence_is_reported_as_unusable_rather_than_guessed(store):
    # Below the support threshold the baseline must decline to answer, so the
    # advisor explores instead of acting on one lucky run.
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    add_epoch(store, rig, session, {"ra.aggression": 0.6}, 0.5, seconds=310)
    rec = best_known_params(store, rig, cond())
    assert not rec.usable


def test_old_evidence_loses_to_recent_evidence(store):
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    stale = datetime.now(timezone.utc) - timedelta(days=400)
    for _ in range(5):
        add_epoch(store, rig, session, {"ra.aggression": 0.5}, 0.50, start=stale)
    for _ in range(5):
        add_epoch(store, rig, session, {"ra.aggression": 0.8}, 0.58)

    rec = best_known_params(store, rig, cond())
    assert rec.params == {"ra.aggression": 0.8}


def test_exploration_decays_with_experience():
    # This is the convergence property: a well-explored bucket stops being
    # fiddled with.
    assert explore_probability(0) > explore_probability(10) > explore_probability(60)
    assert explore_probability(1000) >= 0.02


# ── retrieval and prompt rendering ─────────────────────────────────────


def test_retrieved_context_fits_the_small_model_budget(store):
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    for i in range(30):
        add_epoch(store, rig, session, {"ra.aggression": 0.5 + i * 0.01}, 0.6 + i * 0.01)

    budget = PromptBudget.for_profile("small")
    baseline = best_known_params(store, rig, cond())
    ctx = retrieve_context(store, rig, cond(), baseline, budget)
    text = render_priors(ctx, budget)

    assert len(ctx.epochs) <= budget.max_epochs
    assert len(text) <= budget.max_chars
    assert "BEST-KNOWN" in text


def test_do_not_retry_lists_repeated_failures(store):
    # Without this the model re-proposes the same dead end every night.
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    for _ in range(3):
        change = store.add_change(
            session_id=session, rig_id=rig, source="llm",
            axis="ra", param="aggression",
            before_value=0.70, requested_value=0.85, applied_value=0.85,
            conditions=cond(),
        )
        store.close_change(
            change, outcome="worsened", after_rms=0.90, after_n=90,
            delta=0.15, effect_sigma=3.0, confounded=False,
        )

    baseline = best_known_params(store, rig, cond())
    ctx = retrieve_context(store, rig, cond(), baseline, PromptBudget())
    assert any("0.7->0.85" in row for row in ctx.do_not_retry)
    assert "DO NOT RETRY" in render_priors(ctx, PromptBudget())


def test_confounded_changes_are_excluded_from_do_not_retry(store):
    # The sky changed, not the parameter -- it is not evidence either way.
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    for _ in range(3):
        change = store.add_change(
            session_id=session, rig_id=rig, source="llm",
            axis="ra", param="aggression",
            before_value=0.70, requested_value=0.85, applied_value=0.85,
            conditions=cond(),
        )
        store.close_change(
            change, outcome="worsened", after_rms=0.90, after_n=90,
            delta=0.15, effect_sigma=3.0, confounded=True,
        )

    baseline = best_known_params(store, rig, cond())
    ctx = retrieve_context(store, rig, cond(), baseline, PromptBudget())
    assert ctx.do_not_retry == []


def test_pruning_keeps_the_knowledge_and_drops_the_samples(store):
    rig = store.ensure_rig(mount="EQ6")
    session = store.start_session(rig)
    sample = store.add_condition_sample(session, cond())
    add_epoch(store, rig, session, {"ra.aggression": 0.7}, 0.6)

    # Backdate the sample; a fresh one must survive a normal prune.
    old = (datetime.now(timezone.utc) - timedelta(days=900)).isoformat(timespec="seconds")
    store.db.execute(
        "UPDATE condition_sample SET t_utc = ? WHERE sample_id = ?", (old, sample)
    )
    store.db.commit()
    store.add_condition_sample(session, cond())

    assert store.prune(older_than_days=730) == 1
    stats = store.stats()
    assert stats["condition_samples"] == 1   # the recent one is kept
    assert stats["epochs"] == 1              # epochs are never pruned
