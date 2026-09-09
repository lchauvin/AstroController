"""
Image handling: the PNG encoder, the FITS stretch, the guide-star crop, and
the equipment summariser.

numpy and astropy are optional dependencies, so the FITS tests skip rather than
fail when they are absent -- the app itself degrades the same way.
"""

from __future__ import annotations

import base64
import struct
import zlib

import pytest

from astrocontroller.imaging import preview
from astrocontroller.imaging.png import encode_png
from astrocontroller.imaging.preview import StretchOptions
from astrocontroller.imaging.star import StarImageError, decode_star_image, render_star_png
from astrocontroller.nina.equipment import summarize

np = pytest.importorskip("numpy")


# ── PNG encoder ────────────────────────────────────────────────────────


def _decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """Minimal reader, enough to prove the encoder round-trips."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    header = None
    idat = b""
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF, f"bad CRC on {tag!r}"
        if tag == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            idat += payload
        offset += 12 + length
    assert header is not None
    return header[0], header[1], header[3], zlib.decompress(idat)


def test_grayscale_round_trips():
    array = np.arange(12, dtype=np.uint8).reshape(3, 4)
    width, height, colour, raw = _decode_png(encode_png(array))
    assert (width, height, colour) == (4, 3, 0)
    # Every scanline is prefixed with filter byte 0.
    assert raw == b"".join(b"\x00" + bytes(row) for row in array)


def test_rgb_round_trips():
    array = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    width, height, colour, raw = _decode_png(encode_png(array))
    assert (width, height, colour) == (3, 2, 2)
    assert len(raw) == height * (width * 3 + 1)


def test_an_unsupported_shape_is_rejected():
    with pytest.raises(ValueError, match="expected"):
        encode_png(np.zeros((2, 2, 2), dtype=np.uint8))


# ── stretch ────────────────────────────────────────────────────────────


def _sky_frame(width=200, height=150, sky=900.0, noise=20.0):
    rng = np.random.default_rng(7)
    frame = rng.normal(sky, noise, (height, width)).astype(np.float32)
    # A few saturated cores, which is exactly what a max-based white point
    # would let ruin the stretch.
    frame[10, 10] = frame[40, 90] = 60000.0
    return frame


def test_the_sky_lands_near_the_target_background():
    frame = _sky_frame()
    out = preview._autostretch(frame, StretchOptions(target_background=0.25))
    assert out.dtype == np.uint8
    # Median of the result should sit near 0.25 * 255.
    assert 45 < float(np.median(out)) < 85


def test_saturated_cores_do_not_flatten_the_frame():
    frame = _sky_frame()
    stretched = preview._autostretch(frame, StretchOptions())
    # With a max-based white point the sky would collapse to zero; with the
    # percentile it keeps real contrast.
    assert float(np.std(stretched)) > 8


def test_a_flat_frame_does_not_divide_by_zero():
    frame = np.full((20, 20), 1234.0, dtype=np.float32)
    out = preview._autostretch(frame, StretchOptions())
    assert out.shape == (20, 20)
    assert np.isfinite(out).all()


def test_invert_flips_the_result():
    frame = _sky_frame()
    normal = preview._autostretch(frame, StretchOptions())
    inverted = preview._autostretch(frame, StretchOptions(invert=True))
    assert float(np.mean(inverted)) > float(np.mean(normal))


def test_binning_averages_blocks():
    array = np.arange(16, dtype=np.float32).reshape(4, 4)
    binned = preview._bin_down(array, 2)
    assert binned.shape == (2, 2)
    assert binned[0, 0] == pytest.approx((0 + 1 + 4 + 5) / 4)


def test_superpixel_debayer_picks_the_right_quadrants():
    # RGGB: red top-left, blue bottom-right, green on the diagonal.
    cell = np.array([[10.0, 20.0], [30.0, 40.0]], dtype=np.float32)
    rgb = preview._superpixel(np.tile(cell, (2, 2)), "RGGB")
    assert rgb.shape == (2, 2, 3)
    assert rgb[0, 0, 0] == 10.0
    assert rgb[0, 0, 1] == 25.0
    assert rgb[0, 0, 2] == 40.0


def test_an_unknown_bayer_pattern_falls_back_to_mono():
    data = np.ones((4, 4), dtype=np.float32)
    assert preview._superpixel(data, "XYZW").ndim == 2


# ── the whole FITS path ────────────────────────────────────────────────


def test_render_fits_produces_a_png(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    path = tmp_path / "light.fits"
    header = fits.Header()
    header["OBJECT"] = "M31"
    header["FILTER"] = "Ha"
    header["EXPOSURE"] = 300.0
    header["ROWORDER"] = "TOP-DOWN"
    fits.PrimaryHDU(
        data=_sky_frame(600, 400).astype(np.uint16), header=header
    ).writeto(path)

    image = preview.render_fits(path, StretchOptions(max_width=300))
    assert image.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert image.meta["target"] == "M31"
    assert image.meta["filter"] == "Ha"
    assert image.meta["bin_factor"] == 2
    assert image.meta["width"] == 300


def test_a_file_that_is_not_fits_reports_rather_than_raises(tmp_path):
    pytest.importorskip("astropy.io.fits")
    path = tmp_path / "broken.fits"
    path.write_bytes(b"not a fits file at all")
    with pytest.raises(preview.PreviewError, match="cannot read"):
        preview.render_fits(path, StretchOptions())


def test_latest_share_file_picks_the_newest(tmp_path):
    import os
    import time

    old = tmp_path / "a.fits"
    new = tmp_path / "sub" / "b.fits"
    new.parent.mkdir()
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    (tmp_path / "notes.txt").write_bytes(b"z")

    stale = time.time() - 600
    os.utime(old, (stale, stale))
    os.utime(new, (stale + 60, stale + 60))

    assert preview.latest_share_file(tmp_path) == new


def test_a_frame_still_landing_is_skipped(tmp_path):
    # Written just now: a file mid-copy is unreadable in too many ways to
    # parse, so it waits for the next poll instead.
    (tmp_path / "fresh.fits").write_bytes(b"x")
    assert preview.latest_share_file(tmp_path) is None


def test_an_absent_share_is_not_an_error(tmp_path):
    assert preview.latest_share_file(tmp_path / "nope") is None


def test_the_previewer_caches_by_file_and_options(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    path = tmp_path / "light.fits"
    fits.PrimaryHDU(data=_sky_frame(120, 90).astype(np.uint16)).writeto(path)

    previewer = preview.SharePreviewer(str(tmp_path))
    options = StretchOptions(max_width=100)
    first = previewer.render(path, 1.0, options)
    assert previewer.render(path, 1.0, options) is first
    # A different mtime is a different frame, even at the same path.
    assert previewer.render(path, 2.0, options) is not first


# ── PHD2 guide star ────────────────────────────────────────────────────


def _star_payload(size=15, peak=20000):
    ys, xs = np.mgrid[0:size, 0:size]
    centre = (size - 1) / 2
    data = 500 + peak * np.exp(-(((xs - centre) ** 2 + (ys - centre) ** 2) / 4.0))
    pixels = np.clip(data, 0, 65535).astype("<u2")
    return {
        "frame": 42,
        "width": size,
        "height": size,
        "star_pos": [centre, centre],
        "pixels": base64.b64encode(pixels.tobytes()).decode("ascii"),
    }


def test_a_star_image_decodes_to_the_right_shape():
    array, meta = decode_star_image(_star_payload(21))
    assert array.shape == (21, 21)
    assert array.dtype == np.uint8
    assert meta["frame"] == 42
    assert meta["star_x"] == 10.0
    # The core is the brightest thing in the crop.
    assert array[10, 10] == 255


def test_a_truncated_star_image_is_rejected():
    payload = _star_payload(15)
    payload["width"] = 31
    with pytest.raises(StarImageError, match="short"):
        decode_star_image(payload)


def test_an_empty_star_response_is_rejected():
    with pytest.raises(StarImageError, match="no star image"):
        decode_star_image({"width": 0, "height": 0})


def test_render_star_png_returns_png_and_metadata():
    png, meta = render_star_png(_star_payload())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert meta["width"] == 15


# ── equipment summary ──────────────────────────────────────────────────


def test_connected_devices_get_a_detail_line():
    rows = summarize(
        {
            "camera": {"Connected": True, "Name": "ASI2600", "Temperature": -10.0,
                       "CoolerPower": 43.0},
            "focuser": {"Connected": True, "Name": "EAF", "Position": 18500},
            "filterwheel": {"Connected": True, "Name": "EFW",
                            "SelectedFilter": {"Name": "Ha"}},
        }
    )
    by_key = {r["key"]: r for r in rows}
    assert by_key["camera"]["connected"] is True
    assert "cooler 43%" in by_key["camera"]["detail"]
    assert "step 18500" in by_key["focuser"]["detail"]
    assert by_key["filterwheel"]["detail"] == "Ha"


def test_field_names_are_matched_case_insensitively():
    # The plugin has changed its casing between versions; a summary that
    # depended on one spelling would silently go blank after an update.
    rows = summarize({"mount": {"connected": True, "name": "EQ6", "altitude": 58.4}})
    assert rows[0]["connected"] is True
    assert "alt 58°" in rows[0]["detail"]


def test_an_unconfigured_accessory_is_dropped_not_shown_as_broken():
    rows = summarize(
        {
            "camera": {"Connected": True, "Name": "ASI2600"},
            "dome": {"Connected": False, "Error": "dome is not configured"},
        }
    )
    assert {r["key"] for r in rows} == {"camera"}


def test_a_core_device_that_is_missing_is_still_reported():
    # A camera NINA cannot reach is the thing you got out of bed for.
    rows = summarize({"camera": {"Connected": False, "Error": "connection refused"}})
    assert rows[0]["connected"] is False
    assert rows[0]["error"] == "connection refused"


def test_a_device_absent_from_the_payload_is_omitted():
    assert summarize({}) == []


def test_a_malformed_payload_still_reports_its_connection_state():
    rows = summarize({"mount": {"Connected": True, "Name": "EQ6", "Altitude": "east"}})
    assert rows[0]["connected"] is True
    assert isinstance(rows[0]["detail"], str)


def test_a_degenerate_axis_is_treated_as_mono(tmp_path):
    # (1, h, w) is a mono frame with a spare axis, not a one-channel colour
    # image; taking it for colour would slice the frame to ribbons.
    fits = pytest.importorskip("astropy.io.fits")
    path = tmp_path / "mono.fits"
    fits.PrimaryHDU(data=_sky_frame(120, 90).astype(np.uint16)[None, ...]).writeto(path)
    image = preview.render_fits(path, StretchOptions(max_width=120))
    assert image.meta["colour"] is False
    assert (image.meta["width"], image.meta["height"]) == (120, 90)


def test_a_colour_fits_renders_as_rgb(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    path = tmp_path / "rgb.fits"
    cube = np.stack([_sky_frame(120, 90) for _ in range(3)]).astype(np.uint16)
    fits.PrimaryHDU(data=cube).writeto(path)
    image = preview.render_fits(path, StretchOptions(max_width=120))
    assert image.meta["colour"] is True


def test_a_bottom_up_frame_is_flipped(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    frame = np.zeros((40, 40), dtype=np.uint16)
    frame[:5] = 60000  # a bright band on the first stored row

    def render(roworder: str):
        path = tmp_path / f"{roworder}.fits"
        header = fits.Header()
        header["ROWORDER"] = roworder
        fits.PrimaryHDU(data=frame, header=header).writeto(path, overwrite=True)
        return preview.render_fits(path, StretchOptions(max_width=40))

    top = render("TOP-DOWN")
    bottom = render("BOTTOM-UP")
    assert top.png != bottom.png
