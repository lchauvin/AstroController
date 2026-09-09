"""
A minimal PNG encoder.

Pillow would do this in one line, but it is a compiled dependency and the only
thing needed here is "8-bit array in, PNG bytes out". zlib and struct are in the
standard library, the PNG container is four chunks long, and this keeps
`uv sync` on the observatory laptop free of a wheel that has to build.

Accepts either a 2-D array (grayscale) or a 3-D `(h, w, 3)` array (RGB) of
`uint8`. Anything else is the caller's bug.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

GRAY = 0
RGB = 2


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(array: Any, *, level: int = 6) -> bytes:
    """
    Encode a uint8 numpy array as a PNG.

    Every scanline is emitted with filter type 0 (None). Real encoders try the
    five filters per row and keep the cheapest; for stretched astronomical data
    -- which is noisy at the pixel level -- the filters win little and cost a
    pass over the image on every request.
    """
    import numpy as np

    data = np.ascontiguousarray(array, dtype=np.uint8)
    if data.ndim == 2:
        height, width = data.shape
        colour = GRAY
        channels = 1
    elif data.ndim == 3 and data.shape[2] == 3:
        height, width, _ = data.shape
        colour = RGB
        channels = 3
    else:
        raise ValueError(f"expected (h,w) or (h,w,3) uint8, got {data.shape}")

    # Prepend the per-row filter byte by building the raw stream in one
    # allocation: [0, row0..., 0, row1..., ...].
    stride = width * channels
    raw = np.zeros((height, stride + 1), dtype=np.uint8)
    raw[:, 1:] = data.reshape(height, stride)

    header = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw.tobytes(), level))
        + _chunk(b"IEND", b"")
    )
