"""
Turn NINA's per-device `/equipment/{device}/info` payloads into display rows.

Every device answers with a differently shaped object and the field names have
moved between plugin versions, so nothing here indexes a payload directly:
`_pick` looks a key up case-insensitively and tolerates its absence. A device
whose detail line cannot be built still reports its connection state, which is
the part the dashboard actually needs.

The rows are ordered by how much it matters that the device is missing at 2am:
camera and mount first, accessories after.
"""

from __future__ import annotations

from typing import Any, Optional

# (api name, label, icon). The API name is what goes in the URL; `guider` is
# NINA's own guider abstraction, which is not the same thing as our direct PHD2
# connection -- both appear, and it is useful when they disagree.
DEVICES: tuple[tuple[str, str, str], ...] = (
    ("camera", "Camera", "camera"),
    ("mount", "Mount", "mount"),
    ("focuser", "Focuser", "focuser"),
    ("filterwheel", "Filter wheel", "filter"),
    ("guider", "Guider", "guider"),
    ("rotator", "Rotator", "rotator"),
    ("dome", "Dome", "dome"),
    ("switch", "Switch", "switch"),
    ("flatdevice", "Flat panel", "flat"),
    ("weather", "Weather", "weather"),
    ("safetymonitor", "Safety", "safety"),
)

DEVICE_NAMES: tuple[str, ...] = tuple(name for name, _label, _icon in DEVICES)


def _pick(payload: Any, *keys: str) -> Any:
    """Case-insensitive lookup across several candidate key spellings."""
    if not isinstance(payload, dict):
        return None
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _number(value: Any, digits: int = 1) -> Optional[str]:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return None


def _join(*parts: Optional[str]) -> str:
    return " · ".join(p for p in parts if p)


def _camera(payload: dict) -> str:
    temperature = _number(_pick(payload, "Temperature", "CCDTemperature"))
    target = _number(_pick(payload, "TemperatureSetPoint", "SetTemperature"))
    cooling = None
    if temperature is not None:
        # The setpoint only earns its space while the camera is still getting
        # there; repeating "-10.0°C → -10.0°C" says nothing and truncates the
        # cooler percentage that does.
        drifting = target is not None and abs(float(target) - float(temperature)) > 0.6
        cooling = f"{temperature}°C" + (f" → {target}°C" if drifting else "")
    power = _number(_pick(payload, "CoolerPower"), 0)
    return _join(
        cooling,
        f"cooler {power}%" if power is not None else None,
        "exposing" if _pick(payload, "IsExposing") is True else None,
    )


def _mount(payload: dict) -> str:
    altitude = _number(_pick(payload, "Altitude"), 0)
    pier = _pick(payload, "SideOfPier")
    if isinstance(pier, str):
        pier = pier.replace("pier", "").strip().lower() or None
    flip = _pick(payload, "TimeToMeridianFlip", "HoursToMeridian")
    flip_text = None
    if isinstance(flip, (int, float)) and flip > 0:
        flip_text = f"flip in {int(flip * 60)}m" if flip < 12 else None
    return _join(
        f"alt {altitude}°" if altitude is not None else None,
        f"{pier} side" if pier else None,
        "parked" if _pick(payload, "AtPark") is True else None,
        "tracking" if _pick(payload, "TrackingEnabled") is True else None,
        flip_text,
    )


def _focuser(payload: dict) -> str:
    position = _pick(payload, "Position")
    temperature = _number(_pick(payload, "Temperature"))
    return _join(
        f"step {int(position)}" if isinstance(position, (int, float)) else None,
        f"{temperature}°C" if temperature is not None else None,
        "moving" if _pick(payload, "IsMoving") is True else None,
    )


def _filterwheel(payload: dict) -> str:
    selected = _pick(payload, "SelectedFilter")
    name = _pick(selected, "Name") if isinstance(selected, dict) else selected
    return _join(
        str(name) if name else None,
        "moving" if _pick(payload, "IsMoving") is True else None,
    )


def _guider(payload: dict) -> str:
    rms = _pick(payload, "RMSError")
    total = _pick(rms, "Total") if isinstance(rms, dict) else None
    arcsec = _pick(total, "Arcseconds") if isinstance(total, dict) else total
    value = _number(arcsec, 2)
    return _join(
        str(_pick(payload, "State") or "") or None,
        f'{value}" RMS' if value is not None else None,
    )


def _rotator(payload: dict) -> str:
    angle = _number(_pick(payload, "Position", "MechanicalPosition"))
    return _join(
        f"{angle}°" if angle is not None else None,
        "moving" if _pick(payload, "IsMoving") is True else None,
    )


def _dome(payload: dict) -> str:
    return _join(
        str(_pick(payload, "ShutterStatus") or "") or None,
        "slewing" if _pick(payload, "Slewing") is True else None,
        "parked" if _pick(payload, "AtPark") is True else None,
    )


def _flatdevice(payload: dict) -> str:
    brightness = _pick(payload, "Brightness")
    return _join(
        str(_pick(payload, "CoverState") or "") or None,
        "light on" if _pick(payload, "LightOn") is True else None,
        f"{int(brightness)}" if isinstance(brightness, (int, float)) else None,
    )


def _weather(payload: dict) -> str:
    cloud = _number(_pick(payload, "CloudCover"), 0)
    temperature = _number(_pick(payload, "Temperature"))
    wind = _number(_pick(payload, "WindSpeed"))
    return _join(
        f"{cloud}% cloud" if cloud is not None else None,
        f"{temperature}°C" if temperature is not None else None,
        f"wind {wind}" if wind is not None else None,
    )


def _safety(payload: dict) -> str:
    safe = _pick(payload, "IsSafe")
    if safe is True:
        return "safe"
    if safe is False:
        return "UNSAFE"
    return ""


def _switch(payload: Any) -> str:
    writable = _pick(payload, "WritableSwitches") or []
    readonly = _pick(payload, "ReadonlySwitches") or []
    count = len(writable) + len(readonly) if isinstance(writable, list) else 0
    return f"{count} switches" if count else ""


DETAIL_BUILDERS = {
    "camera": _camera,
    "mount": _mount,
    "focuser": _focuser,
    "filterwheel": _filterwheel,
    "guider": _guider,
    "rotator": _rotator,
    "dome": _dome,
    "flatdevice": _flatdevice,
    "weather": _weather,
    "safetymonitor": _safety,
    "switch": _switch,
}


def summarize(equipment: dict[str, Any]) -> list[dict]:
    """
    One row per device: connection state, device name, and a short detail line.

    Devices NINA does not know about at all are dropped rather than shown as
    permanently red -- a rig without a rotator should not look broken. A device
    that answers "not connected" *is* shown, because that is a real state
    somebody may need to go and fix.
    """
    rows: list[dict] = []
    for name, label, icon in DEVICES:
        payload = equipment.get(name)
        if payload is None:
            continue

        error = _pick(payload, "Error") if isinstance(payload, dict) else None
        connected = bool(_pick(payload, "Connected") is True)

        # A device the profile has never had configured answers with an error
        # rather than a payload. Showing every one of those as a red light
        # would bury the two that actually matter.
        if not connected and error and name not in ("camera", "mount", "guider"):
            continue

        detail = ""
        if connected:
            builder = DETAIL_BUILDERS.get(name)
            if builder is not None:
                try:
                    detail = builder(payload)
                except Exception:  # noqa: BLE001 - a odd payload is not fatal
                    detail = ""

        rows.append(
            {
                "key": name,
                "label": label,
                "icon": icon,
                "connected": connected,
                "name": _pick(payload, "Name", "DeviceId") or None,
                "detail": detail,
                "error": str(error) if error and not connected else None,
            }
        )
    return rows
