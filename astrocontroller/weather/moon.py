"""
Moon phase, altitude and target separation.

Computed locally rather than fetched: it is pure ephemeris, it must keep
working when the observatory has no internet, and NINA's own
`/astro-util/moon-separation` only covers the separation.

`astropy` is an optional dependency (it arrives with the `sky` extra, and is
already present via `astro-eval` when that is installed). Without it these
functions degrade to a low-precision built-in phase calculation rather than
failing -- knowing roughly how bright the moon is still beats knowing nothing.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Reference new moon: 2000-01-06 18:14 UTC, and the mean synodic month.
_NEW_MOON_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
_SYNODIC_MONTH_DAYS = 29.530588853


@dataclass
class MoonInfo:
    illumination: float
    """Fraction of the disc lit, 0.0 (new) to 1.0 (full)."""
    phase_name: str
    altitude_deg: Optional[float] = None
    azimuth_deg: Optional[float] = None
    separation_deg: Optional[float] = None
    """Angular distance from the current target, when one is known."""
    precise: bool = False
    """False when computed from the low-precision fallback."""

    @property
    def up(self) -> Optional[bool]:
        if self.altitude_deg is None:
            return None
        return self.altitude_deg > 0

    def as_dict(self) -> dict:
        return {
            "illumination": round(self.illumination, 3),
            "phase_name": self.phase_name,
            "altitude_deg": (
                round(self.altitude_deg, 1) if self.altitude_deg is not None else None
            ),
            "azimuth_deg": (
                round(self.azimuth_deg, 1) if self.azimuth_deg is not None else None
            ),
            "separation_deg": (
                round(self.separation_deg, 1)
                if self.separation_deg is not None
                else None
            ),
            "up": self.up,
            "precise": self.precise,
        }


def phase_name(illumination: float, waxing: bool) -> str:
    if illumination < 0.02:
        return "New"
    if illumination > 0.98:
        return "Full"
    if illumination < 0.48:
        return "Waxing crescent" if waxing else "Waning crescent"
    if illumination < 0.52:
        return "First quarter" if waxing else "Last quarter"
    return "Waxing gibbous" if waxing else "Waning gibbous"


def _fallback_phase(when: datetime) -> tuple[float, bool]:
    """Mean-synodic approximation: good to a few percent, no dependencies."""
    days = (when - _NEW_MOON_EPOCH).total_seconds() / 86400.0
    age = days % _SYNODIC_MONTH_DAYS
    phase_angle = 2 * math.pi * age / _SYNODIC_MONTH_DAYS
    illumination = (1 - math.cos(phase_angle)) / 2
    return illumination, age < _SYNODIC_MONTH_DAYS / 2


def moon_info(
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    elevation_m: float = 0.0,
    when: Optional[datetime] = None,
    target_ra_deg: Optional[float] = None,
    target_dec_deg: Optional[float] = None,
) -> MoonInfo:
    """
    Moon state now, with altitude and target separation when possible.

    Falls back to the low-precision phase calculation when astropy is absent or
    when no observing site is known.
    """
    when = when or datetime.now(timezone.utc)

    try:
        return _astropy_moon(
            latitude, longitude, elevation_m, when, target_ra_deg, target_dec_deg
        )
    except ImportError:
        log.debug("astropy unavailable; using approximate moon phase")
    except Exception as exc:  # noqa: BLE001 - ephemeris must never break the app
        log.warning("moon calculation failed (%s); using approximation", exc)

    illumination, waxing = _fallback_phase(when)
    return MoonInfo(
        illumination=illumination,
        phase_name=phase_name(illumination, waxing),
        precise=False,
    )


def _astropy_moon(
    latitude: Optional[float],
    longitude: Optional[float],
    elevation_m: float,
    when: datetime,
    target_ra_deg: Optional[float],
    target_dec_deg: Optional[float],
) -> MoonInfo:
    import astropy.units as u  # type: ignore[import-not-found]
    from astropy.coordinates import (  # type: ignore[import-not-found]
        AltAz,
        EarthLocation,
        SkyCoord,
        get_body,
    )
    from astropy.time import Time  # type: ignore[import-not-found]

    t = Time(when)
    location = None
    if latitude is not None and longitude is not None:
        location = EarthLocation(
            lat=latitude * u.deg, lon=longitude * u.deg, height=elevation_m * u.m
        )

    moon = get_body("moon", t, location)
    sun = get_body("sun", t, location)

    # Illuminated fraction from the sun-moon elongation as seen from Earth.
    elongation = moon.separation(sun)
    illumination = float((1 + math.cos(math.pi - elongation.radian)) / 2)

    # Waxing if the moon is east of the sun in ecliptic longitude.
    waxing = bool(
        ((moon.geocentrictrueecliptic.lon - sun.geocentrictrueecliptic.lon)
         .wrap_at(360 * u.deg).deg) < 180
    )

    altitude = azimuth = None
    if location is not None:
        altaz = moon.transform_to(AltAz(obstime=t, location=location))
        altitude = float(altaz.alt.deg)
        azimuth = float(altaz.az.deg)

    separation = None
    if target_ra_deg is not None and target_dec_deg is not None:
        target = SkyCoord(ra=target_ra_deg * u.deg, dec=target_dec_deg * u.deg)
        separation = float(moon.separation(target).deg)

    return MoonInfo(
        illumination=illumination,
        phase_name=phase_name(illumination, waxing),
        altitude_deg=altitude,
        azimuth_deg=azimuth,
        separation_deg=separation,
        precise=True,
    )
