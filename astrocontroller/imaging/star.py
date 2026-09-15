"""
Render PHD2's guide-star crop as a PNG.

`get_star_image` hands back a small square of raw 16-bit pixels centred on the
star PHD2 is actually guiding on. It is the single most diagnostic image in the
whole system: a round star means guiding is fine, an elongated one means the
mount is dragging, a faint smear means the sky went. None of that is visible in
an RMS number.

The crop is 15-33 pixels across, so it is served at native size and blown up by
the browser with nearest-neighbour scaling -- upscaling here would only make
the response bigger without adding information.
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from .png import encode_png


class StarImageError(RuntimeError):
    """The response could not be turned into an image."""


def decode_star_image(payload: dict) -> tuple[Any, dict]:
    """Decode PHD2's `get_star_image` response into a uint8 array plus metadata."""
    import numpy as np

    width = int(payload.get("width") or 0)
    height = int(payload.get("height") or 0)
    encoded = payload.get("pixels")
    if width <= 0 or height <= 0 or not isinstance(encoded, str):
        raise StarImageError("PHD2 returned no star image")

    try:
        raw = base64.b64decode(encoded, validate=False)
    except (ValueError, TypeError) as exc:
        raise StarImageError(f"undecodable star image: {exc}") from exc

    expected = width * height * 2
    if len(raw) < expected:
        raise StarImageError(
            f"star image is short: {len(raw)} bytes for {width}x{height}"
        )

    pixels = np.frombuffer(raw[:expected], dtype="<u2").astype(np.float32)
    pixels = pixels.reshape(height, width)

    # Percentile stretch, then a square root. The frame is one star on sky
    # background, so there is nothing for a midtone curve to rescue -- but a
    # linear ramp puts everything except the core in the bottom few percent,
    # and the wings are where elongation and bad focus actually show up.
    # Percentile endpoints rather than min/max: one hot pixel would otherwise
    # pin the white point and crush the star to mid-grey.
    lo, hi = np.percentile(pixels, (0.1, 99.9))
    span = hi - lo
    if span <= 0:
        scaled = np.zeros_like(pixels)
    else:
        scaled = np.sqrt((pixels - lo) / span)
    eight_bit = (np.clip(scaled, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    position = payload.get("star_pos") or []
    meta = {
        "width": width,
        "height": height,
        "frame": payload.get("frame"),
        "star_x": _number(position[0]) if len(position) > 0 else None,
        "star_y": _number(position[1]) if len(position) > 1 else None,
        "peak": float(pixels.max()),
        "background": lo,
    }
    return eight_bit, meta


def render_star_png(payload: dict) -> tuple[bytes, dict]:
    array, meta = decode_star_image(payload)
    return encode_png(array, level=9), meta


def render_field_png(payload: dict) -> tuple[bytes, dict]:
    """
    Render a large guide-camera crop (the lock region up to PHD2's 255px cap)
    for the dashboard's field view.

    Same pipeline as the star crop except the stretch is a log curve: at this
    size the frame contains sky background and dim field stars alongside the
    guide star, and a square root leaves everything but the core invisible.
    """
    import numpy as np

    from .png import encode_png

    width = int(payload.get("width") or 0)
    height = int(payload.get("height") or 0)
    encoded = payload.get("pixels")
    if width <= 0 or height <= 0 or not isinstance(encoded, str):
        raise StarImageError("PHD2 returned no guide image")

    try:
        raw = base64.b64decode(encoded, validate=False)
    except (ValueError, TypeError) as exc:
        raise StarImageError(f"undecodable guide image: {exc}") from exc

    expected = width * height * 2
    if len(raw) < expected:
        raise StarImageError(
            f"guide image is short: {len(raw)} bytes for {width}x{height}"
        )

    pixels = np.frombuffer(raw[:expected], dtype="<u2").astype(np.float32)
    pixels = pixels.reshape(height, width)

    lo, hi = np.percentile(pixels, (0.1, 99.95))
    span = hi - lo
    if span <= 0:
        scaled = np.zeros_like(pixels)
    else:
        scaled = np.log1p((pixels - lo) / span * 99.0) / np.log1p(99.0)
    eight_bit = (np.clip(scaled, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    position = payload.get("star_pos") or []
    meta = {
        "width": width,
        "height": height,
        "frame": payload.get("frame"),
        "star_x": _number(position[0]) if len(position) > 0 else None,
        "star_y": _number(position[1]) if len(position) > 1 else None,
        "peak": float(pixels.max()),
        "background": lo,
    }
    return encode_png(eight_bit, level=6), meta


def _number(value: Any) -> Optional[float]:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None
