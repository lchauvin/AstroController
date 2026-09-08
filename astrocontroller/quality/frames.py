"""
Per-frame quality assessment from NINA's own statistics.

No FITS file is read here. NINA already computes everything needed for the
common checks, and it arrives with the `IMAGE-SAVE` event, so this works over
the network with nothing mounted:

* **saturation** -- `max` against the sensor's full-well ceiling, and the gap
  between `median` and `max`;
* **transparency loss / cloud** -- a collapse in `stars` or a jump in `hfr`
  relative to the rolling median for the same filter;
* **tracking** -- `hfr_stdev` relative to `hfr`, elongated stars inflate the
  spread even when the median looks acceptable.

Comparisons are always made **within a filter**. Ha subs legitimately show far
fewer stars than L, so a global baseline would flag every narrowband frame as
clouded.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# Common ADU ceilings. NINA reports Max in ADU at the camera's bit depth.
KNOWN_CEILINGS = (65535.0, 16383.0, 4095.0, 1023.0)


@dataclass
class FrameFlags:
    saturated: bool = False
    saturation_level: Optional[float] = None
    cloud_suspect: bool = False
    star_drop_pct: Optional[float] = None
    hfr_excursion_pct: Optional[float] = None
    tracking_suspect: bool = False
    hfr_spread_ratio: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    @property
    def any_flag(self) -> bool:
        return self.saturated or self.cloud_suspect or self.tracking_suspect

    def as_dict(self) -> dict:
        return {
            "saturated": self.saturated,
            "saturation_level": (
                round(self.saturation_level, 3)
                if self.saturation_level is not None
                else None
            ),
            "cloud_suspect": self.cloud_suspect,
            "star_drop_pct": (
                round(self.star_drop_pct, 1) if self.star_drop_pct is not None else None
            ),
            "hfr_excursion_pct": (
                round(self.hfr_excursion_pct, 1)
                if self.hfr_excursion_pct is not None
                else None
            ),
            "tracking_suspect": self.tracking_suspect,
            "hfr_spread_ratio": (
                round(self.hfr_spread_ratio, 3)
                if self.hfr_spread_ratio is not None
                else None
            ),
            "notes": self.notes,
        }


def infer_ceiling(max_adu: Optional[float]) -> Optional[float]:
    """Smallest standard ADU ceiling that the observed maximum fits under."""
    if max_adu is None or max_adu <= 0:
        return None
    for ceiling in sorted(KNOWN_CEILINGS):
        if max_adu <= ceiling:
            return ceiling
    return None


def _median(values: list[float]) -> Optional[float]:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    if not clean:
        return None
    clean.sort()
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return 0.5 * (clean[mid - 1] + clean[mid])


class FrameAnalyzer:
    """
    Rolling per-filter baselines and the flags derived from them.

    The first few frames of a filter cannot be judged -- there is no baseline
    yet -- so only the absolute checks (saturation) run until enough history
    exists. Guessing early is how a dashboard cries wolf on the first sub of
    every session.
    """

    def __init__(
        self,
        *,
        history: int = 25,
        min_baseline: int = 3,
        star_drop_threshold: float = 40.0,
        hfr_excursion_threshold: float = 25.0,
        saturation_fraction: float = 0.98,
        hfr_spread_threshold: float = 0.25,
    ) -> None:
        self.history = history
        self.min_baseline = min_baseline
        self.star_drop_threshold = star_drop_threshold
        self.hfr_excursion_threshold = hfr_excursion_threshold
        self.saturation_fraction = saturation_fraction
        self.hfr_spread_threshold = hfr_spread_threshold

        self._stars: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=history)
        )
        self._hfr: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history))

    def baseline(self, filter_name: Optional[str]) -> dict:
        key = filter_name or "?"
        return {
            "filter": key,
            "n": len(self._stars[key]),
            "median_stars": _median(list(self._stars[key])),
            "median_hfr": _median(list(self._hfr[key])),
        }

    def analyze(self, stats) -> FrameFlags:
        """Flag one frame, then fold it into the rolling baseline."""
        flags = FrameFlags()
        key = stats.filter or "?"

        self._check_saturation(stats, flags)
        self._check_tracking(stats, flags)
        self._check_transparency(stats, flags, key)

        # Fold in only after judging, so a frame is never compared to itself.
        if stats.stars is not None:
            self._stars[key].append(float(stats.stars))
        if stats.hfr is not None and stats.hfr > 0:
            self._hfr[key].append(float(stats.hfr))

        return flags

    def _check_saturation(self, stats, flags: FrameFlags) -> None:
        ceiling = infer_ceiling(stats.max)
        if ceiling is None or stats.max is None:
            return
        level = stats.max / ceiling
        flags.saturation_level = level
        if level >= self.saturation_fraction:
            flags.saturated = True
            flags.notes.append(
                f"peak {stats.max:.0f} ADU is {level * 100:.0f}% of full well"
            )
            # A median already high relative to the ceiling means the whole
            # frame is hot, not just a few bright stars.
            if stats.median is not None and stats.median / ceiling > 0.5:
                flags.notes.append(
                    "background is very bright -- check exposure or moonlight"
                )

    def _check_tracking(self, stats, flags: FrameFlags) -> None:
        if not stats.hfr or not stats.hfr_stdev or stats.hfr <= 0:
            return
        ratio = stats.hfr_stdev / stats.hfr
        flags.hfr_spread_ratio = ratio
        if ratio > self.hfr_spread_threshold:
            flags.tracking_suspect = True
            flags.notes.append(
                f"HFR spread is {ratio * 100:.0f}% of HFR -- elongated or "
                "inconsistent stars"
            )

    def _check_transparency(self, stats, flags: FrameFlags, key: str) -> None:
        if len(self._stars[key]) < self.min_baseline:
            return

        base_stars = _median(list(self._stars[key]))
        base_hfr = _median(list(self._hfr[key]))

        if base_stars and stats.stars is not None and base_stars > 0:
            drop = (base_stars - stats.stars) / base_stars * 100
            flags.star_drop_pct = drop
            if drop >= self.star_drop_threshold:
                flags.cloud_suspect = True
                flags.notes.append(
                    f"star count fell {drop:.0f}% below the {key} baseline "
                    f"({stats.stars} vs {base_stars:.0f})"
                )

        if base_hfr and stats.hfr and base_hfr > 0:
            excursion = (stats.hfr - base_hfr) / base_hfr * 100
            flags.hfr_excursion_pct = excursion
            if excursion >= self.hfr_excursion_threshold:
                flags.notes.append(
                    f"HFR rose {excursion:.0f}% above the {key} baseline "
                    f"({stats.hfr:.2f} vs {base_hfr:.2f})"
                )
                # A simultaneous HFR rise and star loss is the classic cloud
                # signature; either alone is weaker evidence.
                if flags.star_drop_pct and flags.star_drop_pct > 15:
                    flags.cloud_suspect = True
