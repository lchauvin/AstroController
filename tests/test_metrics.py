"""Exclusion algebra and guiding RMS windowing."""

from __future__ import annotations

import math

import pytest

from astrocontroller.metrics.exclusions import ExclusionTracker, IntervalSet
from astrocontroller.metrics.guiding import GuideBuffer, compute_rms


# ── IntervalSet ────────────────────────────────────────────────────────


def test_usable_seconds_subtracts_exclusions():
    s = IntervalSet()
    s.add("dither", 100.0, 30.0)
    assert s.usable_seconds(0.0, 200.0) == pytest.approx(170.0)


def test_overlapping_exclusions_are_not_double_counted():
    s = IntervalSet()
    s.add("dither", 100.0, 30.0)      # 100-130
    s.add("star_lost", 120.0, 30.0)   # 120-150 (overlaps)
    # Union is 100-150 == 50s excluded, not 60.
    assert s.usable_seconds(0.0, 200.0) == pytest.approx(150.0)


def test_open_interval_excludes_everything_after_it():
    s = IntervalSet()
    s.open("calibrating", at=50.0)
    assert s.is_excluded(1e6)
    assert s.usable_seconds(0.0, 100.0) == pytest.approx(50.0)


def test_close_applies_trailing_guard():
    s = IntervalSet()
    s.open("settling", at=10.0)
    s.close("settling", at=20.0, guard_s=5.0)
    assert s.is_excluded(24.9)
    assert not s.is_excluded(25.1)


def test_closing_unopened_interval_is_ignored():
    # Events can be missed across a reconnect; a stray close must not raise.
    s = IntervalSet()
    assert s.close("settling", at=10.0) is None


def test_exclusions_clipped_to_query_window():
    s = IntervalSet()
    s.add("flip", 0.0, 500.0)
    # Only 100-200 of the query window is covered.
    assert s.usable_seconds(100.0, 200.0) == pytest.approx(0.0)
    assert s.usable_seconds(400.0, 600.0) == pytest.approx(100.0)


# ── ExclusionTracker ───────────────────────────────────────────────────


def test_dither_settle_sequence_excludes_the_whole_disturbance():
    t = ExclusionTracker(settle_guard_s=5.0, dither_guard_s=30.0)
    t.on_phd2_event("GuidingDithered", at=100.0)
    t.on_phd2_event("SettleBegin", at=100.5)
    t.on_phd2_event("SettleDone", at=140.0)
    # dither covers 100-130, settling covers 100.5-145 -> union 100-145.
    assert t.intervals.usable_seconds(0.0, 200.0) == pytest.approx(155.0)
    assert not t.disturbed


def test_meridian_flip_holds_a_long_guard():
    t = ExclusionTracker(flip_guard_s=300.0)
    t.on_nina_event("MOUNT-BEFORE-FLIP", at=1000.0)
    t.on_nina_event("MOUNT-AFTER-FLIP", at=1120.0)
    assert t.intervals.is_excluded(1400.0)
    assert not t.intervals.is_excluded(1421.0)


def test_disconnect_opens_an_unbounded_gap():
    t = ExclusionTracker()
    t.on_phd2_event("SettleBegin", at=10.0)
    t.on_disconnect(at=50.0)
    assert "disconnected" in t.intervals.open_reasons
    assert "settling" not in t.intervals.open_reasons  # closed by the disconnect
    assert t.intervals.is_excluded(9999.0)
    t.on_reconnect(at=100.0)
    assert not t.intervals.is_excluded(200.0)


# ── RMS ────────────────────────────────────────────────────────────────


def _fill(buf: GuideBuffer, start: float, n: int, ra: float, dec: float, step: float = 2.0):
    for i in range(n):
        buf.add_guide_step(
            {"Frame": i, "RADistanceRaw": ra, "DECDistanceRaw": dec,
             "SNR": 20.0, "StarMass": 5000.0, "HFD": 3.0},
            at=start + i * step,
        )


def test_rms_is_about_zero_not_about_the_mean():
    # A constant 1.0" offset is a real error (polar misalignment / flexure).
    # Standard deviation would report 0; RMS about zero must report 1.0.
    buf = GuideBuffer()
    buf.set_pixel_scale(1.0)
    _fill(buf, 0.0, 50, ra=1.0, dec=0.0)
    stats = buf.window(0.0, 200.0, min_samples=10)
    assert stats is not None
    assert stats.rms_ra == pytest.approx(1.0)
    assert stats.rms_total == pytest.approx(1.0)


def test_pixel_scale_converts_to_arcsec():
    buf = GuideBuffer()
    buf.set_pixel_scale(2.5)
    _fill(buf, 0.0, 40, ra=0.4, dec=0.0)
    stats = buf.window(0.0, 200.0, min_samples=10)
    assert stats.rms_ra == pytest.approx(1.0)


def test_no_samples_without_pixel_scale():
    # Storing raw pixels as if they were arcsec would corrupt every later
    # comparison, so the sample is refused instead.
    buf = GuideBuffer()
    assert buf.add_guide_step({"RADistanceRaw": 1.0}) is None
    assert len(buf) == 0


def test_window_excludes_dither_samples():
    buf = GuideBuffer()
    buf.set_pixel_scale(1.0)
    _fill(buf, 0.0, 50, ra=0.5, dec=0.5)          # t = 0..98, good
    _fill(buf, 100.0, 15, ra=8.0, dec=8.0)        # t = 100..128, dither spike

    tracker = ExclusionTracker(dither_guard_s=30.0)
    tracker.on_phd2_event("GuidingDithered", at=100.0)

    dirty = buf.window(0.0, 200.0, min_samples=10)
    clean = buf.window(0.0, 200.0, exclusions=tracker.intervals, min_samples=10)

    assert clean.rms_total == pytest.approx(math.hypot(0.5, 0.5), abs=1e-6)
    assert dirty.rms_total > clean.rms_total * 2
    assert clean.n == 50


def test_window_returns_none_below_min_samples():
    # A window this thin is not evidence; returning a number anyway is how a
    # learning store fills up with noise.
    buf = GuideBuffer()
    buf.set_pixel_scale(1.0)
    _fill(buf, 0.0, 5, ra=0.5, dec=0.5)
    assert buf.window(0.0, 100.0, min_samples=30) is None


def test_standard_error_shrinks_with_more_samples():
    small = compute_rms(_samples(10), usable_seconds=20.0)
    large = compute_rms(_samples(400), usable_seconds=800.0)
    assert small.se_total > large.se_total
    assert large.se_total == pytest.approx(
        large.rms_total / math.sqrt(2 * large.n), rel=1e-6
    )


def test_buffer_prunes_beyond_its_retention_window():
    buf = GuideBuffer(retain_s=100.0)
    buf.set_pixel_scale(1.0)
    _fill(buf, 0.0, 100, ra=0.5, dec=0.5, step=2.0)  # spans 198s
    # Only the last 100s of samples survive.
    assert len(buf) == pytest.approx(51, abs=1)


def _samples(n: int):
    buf = GuideBuffer(retain_s=1e9)
    buf.set_pixel_scale(1.0)
    _fill(buf, 0.0, n, ra=0.6, dec=0.8)
    return buf.samples_between(0.0, 1e9)
