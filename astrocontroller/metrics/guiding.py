"""
Guiding telemetry: the rolling buffer of PHD2 guide steps and the RMS
statistics derived from it.

Two decisions worth stating explicitly, because both are easy to get wrong:

* **RMS is taken about zero, not about the mean.** A non-zero mean deviation is
  a real error -- polar misalignment, differential flexure, a drifting mount --
  and subtracting it out (which is what a standard deviation does) would hide
  exactly the problem the dashboard exists to surface.

* **Timestamps are local `time.monotonic()` at arrival.** PHD2's own `Time`
  field is seconds since PHD2 started, which is useless for correlating with
  NINA events from another machine. The remote value is kept for display only.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from .exclusions import IntervalSet

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuideSample:
    """One PHD2 GuideStep, converted to angular units."""

    t_mono: float
    t_utc: datetime
    frame: int
    ra_arcsec: float
    dec_arcsec: float
    snr: float
    star_mass: float
    hfd_px: float
    ra_limited: bool = False
    dec_limited: bool = False
    ra_duration_ms: float = 0.0
    dec_duration_ms: float = 0.0

    @property
    def total_arcsec(self) -> float:
        return math.hypot(self.ra_arcsec, self.dec_arcsec)


@dataclass(frozen=True)
class RmsStats:
    """Guiding error over some window, in arcseconds."""

    rms_ra: float
    rms_dec: float
    rms_total: float
    peak_ra: float
    peak_dec: float
    n: int
    usable_seconds: float
    se_total: float
    """
    Standard error of `rms_total`. For an RMS of n samples this is
    approximately rms / sqrt(2n); it is what decides whether a measured
    before/after difference is real or noise.
    """
    hfd_med: float
    snr_med: float
    star_mass_med: float
    ra_limited_frac: float = 0.0
    dec_limited_frac: float = 0.0

    def as_dict(self) -> dict:
        return {
            "rms_ra": round(self.rms_ra, 3),
            "rms_dec": round(self.rms_dec, 3),
            "rms_total": round(self.rms_total, 3),
            "peak_ra": round(self.peak_ra, 3),
            "peak_dec": round(self.peak_dec, 3),
            "n": self.n,
            "usable_seconds": round(self.usable_seconds, 1),
            "se_total": round(self.se_total, 4),
            "hfd_med": round(self.hfd_med, 2),
            "snr_med": round(self.snr_med, 1),
        }


def _median(values: list[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _rms(values: Iterable[float]) -> float:
    vals = [v for v in values if not math.isnan(v)]
    if not vals:
        return float("nan")
    return math.sqrt(sum(v * v for v in vals) / len(vals))


def compute_rms(
    samples: list[GuideSample],
    usable_seconds: Optional[float] = None,
) -> Optional[RmsStats]:
    """Aggregate already-filtered samples. Returns None for an empty window."""
    if not samples:
        return None

    ra = [s.ra_arcsec for s in samples]
    dec = [s.dec_arcsec for s in samples]
    rms_ra = _rms(ra)
    rms_dec = _rms(dec)
    rms_total = math.sqrt(rms_ra**2 + rms_dec**2)
    n = len(samples)

    if usable_seconds is None:
        usable_seconds = samples[-1].t_mono - samples[0].t_mono

    return RmsStats(
        rms_ra=rms_ra,
        rms_dec=rms_dec,
        rms_total=rms_total,
        peak_ra=max((abs(v) for v in ra), default=0.0),
        peak_dec=max((abs(v) for v in dec), default=0.0),
        n=n,
        usable_seconds=usable_seconds,
        se_total=rms_total / math.sqrt(2 * n) if n else float("nan"),
        hfd_med=_median([s.hfd_px for s in samples]),
        snr_med=_median([s.snr for s in samples]),
        star_mass_med=_median([s.star_mass for s in samples]),
        ra_limited_frac=sum(1 for s in samples if s.ra_limited) / n,
        dec_limited_frac=sum(1 for s in samples if s.dec_limited) / n,
    )


class GuideBuffer:
    """
    Ring buffer of guide samples with exclusion-aware windowed statistics.

    Sized by duration rather than count so that the `before` window of a
    parameter change can always be computed retrospectively -- nothing has to
    be predicted or armed in advance.
    """

    def __init__(self, retain_s: float = 900.0, max_samples: int = 20000) -> None:
        self.retain_s = retain_s
        self._samples: deque[GuideSample] = deque(maxlen=max_samples)
        self.pixel_scale: Optional[float] = None
        """Arcsec per pixel for the guide camera, from PHD2 get_pixel_scale."""

    # ── ingestion ──────────────────────────────────────────────────────

    def set_pixel_scale(self, arcsec_per_px: Optional[float]) -> None:
        if arcsec_per_px and arcsec_per_px > 0:
            if self.pixel_scale and abs(self.pixel_scale - arcsec_per_px) > 1e-6:
                log.info(
                    "guide pixel scale changed %.3f -> %.3f arcsec/px",
                    self.pixel_scale,
                    arcsec_per_px,
                )
            self.pixel_scale = arcsec_per_px

    def add_guide_step(
        self,
        payload: dict,
        at: Optional[float] = None,
    ) -> Optional[GuideSample]:
        """
        Convert a PHD2 `GuideStep` event into a sample and store it.

        Returns None when the pixel scale is not yet known -- without it the
        deviations cannot be expressed in arcseconds, and storing raw pixels
        would silently corrupt every downstream comparison.
        """
        if not self.pixel_scale:
            return None

        now = time.monotonic() if at is None else at
        scale = self.pixel_scale
        sample = GuideSample(
            t_mono=now,
            t_utc=datetime.now(timezone.utc),
            frame=int(payload.get("Frame", 0) or 0),
            ra_arcsec=float(payload.get("RADistanceRaw", 0.0) or 0.0) * scale,
            dec_arcsec=float(payload.get("DECDistanceRaw", 0.0) or 0.0) * scale,
            snr=float(payload.get("SNR", 0.0) or 0.0),
            star_mass=float(payload.get("StarMass", 0.0) or 0.0),
            hfd_px=float(payload.get("HFD", 0.0) or 0.0),
            ra_limited=bool(payload.get("RALimited", False)),
            dec_limited=bool(payload.get("DecLimited", False)),
            ra_duration_ms=float(payload.get("RADuration", 0.0) or 0.0),
            dec_duration_ms=float(payload.get("DECDuration", 0.0) or 0.0),
        )
        self._samples.append(sample)
        self._prune(now)
        return sample

    def _prune(self, now: float) -> None:
        cutoff = now - self.retain_s
        while self._samples and self._samples[0].t_mono < cutoff:
            self._samples.popleft()

    def clear(self) -> None:
        self._samples.clear()

    # ── queries ────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._samples)

    @property
    def latest(self) -> Optional[GuideSample]:
        return self._samples[-1] if self._samples else None

    def samples_between(
        self,
        start: float,
        end: float,
        exclusions: Optional[IntervalSet] = None,
    ) -> list[GuideSample]:
        out = [s for s in self._samples if start <= s.t_mono < end]
        if exclusions is not None:
            out = [s for s in out if not exclusions.is_excluded(s.t_mono)]
        return out

    def window(
        self,
        start: float,
        end: float,
        exclusions: Optional[IntervalSet] = None,
        min_samples: int = 30,
    ) -> Optional[RmsStats]:
        """
        RMS over [start, end) with excluded samples removed.

        Returns None when fewer than `min_samples` clean samples survive --
        a window that thin is not evidence, and returning a number anyway is
        how a learning store fills up with noise.
        """
        samples = self.samples_between(start, end, exclusions)
        if len(samples) < min_samples:
            return None
        usable = (
            exclusions.usable_seconds(start, end)
            if exclusions is not None
            else end - start
        )
        return compute_rms(samples, usable_seconds=usable)

    def recent(
        self,
        seconds: float,
        exclusions: Optional[IntervalSet] = None,
        min_samples: int = 5,
    ) -> Optional[RmsStats]:
        """Rolling stats for the live display (a lower bar than evidence)."""
        now = time.monotonic()
        return self.window(now - seconds, now + 1.0, exclusions, min_samples)

    def graph_series(self, seconds: float = 600.0) -> dict:
        """Downsample-free series for the UI chart."""
        now = time.monotonic()
        pts = [s for s in self._samples if s.t_mono >= now - seconds]
        return {
            "t": [round(s.t_mono - now, 2) for s in pts],
            "ra": [round(s.ra_arcsec, 3) for s in pts],
            "dec": [round(s.dec_arcsec, 3) for s in pts],
            "snr": [round(s.snr, 1) for s in pts],
            "pixel_scale": self.pixel_scale,
        }
