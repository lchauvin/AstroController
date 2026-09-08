"""
HTTP client for the N.I.N.A. "Advanced API" plugin.

Every endpoint wraps its payload in a common envelope::

    {"Response": ..., "Error": "", "StatusCode": 200, "Success": true, "Type": "API"}

`_unwrap` turns that into the payload or an exception, so callers never see it.

The plugin has no authentication of any kind and binds all interfaces, which is
why AstroController must stay inside the observatory LAN.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class NinaError(RuntimeError):
    """NINA returned an unsuccessful envelope."""

    def __init__(self, endpoint: str, message: str, status: int = 0) -> None:
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.message = message
        self.status = status


class NinaUnavailable(ConnectionError):
    """NINA could not be reached at all."""


@dataclass(frozen=True)
class ImageStats:
    """
    Per-frame statistics as reported by NINA itself.

    These arrive without reading a single FITS file, which is what makes a
    separate-machine deployment practical: saturation (`max` vs bit depth),
    transparency loss (`stars` / `hfr` trend) and tracking quality
    (`hfr_stdev`, `rms_text`) are all derivable from here.
    """

    index: int
    date: Optional[str]
    filename: Optional[str]
    target: Optional[str]
    image_type: Optional[str]
    filter: Optional[str]
    exposure_s: Optional[float]
    gain: Optional[int]
    offset: Optional[int]
    temperature: Optional[float]
    hfr: Optional[float]
    hfr_stdev: Optional[float]
    stars: Optional[int]
    mean: Optional[float]
    median: Optional[float]
    stdev: Optional[float]
    min: Optional[float]
    max: Optional[float]
    rms_text: Optional[str]
    is_bayered: bool = False
    focal_length: Optional[float] = None
    camera_name: Optional[str] = None
    telescope_name: Optional[str] = None

    @classmethod
    def from_payload(cls, raw: dict, index: int = -1) -> "ImageStats":
        def num(key: str) -> Optional[float]:
            value = raw.get(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def integer(key: str) -> Optional[int]:
            value = num(key)
            return int(value) if value is not None else None

        return cls(
            index=int(raw.get("Index", index) or index),
            date=raw.get("Date"),
            filename=raw.get("Filename"),
            target=raw.get("TargetName"),
            image_type=raw.get("ImageType"),
            filter=raw.get("Filter"),
            exposure_s=num("ExposureTime"),
            gain=integer("Gain"),
            offset=integer("Offset"),
            temperature=num("Temperature"),
            hfr=num("HFR"),
            hfr_stdev=num("HFRStDev"),
            stars=integer("Stars"),
            mean=num("Mean"),
            median=num("Median"),
            stdev=num("StDev"),
            min=num("Min"),
            max=num("Max"),
            rms_text=raw.get("RmsText"),
            is_bayered=bool(raw.get("IsBayered", False)),
            focal_length=num("FocalLength"),
            camera_name=raw.get("CameraName"),
            telescope_name=raw.get("TelescopeName"),
        )

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "date": self.date,
            "filename": self.filename,
            "target": self.target,
            "image_type": self.image_type,
            "filter": self.filter,
            "exposure_s": self.exposure_s,
            "gain": self.gain,
            "temperature": self.temperature,
            "hfr": self.hfr,
            "hfr_stdev": self.hfr_stdev,
            "stars": self.stars,
            "mean": self.mean,
            "median": self.median,
            "min": self.min,
            "max": self.max,
            "rms_text": self.rms_text,
        }


class NinaRest:
    """Async wrapper over the v2 REST surface."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NinaRest":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    # ── plumbing ───────────────────────────────────────────────────────

    async def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._client.get(path, params=clean)
        except httpx.HTTPError as exc:
            raise NinaUnavailable(f"{path}: {exc}") from exc
        return self._unwrap(path, resp)

    async def _get_bytes(self, path: str, **params: Any) -> bytes:
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._client.get(path, params=clean)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise NinaUnavailable(f"{path}: {exc}") from exc
        return resp.content

    @staticmethod
    def _unwrap(path: str, resp: httpx.Response) -> Any:
        try:
            body = resp.json()
        except ValueError as exc:
            raise NinaError(path, f"non-JSON response ({resp.status_code})") from exc
        if not isinstance(body, dict):
            return body
        if body.get("Success") is False:
            raise NinaError(
                path,
                body.get("Error") or "unknown error",
                int(body.get("StatusCode", resp.status_code) or 0),
            )
        return body.get("Response")

    # ── version / application ──────────────────────────────────────────

    async def version(self) -> Any:
        return await self._get("/version")

    async def nina_version(self) -> Any:
        return await self._get("/version/nina")

    async def logs(self, line_count: int = 100, level: str = "INFO") -> list[dict]:
        """
        Recent NINA log lines, already parsed into
        Timestamp / Level / Source / Member / Line / Message.
        """
        rows = await self._get("/application/logs", lineCount=line_count, level=level)
        return list(rows or [])

    async def screenshot(self) -> Any:
        return await self._get("/application/screenshot")

    async def event_history(self) -> list[dict]:
        rows = await self._get("/event-history")
        return list(rows or [])

    # ── profile ────────────────────────────────────────────────────────

    async def profile(self) -> Any:
        return await self._get("/profile/show")

    async def site_location(self) -> Optional[tuple[float, float, float]]:
        """
        (latitude, longitude, elevation) from the active NINA profile.

        Returned as None when the shape is unfamiliar rather than guessing --
        a wrong observing site silently poisons every moon and altitude
        calculation downstream.
        """
        try:
            payload = await self.profile()
        except (NinaError, NinaUnavailable) as exc:
            log.debug("could not read NINA profile: %s", exc)
            return None

        astro = _find_key(payload, "AstrometrySettings") or {}
        lat, lon = astro.get("Latitude"), astro.get("Longitude")
        if lat is None or lon is None:
            return None
        try:
            return (float(lat), float(lon), float(astro.get("Elevation", 0.0) or 0.0))
        except (TypeError, ValueError):
            return None

    # ── equipment ──────────────────────────────────────────────────────

    async def equipment_info(self, device: str) -> Any:
        return await self._get(f"/equipment/{device}/info")

    async def all_equipment(self) -> dict[str, Any]:
        """Best-effort snapshot; a missing device is reported, not fatal."""
        devices = (
            "camera", "mount", "focuser", "filterwheel",
            "guider", "rotator", "weather", "safetymonitor", "flatdevice",
        )
        out: dict[str, Any] = {}
        for name in devices:
            try:
                out[name] = await self.equipment_info(name)
            except (NinaError, NinaUnavailable) as exc:
                out[name] = {"Connected": False, "Error": str(exc)}
        return out

    async def guider_graph(self) -> Any:
        """NINA's own guider history -- a fallback when PHD2 is unreachable."""
        return await self._get("/equipment/guider/graph")

    async def last_autofocus(self) -> Any:
        return await self._get("/equipment/focuser/last-af")

    # ── sequence ───────────────────────────────────────────────────────

    async def sequence_json(self) -> Any:
        return await self._get("/sequence/json")

    async def sequence_state(self) -> Any:
        return await self._get("/sequence/state")

    async def sequence_start(self) -> Any:
        return await self._get("/sequence/start")

    async def sequence_stop(self) -> Any:
        return await self._get("/sequence/stop")

    async def sequence_skip(self) -> Any:
        return await self._get("/sequence/skip")

    async def sequence_reset(self) -> Any:
        return await self._get("/sequence/reset")

    # ── images ─────────────────────────────────────────────────────────

    async def image_history(
        self,
        all_images: bool = True,
        image_type: Optional[str] = None,
    ) -> list[ImageStats]:
        rows = await self._get(
            "/image-history",
            all=str(bool(all_images)).lower(),
            imageType=image_type,
        )
        if not isinstance(rows, list):
            return []
        return [ImageStats.from_payload(r, i) for i, r in enumerate(rows)]

    async def image_count(self, image_type: Optional[str] = None) -> int:
        value = await self._get("/image-history", count="true", imageType=image_type)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def image_bytes(
        self,
        index: int,
        *,
        scale: Optional[float] = 0.4,
        quality: int = 80,
        debayer: bool = True,
    ) -> bytes:
        """A stretched JPEG preview -- not the raw FITS, which stays on the rig."""
        return await self._get_bytes(
            f"/image/{index}",
            stream="true",
            resize="true" if scale else None,
            scale=scale,
            quality=quality,
            debayer=str(bool(debayer)).lower(),
        )

    async def thumbnail_bytes(self, index: int) -> bytes:
        return await self._get_bytes(f"/image/thumbnail/{index}", stream="true")

    # ── sky ────────────────────────────────────────────────────────────

    async def moon_separation(self) -> Any:
        return await self._get("/astro-util/moon-separation")


def _find_key(payload: Any, key: str) -> Optional[dict]:
    """
    Depth-first search for a nested key.

    NINA's profile payload is deeply nested and its exact shape has moved
    between plugin versions, so this looks for the section by name instead of
    hard-coding a path that would break on the next update.
    """
    if isinstance(payload, dict):
        if key in payload and isinstance(payload[key], dict):
            return payload[key]
        for value in payload.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None
