"""
Condition vectors: "what the sky and the rig were doing" at a moment in time.

Retrieval is two-stage:

* a coarse categorical **bucket key** for cheap indexed lookup, and
* a continuous weighted **distance** for ranking within and across buckets.

The bucket key deliberately contains only *seeing, altitude, wind and pier
side*. Moon, cloud, filter, target and temperature are recorded for ranking and
for the narrative shown to the model, but they are kept out of the key. That is
the decision that lets the store actually converge: with four dimensions there
are at most 108 buckets and a typical night touches a handful, so after a week
or two the buckets have real sample counts. Adding moon/cloud/filter/target
would create thousands of permanently n=1 buckets and nothing would ever be
learned.

It is also defensible physically. Moon and cloud affect *guiding* only through
guide-star SNR, which is second order and already captured directly; the
imaging filter is out of the guide path for both a guide scope and an OAG.
Pier side, by contrast, genuinely belongs in the key -- Dec backlash behaves
differently on either side of the meridian.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Bucket edges.
SEEING_EDGES = ((1.5, "A"), (2.5, "B"), (4.0, "C"))   # arcsec, else "D"
ALTITUDE_EDGES = ((35.0, "L"), (60.0, "M"))            # degrees, else "H"
WIND_EDGES = ((3.0, "L"), (7.0, "M"))                  # m/s, else "H"

UNKNOWN = "?"


@dataclass
class ConditionVector:
    """A snapshot of the conditions relevant to guiding performance."""

    seeing_arcsec: Optional[float] = None
    """Guide-star HFD in arcsec: HFD(px) x pixel_scale."""
    altitude_deg: Optional[float] = None
    azimuth_deg: Optional[float] = None
    hour_angle_h: Optional[float] = None
    pier_side: Optional[str] = None

    wind_ms: Optional[float] = None
    gust_ms: Optional[float] = None
    temp_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    dewpoint_c: Optional[float] = None
    cloud_pct: Optional[float] = None
    moon_illum: Optional[float] = None
    moon_sep_deg: Optional[float] = None

    guide_snr: Optional[float] = None
    guide_star_mass: Optional[float] = None
    rms_total: Optional[float] = None
    rms_ra: Optional[float] = None
    rms_dec: Optional[float] = None

    target: Optional[str] = None
    filter: Optional[str] = None
    exposure_s: Optional[float] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, raw: dict) -> "ConditionVector":
        known = {f for f in cls.__annotations__}
        return cls(**{k: v for k, v in raw.items() if k in known})


def _bin(value: Optional[float], edges: tuple, last: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return UNKNOWN
    for threshold, label in edges:
        if value < threshold:
            return label
    return last


def seeing_bin(value: Optional[float]) -> str:
    return _bin(value, SEEING_EDGES, "D")


def altitude_bin(value: Optional[float]) -> str:
    return _bin(value, ALTITUDE_EDGES, "H")


def wind_bin(value: Optional[float]) -> str:
    return _bin(value, WIND_EDGES, "H")


def pier_bin(side: Optional[object]) -> str:
    """
    Normalise a pier side to 'E' / 'W'.

    ASCOM reports this as `pierEast` / `pierWest` (and sometimes the raw
    enum 0 / 1), so a naive first-character check yields 'P' for every mount
    and quietly drops pier side out of the bucket key entirely.
    """
    if side is None or side == "":
        return UNKNOWN
    if isinstance(side, bool):
        return UNKNOWN
    if isinstance(side, (int, float)):
        # ASCOM PierSide: 0 = pierEast, 1 = pierWest, -1 = unknown.
        return {0: "E", 1: "W"}.get(int(side), UNKNOWN)

    text = str(side).strip().lower()
    if "east" in text:
        return "E"
    if "west" in text:
        return "W"
    if text in ("e", "w"):
        return text.upper()
    return UNKNOWN


def bucket_key(c: ConditionVector) -> str:
    """e.g. ``see:B|alt:H|wind:L|pier:W``"""
    return (
        f"see:{seeing_bin(c.seeing_arcsec)}"
        f"|alt:{altitude_bin(c.altitude_deg)}"
        f"|wind:{wind_bin(c.wind_ms)}"
        f"|pier:{pier_bin(c.pier_side)}"
    )


# Continuous ranking. `scale` is "one unit of meaningful difference".
FEATURE_WEIGHTS: dict[str, tuple[float, float]] = {
    "seeing_arcsec": (0.7, 3.0),
    "altitude_deg": (20.0, 1.5),
    "wind_ms": (4.0, 1.0),
    "hour_angle_h": (2.0, 0.5),
    "gust_ms": (6.0, 0.5),
    "temp_c": (10.0, 0.25),
    "cloud_pct": (40.0, 0.25),
    "moon_illum": (0.5, 0.25),
}


def condition_distance(a: ConditionVector, b: ConditionVector) -> float:
    """
    Weighted, normalised L1 distance.

    Dimensions missing on either side are skipped and the total is renormalised
    by the weight actually used, so partial data degrades gracefully instead of
    being treated as a zero difference (which would make sparse records look
    deceptively similar to everything).
    """
    total = 0.0
    used = 0.0
    for name, (scale, weight) in FEATURE_WEIGHTS.items():
        x, y = getattr(a, name, None), getattr(b, name, None)
        if x is None or y is None:
            continue
        total += weight * abs(x - y) / scale
        used += weight

    # Pier side is categorical: a mismatch is a full unit of difference.
    pa, pb = pier_bin(a.pier_side), pier_bin(b.pier_side)
    if pa != UNKNOWN and pb != UNKNOWN:
        total += 1.0 * (0.0 if pa == pb else 1.0)
        used += 1.0

    if used == 0.0:
        return float("inf")
    return total / used


def relevance(
    distance: float,
    age_days: float,
    usable_seconds: float,
    *,
    half_life_days: float = 120.0,
    saturation_seconds: float = 300.0,
) -> float:
    """
    How much a past observation should count now.

    Recency matters more here than in most learning problems: a belt mod, a
    re-grease, a tweak to polar alignment or simply a change of season
    invalidates old results, so evidence decays rather than accumulating
    forever.
    """
    if math.isinf(distance):
        return 0.0
    n_eff = min(4.0, usable_seconds / saturation_seconds) if saturation_seconds else 1.0
    return (
        math.exp(-distance)
        * math.exp(-max(0.0, age_days) / half_life_days)
        * min(1.0, n_eff / 2.0)
    )


def build_condition_vector(
    *,
    guide_hfd_px: Optional[float] = None,
    pixel_scale: Optional[float] = None,
    guide_snr: Optional[float] = None,
    guide_star_mass: Optional[float] = None,
    rms_total: Optional[float] = None,
    rms_ra: Optional[float] = None,
    rms_dec: Optional[float] = None,
    mount: Optional[dict] = None,
    weather: Optional[dict] = None,
    sky: Optional[dict] = None,
    frame: Optional[dict] = None,
) -> ConditionVector:
    """Assemble a vector from the disparate telemetry sources."""
    seeing = (
        guide_hfd_px * pixel_scale
        if guide_hfd_px and pixel_scale
        else None
    )
    mount = mount or {}
    weather = weather or {}
    sky = sky or {}
    frame = frame or {}

    return ConditionVector(
        seeing_arcsec=seeing,
        altitude_deg=_num(mount.get("Altitude")),
        azimuth_deg=_num(mount.get("Azimuth")),
        hour_angle_h=_num(mount.get("HoursToMeridian")),
        pier_side=mount.get("SideOfPier"),
        wind_ms=_num(weather.get("wind_ms")),
        gust_ms=_num(weather.get("gust_ms")),
        temp_c=_num(weather.get("temp_c")),
        humidity_pct=_num(weather.get("humidity_pct")),
        dewpoint_c=_num(weather.get("dewpoint_c")),
        cloud_pct=_num(weather.get("cloud_pct")),
        moon_illum=_num(sky.get("moon_illum")),
        moon_sep_deg=_num(sky.get("moon_sep_deg")),
        guide_snr=guide_snr,
        guide_star_mass=guide_star_mass,
        rms_total=rms_total,
        rms_ra=rms_ra,
        rms_dec=rms_dec,
        target=frame.get("target"),
        filter=frame.get("filter"),
        exposure_s=_num(frame.get("exposure_s")),
    )


def _num(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)  # type: ignore[arg-type]
        return None if math.isnan(out) else out
    except (TypeError, ValueError):
        return None
