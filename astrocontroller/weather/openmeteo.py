"""
Weather from Open-Meteo.

Free, keyless, and it exposes the variables that actually matter for a night
of imaging: cloud cover split by altitude layer, precipitation probability, and
the temperature/dewpoint spread that predicts dew and frost on the corrector.

Low cloud is reported separately because it matters far more than the total:
high cirrus costs some transparency, but low cloud ends the session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_VARS = (
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "precipitation_probability",
    "relative_humidity_2m",
    "dew_point_2m",
    "temperature_2m",
    "wind_speed_10m",
    "wind_gusts_10m",
)


@dataclass
class HourlyPoint:
    time: str
    cloud_total: Optional[float] = None
    cloud_low: Optional[float] = None
    cloud_mid: Optional[float] = None
    cloud_high: Optional[float] = None
    precip_prob: Optional[float] = None
    humidity: Optional[float] = None
    dewpoint_c: Optional[float] = None
    temp_c: Optional[float] = None
    wind_ms: Optional[float] = None
    gust_ms: Optional[float] = None

    @property
    def dewpoint_spread(self) -> Optional[float]:
        """Degrees above the dew point. Below ~2 C, expect dew on optics."""
        if self.temp_c is None or self.dewpoint_c is None:
            return None
        return self.temp_c - self.dewpoint_c

    def as_dict(self) -> dict:
        out = {
            "time": self.time,
            "cloud_total": self.cloud_total,
            "cloud_low": self.cloud_low,
            "cloud_mid": self.cloud_mid,
            "cloud_high": self.cloud_high,
            "precip_prob": self.precip_prob,
            "humidity": self.humidity,
            "temp_c": self.temp_c,
            "dewpoint_c": self.dewpoint_c,
            "wind_ms": self.wind_ms,
            "gust_ms": self.gust_ms,
        }
        out["dewpoint_spread"] = self.dewpoint_spread
        return out


@dataclass
class WeatherForecast:
    latitude: float
    longitude: float
    timezone_name: str = "UTC"
    hourly: list[HourlyPoint] = field(default_factory=list)
    fetched_at: Optional[str] = None
    error: Optional[str] = None

    def current(self) -> Optional[HourlyPoint]:
        """The hour containing now, falling back to the first future hour."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        best: Optional[HourlyPoint] = None
        for point in self.hourly:
            try:
                stamp = datetime.fromisoformat(point.time)
            except ValueError:
                continue
            if stamp <= now:
                best = point
            elif best is None:
                return point
            else:
                break
        return best

    def night_summary(self, hours: int = 12) -> dict:
        """Worst-case outlook over the next `hours`, for the dashboard banner."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        upcoming: list[HourlyPoint] = []
        for point in self.hourly:
            try:
                stamp = datetime.fromisoformat(point.time)
            except ValueError:
                continue
            if stamp >= now:
                upcoming.append(point)
            if len(upcoming) >= hours:
                break

        def worst(attr: str) -> Optional[float]:
            values = [
                getattr(p, attr) for p in upcoming if getattr(p, attr) is not None
            ]
            return max(values) if values else None

        spreads = [
            p.dewpoint_spread for p in upcoming if p.dewpoint_spread is not None
        ]
        clear = [
            p for p in upcoming
            if p.cloud_total is not None and p.cloud_total <= 20
        ]
        return {
            "hours": len(upcoming),
            "max_cloud": worst("cloud_total"),
            "max_cloud_low": worst("cloud_low"),
            "max_precip_prob": worst("precip_prob"),
            "max_gust_ms": worst("gust_ms"),
            "min_dewpoint_spread": min(spreads) if spreads else None,
            "clear_hours": len(clear),
            "dew_risk": bool(spreads and min(spreads) < 2.0),
            "rain_risk": bool(
                worst("precip_prob") is not None and worst("precip_prob") >= 30
            ),
        }

    def as_dict(self) -> dict:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone_name,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "current": self.current().as_dict() if self.current() else None,
            "summary": self.night_summary(),
            "hourly": [p.as_dict() for p in self.hourly[:48]],
        }


async def fetch_forecast(
    latitude: float,
    longitude: float,
    *,
    forecast_days: int = 2,
    timeout: float = 15.0,
) -> WeatherForecast:
    """
    Fetch the hourly forecast.

    Network failure returns a forecast carrying `error` rather than raising:
    the weather panel going stale must never take the guiding dashboard down
    with it.
    """
    params = {
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "hourly": ",".join(HOURLY_VARS),
        "forecast_days": str(forecast_days),
        "timezone": "auto",
        "wind_speed_unit": "ms",
    }
    forecast = WeatherForecast(latitude=latitude, longitude=longitude)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(API_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("weather fetch failed: %s", exc)
        forecast.error = str(exc)
        return forecast

    forecast.timezone_name = payload.get("timezone", "UTC")
    forecast.hourly = _parse_hourly(payload.get("hourly") or {})
    forecast.fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.info(
        "weather: %d hourly points for %.3f,%.3f",
        len(forecast.hourly), latitude, longitude,
    )
    return forecast


def _parse_hourly(hourly: dict[str, Any]) -> list[HourlyPoint]:
    times = hourly.get("time") or []

    def col(name: str) -> list:
        values = hourly.get(name) or []
        return list(values) + [None] * (len(times) - len(values))

    columns = {name: col(name) for name in HOURLY_VARS}
    points: list[HourlyPoint] = []
    for i, stamp in enumerate(times):
        points.append(
            HourlyPoint(
                time=stamp,
                cloud_total=columns["cloud_cover"][i],
                cloud_low=columns["cloud_cover_low"][i],
                cloud_mid=columns["cloud_cover_mid"][i],
                cloud_high=columns["cloud_cover_high"][i],
                precip_prob=columns["precipitation_probability"][i],
                humidity=columns["relative_humidity_2m"][i],
                dewpoint_c=columns["dew_point_2m"][i],
                temp_c=columns["temperature_2m"][i],
                wind_ms=columns["wind_speed_10m"][i],
                gust_ms=columns["wind_gusts_10m"][i],
            )
        )
    return points
