"""
Trial attribution, frame quality flags, and parsing of model replies.
"""

from __future__ import annotations

import pytest

from astrocontroller.advisor.llm import LlmError, extract_json, split_model
from astrocontroller.advisor.prompt import build_prompt, parse_proposal, render_bounds
from astrocontroller.config import DEFAULT_BOUNDS
from astrocontroller.learning.baseline import BaselineRecommendation
from astrocontroller.learning.retrieval import PromptBudget, RetrievedContext
from astrocontroller.metrics.conditions import ConditionVector
from astrocontroller.metrics.exclusions import IntervalSet
from astrocontroller.metrics.guiding import GuideBuffer
from astrocontroller.metrics.trials import TrialTracker
from astrocontroller.nina.rest import ImageStats
from astrocontroller.quality.frames import FrameAnalyzer, infer_ceiling

NOW = 5_000.0


def buffer_with(ra: float, dec: float, hfd: float = 3.0, snr: float = 25.0,
                start: float = 0.0, n: int = 200, step: float = 2.0) -> GuideBuffer:
    buf = GuideBuffer(retain_s=1e9)
    buf.set_pixel_scale(1.0)
    for i in range(n):
        buf.add_guide_step(
            {"RADistanceRaw": ra, "DECDistanceRaw": dec, "SNR": snr,
             "StarMass": 5000, "HFD": hfd},
            at=start + i * step,
        )
    return buf


# ── trials ─────────────────────────────────────────────────────────────


def test_a_clear_improvement_is_recorded_as_improved():
    tracker = TrialTracker(before_window_s=200, after_window_s=200,
                           settle_lag_s=10, min_samples=20)
    excl = IntervalSet()
    buf = buffer_with(0.8, 0.8, start=0, n=100)          # before: rms 1.13
    before = tracker.measure_before(buf, excl, at=200.0)
    assert before is not None

    tracker.open(change_id=1, axis="ra", param="aggression",
                 before_value=0.7, after_value=0.8, before=before,
                 conditions=ConditionVector(), at=200.0)

    for i in range(120):                                  # after: rms 0.42
        buf.add_guide_step(
            {"RADistanceRaw": 0.3, "DECDistanceRaw": 0.3, "SNR": 25,
             "StarMass": 5000, "HFD": 3.0},
            at=210.0 + i * 2,
        )
    trial = tracker.poll(buf, excl, at=460.0)
    assert trial is not None and trial.outcome == "improved"
    assert trial.delta < 0 and trial.effect_sigma > 1.5


def test_a_difference_within_the_noise_is_neutral():
    # Guiding RMS wanders on its own; without this the store would fill with
    # confident nonsense.
    tracker = TrialTracker(before_window_s=200, after_window_s=200,
                           settle_lag_s=10, min_samples=20)
    excl = IntervalSet()
    buf = buffer_with(0.50, 0.50, start=0, n=100)
    before = tracker.measure_before(buf, excl, at=200.0)
    tracker.open(change_id=1, axis="ra", param="aggression",
                 before_value=0.7, after_value=0.75, before=before,
                 conditions=ConditionVector(), at=200.0)
    for i in range(120):
        buf.add_guide_step(
            {"RADistanceRaw": 0.505, "DECDistanceRaw": 0.505, "SNR": 25,
             "StarMass": 5000, "HFD": 3.0},
            at=210.0 + i * 2,
        )
    trial = tracker.poll(buf, excl, at=460.0)
    assert trial.outcome == "neutral"


def test_a_seeing_change_marks_the_trial_confounded():
    # The sky changed, not the parameter. Guide-star HFD at ~2s cadence is a
    # far better seeing proxy than NINA's HFR at 300s cadence.
    tracker = TrialTracker(before_window_s=200, after_window_s=200,
                           settle_lag_s=10, min_samples=20,
                           hfd_confound_ratio=0.20)
    excl = IntervalSet()
    buf = buffer_with(0.4, 0.4, hfd=3.0, start=0, n=100)
    before = tracker.measure_before(buf, excl, at=200.0)
    tracker.open(change_id=1, axis="ra", param="aggression",
                 before_value=0.7, after_value=0.8, before=before,
                 conditions=ConditionVector(), at=200.0)
    for i in range(120):
        buf.add_guide_step(
            {"RADistanceRaw": 0.9, "DECDistanceRaw": 0.9, "SNR": 12,
             "StarMass": 5000, "HFD": 4.6},   # seeing blew out
            at=210.0 + i * 2,
        )
    trial = tracker.poll(buf, excl, at=460.0)
    assert trial.outcome == "worsened"
    assert trial.confounded is True


def test_only_one_trial_can_be_open():
    tracker = TrialTracker(min_samples=5)
    excl = IntervalSet()
    buf = buffer_with(0.5, 0.5, n=50)
    before = tracker.measure_before(buf, excl, at=100.0)
    tracker.open(change_id=1, axis="ra", param="minMove", before_value=0.1,
                 after_value=0.15, before=before, conditions=ConditionVector(),
                 at=100.0)
    with pytest.raises(RuntimeError):
        tracker.open(change_id=2, axis="ra", param="aggression", before_value=0.7,
                     after_value=0.8, before=before, conditions=ConditionVector(),
                     at=100.0)


def test_invalidate_closes_the_trial_as_inconclusive():
    tracker = TrialTracker(min_samples=5)
    excl = IntervalSet()
    buf = buffer_with(0.5, 0.5, n=50)
    before = tracker.measure_before(buf, excl, at=100.0)
    tracker.open(change_id=1, axis="ra", param="minMove", before_value=0.1,
                 after_value=0.15, before=before, conditions=ConditionVector(),
                 at=100.0)
    trial = tracker.invalidate("PHD2 disconnected")
    assert trial.outcome == "inconclusive"
    assert not tracker.busy


def test_a_trial_that_never_gathers_data_expires():
    tracker = TrialTracker(before_window_s=100, after_window_s=100,
                           settle_lag_s=10, min_samples=20)
    excl = IntervalSet()
    buf = buffer_with(0.5, 0.5, n=100)
    before = tracker.measure_before(buf, excl, at=200.0)
    tracker.open(change_id=1, axis="ra", param="minMove", before_value=0.1,
                 after_value=0.15, before=before, conditions=ConditionVector(),
                 at=200.0)
    # No further samples; well past the deadline.
    trial = tracker.poll(buf, excl, at=200.0 + 10 + 100 * 3 + 10)
    assert trial is not None and trial.outcome == "inconclusive"


# ── frame quality ──────────────────────────────────────────────────────


def stats(**kw) -> ImageStats:
    base = dict(
        index=0, date=None, filename="x.fits", target="M31", image_type="LIGHT",
        filter="Ha", exposure_s=300.0, gain=100, offset=50, temperature=-10.0,
        hfr=3.0, hfr_stdev=0.3, stars=800, mean=900.0, median=880.0,
        stdev=100.0, min=100.0, max=40000.0, rms_text="0.5",
    )
    base.update(kw)
    return ImageStats(**base)


def test_saturation_is_detected_against_the_bit_depth():
    analyzer = FrameAnalyzer()
    assert analyzer.analyze(stats(max=65535.0)).saturated
    assert not analyzer.analyze(stats(max=40000.0)).saturated


def test_ceiling_inference():
    assert infer_ceiling(65535.0) == 65535.0
    assert infer_ceiling(16000.0) == 16383.0
    assert infer_ceiling(None) is None


def test_early_frames_are_not_judged_against_a_baseline():
    # Guessing before there is history is how a dashboard cries wolf on the
    # first sub of every session.
    analyzer = FrameAnalyzer(min_baseline=3)
    flags = analyzer.analyze(stats(stars=100))
    assert not flags.cloud_suspect
    assert flags.star_drop_pct is None


def test_star_collapse_flags_cloud():
    analyzer = FrameAnalyzer(min_baseline=3, star_drop_threshold=40.0)
    for _ in range(5):
        analyzer.analyze(stats(stars=800, hfr=3.0))
    flags = analyzer.analyze(stats(stars=300, hfr=3.0))
    assert flags.cloud_suspect
    assert flags.star_drop_pct > 40


def test_baselines_are_kept_per_filter():
    # Ha legitimately shows far fewer stars than L; a global baseline would
    # flag every narrowband frame as clouded.
    analyzer = FrameAnalyzer(min_baseline=3)
    for _ in range(5):
        analyzer.analyze(stats(filter="L", stars=3000))
    for _ in range(5):
        analyzer.analyze(stats(filter="Ha", stars=400))
    assert not analyzer.analyze(stats(filter="Ha", stars=390)).cloud_suspect
    assert analyzer.baseline("L")["median_stars"] == 3000


def test_elongated_stars_flag_tracking():
    analyzer = FrameAnalyzer(hfr_spread_threshold=0.25)
    assert analyzer.analyze(stats(hfr=3.0, hfr_stdev=1.2)).tracking_suspect
    assert not analyzer.analyze(stats(hfr=3.0, hfr_stdev=0.2)).tracking_suspect


def test_a_frame_is_never_compared_against_itself():
    analyzer = FrameAnalyzer(min_baseline=1)
    first = analyzer.analyze(stats(stars=800))
    assert first.star_drop_pct is None


# ── model plumbing ─────────────────────────────────────────────────────


def test_provider_split_keeps_openrouter_ids_intact():
    assert split_model("ollama/llama3.1:8b") == ("ollama", "llama3.1:8b")
    # OpenRouter ids contain their own slash.
    assert split_model("openrouter/anthropic/claude-sonnet-5") == (
        "openrouter",
        "anthropic/claude-sonnet-5",
    )


def test_bad_model_strings_are_rejected():
    with pytest.raises(LlmError):
        split_model("llama3.1")
    with pytest.raises(LlmError):
        split_model("nosuchprovider/model")


@pytest.mark.parametrize(
    "text",
    [
        '{"action":"none"}',
        '```json\n{"action":"none"}\n```',
        'Sure! Here you go:\n{"action":"none"}\nHope that helps.',
        '{"action":"none"}  trailing words',
    ],
)
def test_json_is_extracted_from_messy_replies(text):
    # Small models wrap JSON in prose and fences.
    assert extract_json(text) == {"action": "none"}


def test_unparseable_reply_returns_none():
    assert extract_json("I think you should raise aggression a bit.") is None
    assert extract_json("") is None


def test_braces_inside_strings_do_not_break_extraction():
    parsed = extract_json('{"action":"none","rationale":"a } brace"}')
    assert parsed["rationale"] == "a } brace"


def test_proposal_validation():
    ok, err = parse_proposal(
        {"action": "set", "axis": "ra", "param": "aggression", "value": 0.8}
    )
    assert err is None and ok["value"] == 0.8

    none, err = parse_proposal({"action": "none"})
    assert none is None and err is None

    _, err = parse_proposal({"action": "set", "axis": "up", "param": "x", "value": 1})
    assert "invalid axis" in err
    _, err = parse_proposal({"action": "set", "axis": "ra", "param": "x", "value": "hi"})
    assert "non-numeric" in err
    _, err = parse_proposal(None)
    assert err is not None


def test_bounds_rendering_hides_parameters_the_algorithm_lacks():
    # Lowpass2 exposes 'aggressiveness', not 'aggression'.
    text = render_bounds(
        list(DEFAULT_BOUNDS),
        current={("ra", "aggression"): 0.7, ("ra", "aggressiveness"): 0.7},
        available={"ra": ("minMove", "aggressiveness")},
    )
    assert "aggressiveness" in text
    assert "ra aggression " not in text


def test_prompt_includes_the_do_not_retry_list():
    ctx = RetrievedContext(
        bucket="see:B|alt:H|wind:L|pier:W",
        baseline=BaselineRecommendation(params={"ra.aggression": 0.8}),
        do_not_retry=["ra aggression 0.7->0.85 (3x here, never helped)"],
    )
    prompt = build_prompt(
        conditions=ConditionVector(seeing_arcsec=2.1, altitude_deg=62),
        stats=None,
        bounds=list(DEFAULT_BOUNDS),
        current={("ra", "aggression"): 0.7},
        available={},
        context=ctx,
        budget=PromptBudget(),
        reason="RMS high",
    )
    assert "DO NOT RETRY" in prompt
    assert "0.7->0.85" in prompt
    assert "RMS high" in prompt
