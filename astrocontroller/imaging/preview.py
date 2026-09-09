"""
Turn the newest FITS on the image share into something a browser can show.

NINA already serves a stretched JPEG of the frame it is holding in memory
(``/image/{index}``), and that stays the fallback. But a share -- an SMB mount
of NINA's own output directory -- is better in three ways: it survives a NINA
restart, it keeps working when the Advanced API is busy, and it shows the
*saved* frame rather than whatever happens to be in the viewer.

The stretch is the usual midtone-transfer autostretch (the shape PixInsight's
STF and NINA's own preview both use): clip the shadows a few robust sigmas
below the sky level, then pull the sky up to a fixed target. It is not a
science-grade rendition and does not try to be -- it exists so somebody on a
phone can see whether the last sub is trailed, clouded, or fine.

numpy and astropy are optional. Without them this module reports itself
unavailable and the UI falls back to NINA's own preview.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

FITS_SUFFIXES = (".fits", ".fit", ".fts")

# A frame still being copied onto the share is unreadable in an interesting
# variety of ways. Rather than parse the failures, ignore anything written in
# the last moment and let the next poll pick it up.
MIN_AGE_S = 2.0

# Guard against share_path being pointed at an entire image library by mistake.
MAX_SCAN_ENTRIES = 20_000
MAX_SCAN_DEPTH = 6


class PreviewError(RuntimeError):
    """No preview could be produced -- the message says why."""


@dataclass(frozen=True)
class StretchOptions:
    max_width: int = 1400
    """Longest edge of the rendered PNG; the frame is binned down to reach it."""
    shadow_sigma: float = 2.5
    """Shadow clip, in robust sigmas below the sky level."""
    target_background: float = 0.18
    """Where the sky lands on a 0-1 scale. Higher looks brighter and flatter."""
    white_percentile: float = 99.9
    """
    Which percentile becomes white.

    Not the maximum: a handful of saturated star cores routinely sit thirty
    times above the sky, and normalising by them squeezes the entire nebula
    into the bottom 1% of the range. Clipping those cores is what a preview
    stretch is *for*.
    """
    invert: bool = False

    def key(self) -> tuple:
        return (
            self.max_width,
            self.shadow_sigma,
            self.target_background,
            self.white_percentile,
            self.invert,
        )


@dataclass
class PreviewImage:
    png: bytes
    meta: dict = field(default_factory=dict)


def available() -> Optional[str]:
    """None when previews can be rendered, else the reason they cannot."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return "numpy is not installed"
    try:
        from astropy.io import fits  # noqa: F401
    except ImportError:
        return "astropy is not installed (run: uv sync --extra sky)"
    return None


# -- finding the newest frame -------------------------------------------


def latest_share_file(root: str | os.PathLike) -> Optional[Path]:
    """
    Newest readable FITS under ``root``, or None.

    Sorted by modification time rather than by name: NINA's filename pattern is
    configurable, and a session that rolls past midnight does not sort into
    capture order under every pattern.
    """
    base = Path(root)
    if not base.is_dir():
        return None

    now = time.time()
    best: Optional[tuple[float, Path]] = None
    seen = 0

    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            log.debug("cannot scan %s: %s", directory, exc)
            continue
        for entry in entries:
            seen += 1
            if seen > MAX_SCAN_ENTRIES:
                log.warning(
                    "image share %s holds more than %d entries; point "
                    "[images].share_path at the download folder, not the library",
                    base,
                    MAX_SCAN_ENTRIES,
                )
                stack.clear()
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_SCAN_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
                    continue
                if not entry.name.lower().endswith(FITS_SUFFIXES):
                    continue
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if now - mtime < MIN_AGE_S:
                continue  # still landing
            if best is None or mtime > best[0]:
                best = (mtime, Path(entry.path))

    return best[1] if best else None


# -- rendering ----------------------------------------------------------


def _midtone_transfer(x: Any, midtone: float) -> Any:
    """
    ``MTF(m, x) = ((m-1)x) / ((2m-1)x - m)`` -- the standard midtone curve.

    Fixed points at 0 and 1, and ``MTF(m, m) == 0.5``, which is what makes it
    usable as "put the sky *here*" rather than as a gamma.
    """
    import numpy as np

    if midtone <= 0.0:
        return np.ones_like(x)
    if midtone >= 1.0:
        return np.zeros_like(x)
    denominator = (2.0 * midtone - 1.0) * x - midtone
    return ((midtone - 1.0) * x) / denominator


def _bin_down(data: Any, factor: int) -> Any:
    """Integer block-mean. Cheaper than an interpolated resize, and less noisy."""
    import numpy as np

    if factor <= 1:
        return data
    height = (data.shape[0] // factor) * factor
    width = (data.shape[1] // factor) * factor
    trimmed = data[:height, :width].astype(np.float32, copy=False)
    return trimmed.reshape(
        height // factor, factor, width // factor, factor
    ).mean(axis=(1, 3))


def _superpixel(data: Any, pattern: str) -> Any:
    """
    2x2 superpixel debayer: one RGB pixel per Bayer cell, at half resolution.

    Interpolating demosaics look better on stars; this is a preview, and the
    downscale below was going to halve the resolution anyway.
    """
    import numpy as np

    height = (data.shape[0] // 2) * 2
    width = (data.shape[1] // 2) * 2
    d = data[:height, :width].astype(np.float32, copy=False)
    quadrant = {
        "tl": d[0::2, 0::2],
        "tr": d[0::2, 1::2],
        "bl": d[1::2, 0::2],
        "br": d[1::2, 1::2],
    }
    order = {
        "RGGB": ("tl", ("tr", "bl"), "br"),
        "BGGR": ("br", ("tr", "bl"), "tl"),
        "GRBG": ("tr", ("tl", "br"), "bl"),
        "GBRG": ("bl", ("tl", "br"), "tr"),
    }.get(pattern.upper())
    if order is None:
        return d
    red, greens, blue = order
    green = 0.5 * (quadrant[greens[0]] + quadrant[greens[1]])
    return np.stack([quadrant[red], green, quadrant[blue]], axis=-1)


def _autostretch(data: Any, options: StretchOptions) -> Any:
    """Robust shadow clip plus a midtone pull. Returns uint8."""
    import numpy as np

    flat = data.reshape(-1)
    finite = flat[np.isfinite(flat)]
    if finite.size == 0:
        return np.zeros(data.shape, dtype=np.uint8)

    # Statistics from at most a quarter-million pixels: the median of a large
    # subsample is indistinguishable here and an order of magnitude cheaper.
    sample = finite
    if sample.size > 250_000:
        sample = sample[:: sample.size // 250_000]

    lo = float(np.min(sample))
    hi = float(np.percentile(sample, options.white_percentile))
    if hi <= lo:
        hi = float(np.max(sample))
    if hi <= lo:
        return np.zeros(data.shape, dtype=np.uint8)

    scaled = np.clip(np.nan_to_num((data - lo) / (hi - lo), nan=0.0), 0.0, 1.0)
    sample_scaled = np.clip((sample - lo) / (hi - lo), 0.0, 1.0)

    median = float(np.median(sample_scaled))
    # MAD rescaled to a Gaussian sigma. Robust to stars -- which are exactly
    # the outliers a plain standard deviation would be dragged around by.
    mad = float(np.median(np.abs(sample_scaled - median))) * 1.4826
    if mad <= 0.0:
        mad = max(float(np.std(sample_scaled)), 1e-6)

    shadows = max(0.0, min(median - options.shadow_sigma * mad, median))
    span = max(1e-6, 1.0 - shadows)
    normalised = np.clip((scaled - shadows) / span, 0.0, 1.0)

    sky = max(1e-6, min((median - shadows) / span, 0.999))
    # Solve MTF(m, sky) == target for m.
    target = options.target_background
    midtone = ((target - 1.0) * sky) / ((2.0 * target - 1.0) * sky - target)
    midtone = float(min(max(midtone, 1e-4), 0.9999))

    out = _midtone_transfer(normalised, midtone)
    if options.invert:
        out = 1.0 - out
    return (np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _header_meta(header: Any, path: Path) -> dict:
    def value(*keys: str) -> Any:
        for key in keys:
            if key in header:
                item = header[key]
                if item not in ("", None):
                    return item
        return None

    return {
        "filename": path.name,
        "path": str(path),
        "target": value("OBJECT", "TARGET"),
        "filter": value("FILTER"),
        "image_type": value("IMAGETYP"),
        "exposure_s": value("EXPOSURE", "EXPTIME"),
        "gain": value("GAIN"),
        "offset": value("OFFSET"),
        "temperature": value("CCD-TEMP", "SET-TEMP"),
        "binning": value("XBINNING"),
        "camera": value("INSTRUME"),
        "telescope": value("TELESCOP"),
        "focal_length": value("FOCALLEN"),
        "date_obs": value("DATE-LOC", "DATE-OBS"),
        "bayer": value("BAYERPAT"),
        "hfr": value("HFR"),
        "stars": value("STARS", "NSTARS"),
    }


def render_fits(path: Path, options: StretchOptions) -> PreviewImage:
    """Read one FITS file and return a stretched PNG plus its header metadata."""
    reason = available()
    if reason:
        raise PreviewError(reason)

    import numpy as np
    from astropy.io import fits

    from .png import encode_png

    started = time.monotonic()
    try:
        with fits.open(path, memmap=False) as hdul:
            hdu = next((h for h in hdul if getattr(h, "data", None) is not None), None)
            if hdu is None:
                raise PreviewError(f"{path.name} contains no image data")
            data = np.asarray(hdu.data, dtype=np.float32)
            header = hdu.header
    except PreviewError:
        raise
    except Exception as exc:  # noqa: BLE001 - a torn or exotic file is expected
        raise PreviewError(f"cannot read {path.name}: {exc}") from exc

    # Some capture software writes a degenerate axis -- (1, h, w) or (h, w, 1)
    # -- which is still a plain mono frame and must not be mistaken for colour.
    data = np.squeeze(data)
    if data.ndim == 3 and data.shape[0] in (3, 4):
        # Colour FITS: (channels, h, w).
        data = np.moveaxis(data[:3], 0, -1)
    if data.ndim not in (2, 3) or (data.ndim == 3 and data.shape[2] < 3):
        raise PreviewError(f"{path.name} has an unsupported shape {data.shape}")

    meta = _header_meta(header, path)

    pattern = meta.get("bayer")
    if data.ndim == 2 and isinstance(pattern, str) and pattern.strip():
        data = _superpixel(data, pattern.strip())
    elif data.ndim == 3:
        data = data[..., :3]

    # FITS stores rows bottom-up by convention; NINA records which way round it
    # actually wrote them. Getting this wrong flips every preview vertically.
    if str(header.get("ROWORDER", "TOP-DOWN")).upper().startswith("BOTTOM"):
        data = data[::-1]

    longest = max(data.shape[0], data.shape[1])
    factor = max(1, int(longest // max(1, options.max_width)))
    if factor > 1:
        if data.ndim == 3:
            data = np.stack(
                [_bin_down(data[..., c], factor) for c in range(data.shape[2])],
                axis=-1,
            )
        else:
            data = _bin_down(data, factor)

    # One stretch across all channels: per-channel would silently white-balance
    # the frame and hide exactly the colour cast a flat is supposed to fix.
    eight_bit = _autostretch(data, options)

    png = encode_png(eight_bit)
    meta.update(
        {
            "width": int(eight_bit.shape[1]),
            "height": int(eight_bit.shape[0]),
            "colour": eight_bit.ndim == 3,
            "bin_factor": factor,
            "render_ms": round((time.monotonic() - started) * 1000),
            "bytes": len(png),
        }
    )
    return PreviewImage(png=png, meta=meta)


class SharePreviewer:
    """
    Renders the newest frame on the share, at most once per file.

    Rendering a full-size frame costs a second or two of CPU plus a read across
    the network, so the result is cached against (path, mtime, options) and
    every browser asking for the same frame gets the same bytes. The cache is
    deliberately tiny: this is a live view, not a gallery.
    """

    def __init__(self, share_path: str = "", cache_size: int = 3) -> None:
        self.share_path = share_path
        self._cache: dict[tuple, PreviewImage] = {}
        self._order: list[tuple] = []
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def scan(self) -> Optional[dict]:
        """Cheap poll: the identity of the newest frame, without reading it."""
        if not self.share_path:
            return None
        path = latest_share_file(self.share_path)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return {
            "path": str(path),
            "filename": path.name,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        }

    def render_latest(self, options: StretchOptions) -> PreviewImage:
        if not self.share_path:
            raise PreviewError("no image share is configured")
        found = self.scan()
        if found is None:
            raise PreviewError(f"no FITS files under {self.share_path}")
        return self.render(Path(found["path"]), found["mtime"], options)

    def render(self, path: Path, mtime: float, options: StretchOptions) -> PreviewImage:
        key = (str(path), mtime, options.key())
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit

        image = render_fits(path, options)
        with self._lock:
            self._cache[key] = image
            self._order.append(key)
            while len(self._order) > self._cache_size:
                self._cache.pop(self._order.pop(0), None)
        return image
