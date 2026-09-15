"""
The settings surface behind the UI's Settings page.

Two jobs, deliberately kept apart:

* **Describe** the effective configuration as a flat list of typed fields the
  browser can render without knowing anything about the dataclasses.
* **Patch** `astrocontroller.toml` in place when the user changes one.

The patcher rewrites individual ``key = value`` lines inside their sections and
leaves everything else -- comments, blank lines, ordering, sections it does not
know about -- byte for byte. Regenerating the file from the parsed config would
be a third of the code and would silently delete the comments explaining why
the observatory PC is at .200, which is exactly the note somebody needs at 2am
six months from now.

Not everything can take effect on a running process. Each field declares
whether it is `live` (the value is read fresh on each use, so patching the
in-memory config is enough) or needs a restart. The UI says which.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .config import CONFIG_NAME, Config, ConfigError, load_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    """One editable setting, described well enough for the UI to render it."""

    section: str
    key: str
    label: str
    kind: str
    """'text' | 'number' | 'bool' | 'choice'"""
    help: str = ""
    live: bool = False
    """True when a running process picks the new value up without a restart."""
    choices: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    placeholder: str = ""

    @property
    def path(self) -> str:
        return f"{self.section}.{self.key}"


# The whitelist. Anything not named here cannot be written through the API --
# `tuning.bounds` in particular is an array of tables that guards the mount's
# safety envelope, and it stays a deliberate text-editor decision.
FIELDS: tuple[Field, ...] = (
    # -- connections ----------------------------------------------------
    Field("nina", "host", "NINA host", "text",
          "IP or hostname of the PC running N.I.N.A.", placeholder="192.168.2.200"),
    Field("nina", "port", "NINA API port", "number",
          "Port of the Advanced API plugin.", minimum=1, maximum=65535, step=1),
    Field("nina", "poll_interval_s", "Sequence poll", "number",
          "Seconds between sequence-tree and equipment polls. NINA emits no "
          "per-step event, so this is how fast the tree updates.",
          live=True, minimum=2, maximum=120, step=1),
    Field("nina", "timeout_s", "NINA timeout", "number",
          "Seconds before a NINA request is given up on.",
          minimum=1, maximum=120, step=1),
    Field("phd2", "host", "PHD2 host", "text",
          "Usually the same machine as NINA.", placeholder="192.168.2.200"),
    Field("phd2", "instance", "PHD2 instance", "number",
          "Instance number, not the port: instance 1 listens on 4400.",
          minimum=1, maximum=8, step=1),

    # -- site -----------------------------------------------------------
    Field("site", "latitude", "Latitude", "number", live=True,
          help="Leave empty to take the site from NINA's profile.",
          minimum=-90, maximum=90, step=0.0001),
    Field("site", "longitude", "Longitude", "number", live=True,
          help="Degrees east of Greenwich; negative for the Americas.",
          minimum=-180, maximum=180, step=0.0001),
    Field("site", "elevation_m", "Elevation", "number", live=True,
          help="Metres above sea level.", minimum=-500, maximum=6000, step=1),

    # -- images ---------------------------------------------------------
    Field("images", "share_path", "Image share", "text", live=True,
          help="A folder this machine can read that NINA writes frames into "
               "(an SMB share works). Used for the live frame preview.",
          placeholder="G:/Astro/.Download"),
    Field("guide_camera", "enabled", "Guide field dumps", "bool", live=True,
          help="Ask PHD2 to save the guide camera frame as a FITS on a timer "
               "and show it on the dashboard. PHD2 has to write into "
               "<share>/<subdir>; each prompted file is deleted once the next "
               "has landed."),
    Field("guide_camera", "interval_s", "Dump interval", "number",
          "Seconds between PHD2 save_image calls. PHD2's own save is not "
          "instant, so below ~4s the panel would strobe.",
          live=True, minimum=4, maximum=120, step=1),
    Field("guide_camera", "share_subdir", "Guide subfolder", "text", live=True,
          help="Subfolder of the image share where PHD2's saves land.",
          placeholder="guide"),

    # -- weather --------------------------------------------------------
    Field("weather", "enabled", "Forecast", "bool", live=True,
          help="Fetch cloud, wind and dew point from Open-Meteo."),
    Field("weather", "poll_interval_s", "Forecast interval", "number",
          "Seconds between forecast refreshes.",
          live=True, minimum=300, maximum=7200, step=60),
    Field("weather", "forecast_days", "Forecast days", "number", live=True,
          help="How far ahead to fetch.", minimum=1, maximum=7, step=1),

    # -- advisor --------------------------------------------------------
    Field("tuning", "mode", "Tuning mode", "choice", live=True,
          help="'suggest' surfaces proposals without touching PHD2. "
               "'auto' applies them, still gated by the arm switch.",
          choices=("off", "suggest", "auto")),
    Field("tuning", "enabled", "Auto-apply armed", "bool", live=True,
          help="The kill switch. Off means nothing is ever written to PHD2."),
    Field("tuning", "max_changes_per_hour", "Changes per hour", "number",
          "Hard budget on parameter changes.",
          live=True, minimum=0, maximum=20, step=1),
    Field("tuning", "max_changes_per_session", "Changes per session", "number",
          "Hard budget for the whole night.",
          live=True, minimum=0, maximum=100, step=1),
    Field("tuning", "target_rms_arcsec", "Target RMS", "number",
          "Guiding good enough to stop tuning, in arcseconds.",
          live=True, minimum=0.1, maximum=3.0, step=0.05),
    Field("tuning", "actionable_rms_ratio", "Engage above", "number",
          "Only act when RMS exceeds this multiple of the best known for "
          "tonight's conditions.", live=True, minimum=1.0, maximum=3.0, step=0.05),
    Field("tuning", "auto_revert_ratio", "Auto-revert at", "number",
          "Revert automatically when the after-window is this much worse.",
          live=True, minimum=0.02, maximum=1.0, step=0.01),

    # -- model ----------------------------------------------------------
    Field("llm", "model", "Model", "text",
          "'provider/model-id'. Providers: ollama, openrouter, openai, anthropic.",
          live=True, placeholder="ollama/llama3.1:8b"),
    Field("llm", "ollama_url", "Ollama URL", "text",
          "Only used when the provider is ollama.",
          live=True, placeholder="http://localhost:11434"),
    Field("llm", "context_profile", "Context size", "choice",
          help="'small' trims the prompt for 7-8B local models.",
          choices=("small", "large")),
    Field("llm", "min_seconds_between_calls", "Model cooldown", "number",
          "Never call the model more often than this.",
          live=True, minimum=0, maximum=7200, step=30),

    # -- server ---------------------------------------------------------
    Field("server", "host", "Bind address", "text",
          "127.0.0.1 for this machine only. 0.0.0.0 to reach it from a phone "
          "-- which also requires a token in the environment.",
          placeholder="127.0.0.1"),
    Field("server", "port", "Bind port", "number",
          "", minimum=1, maximum=65535, step=1),
    Field("storage", "db_path", "Database", "text",
          "Where the learning store lives."),
)

FIELDS_BY_PATH: dict[str, Field] = {f.path: f for f in FIELDS}

GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("connections", "Connections",
     ("nina.host", "nina.port", "phd2.host", "phd2.instance",
      "nina.poll_interval_s", "nina.timeout_s")),
    ("site", "Observing site", ("site.latitude", "site.longitude", "site.elevation_m")),
    ("images", "Images",
     ("images.share_path", "guide_camera.enabled", "guide_camera.interval_s",
      "guide_camera.share_subdir")),
    ("weather", "Weather",
     ("weather.enabled", "weather.poll_interval_s", "weather.forecast_days")),
    ("advisor", "Advisor",
     ("tuning.mode", "tuning.enabled", "tuning.max_changes_per_hour",
      "tuning.max_changes_per_session", "tuning.target_rms_arcsec",
      "tuning.actionable_rms_ratio", "tuning.auto_revert_ratio")),
    ("model", "Language model",
     ("llm.model", "llm.ollama_url", "llm.context_profile",
      "llm.min_seconds_between_calls")),
    ("server", "Server", ("server.host", "server.port", "storage.db_path")),
)


def describe(config: Config) -> dict:
    """The whole settings page as data: groups, fields, and current values."""
    groups = []
    for key, title, paths in GROUPS:
        fields = []
        for path in paths:
            field = FIELDS_BY_PATH[path]
            section = getattr(config, field.section)
            fields.append(
                {
                    "path": path,
                    "section": field.section,
                    "key": field.key,
                    "label": field.label,
                    "kind": field.kind,
                    "help": field.help,
                    "live": field.live,
                    "choices": list(field.choices),
                    "min": field.minimum,
                    "max": field.maximum,
                    "step": field.step,
                    "placeholder": field.placeholder,
                    "value": getattr(section, field.key, None),
                }
            )
        groups.append({"key": key, "title": title, "fields": fields})

    return {
        "groups": groups,
        "source_path": config.source_path,
        "writable": not config.simulated,
        "simulated": config.simulated,
        "bounds": [
            {
                "axis": b.axis,
                "param": b.param,
                "lo": b.lo,
                "hi": b.hi,
                "max_delta": b.max_delta,
                "cooldown_s": b.cooldown_s,
                "lo_arcsec": b.lo_arcsec,
                "hi_arcsec": b.hi_arcsec,
            }
            for b in config.tuning.bounds
        ],
    }


# -- coercion -----------------------------------------------------------


def coerce(field: Field, value: Any) -> Any:
    """Turn a JSON value into what the dataclass field expects, or raise."""
    if field.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        raise ConfigError(f"{field.path} expects true or false")

    if field.kind == "number":
        if value in (None, ""):
            # Only the optional site coordinates may be cleared.
            if field.section == "site" and field.key in ("latitude", "longitude"):
                return None
            raise ConfigError(f"{field.path} cannot be empty")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{field.path} must be a number") from exc
        if field.minimum is not None and number < field.minimum:
            raise ConfigError(f"{field.path} must be at least {field.minimum}")
        if field.maximum is not None and number > field.maximum:
            raise ConfigError(f"{field.path} must be at most {field.maximum}")
        if field.step is not None and float(field.step).is_integer():
            return int(round(number))
        return number

    text = "" if value is None else str(value).strip()
    if field.kind == "choice" and text not in field.choices:
        raise ConfigError(
            f"{field.path} must be one of {', '.join(field.choices)}"
        )
    if field.kind == "text" and not text and field.key not in ("share_path", "share_subdir"):
        raise ConfigError(f"{field.path} cannot be empty")
    return text


def apply_to(config: Config, patch: dict[str, Any]) -> dict[str, Any]:
    """
    Validate a patch and write it into a Config, returning the coerced values.

    The config is mutated only after every field has coerced successfully, so a
    rejected patch leaves the running process exactly as it was.
    """
    coerced: dict[str, Any] = {}
    for path, value in patch.items():
        field = FIELDS_BY_PATH.get(path)
        if field is None:
            raise ConfigError(f"{path} is not an editable setting")
        coerced[path] = coerce(field, value)

    for path, value in coerced.items():
        field = FIELDS_BY_PATH[path]
        setattr(getattr(config, field.section), field.key, value)

    config.validate()
    return coerced


def restart_required(paths: list[str]) -> list[str]:
    """Which of these settings will not take effect until the process restarts."""
    return sorted(
        {
            FIELDS_BY_PATH[path].label
            for path in paths
            if path in FIELDS_BY_PATH and not FIELDS_BY_PATH[path].live
        }
    )


# -- writing the file ---------------------------------------------------


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_SECTION = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(?:#.*)?$")


def _assignment(key: str) -> re.Pattern:
    # Only a bare `key = ...` at the start of a line counts. A commented-out
    # `# key = ...` is documentation, and overwriting it would both lose the
    # comment and leave the real setting untouched.
    return re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*)(.*)$")


def patch_toml(text: str, updates: dict[str, Any]) -> str:
    """
    Rewrite `key = value` lines in place, adding sections and keys as needed.

    Array-of-table headers (`[[tuning.bounds]]`) end the plain `[tuning]`
    section, so anything after one is left alone -- appending a key there would
    silently attach it to the wrong table.
    """
    lines = text.splitlines()
    remaining = dict(updates)

    current: Optional[str] = None
    in_array_table = False
    # Last line index that still belongs to each top-level section, so a new
    # key lands at the end of its own section rather than at the end of file.
    section_end: dict[str, int] = {}

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[["):
            in_array_table = True
            continue
        match = _SECTION.match(line)
        if match:
            current = match.group(1)
            in_array_table = False
            section_end[current] = index
            continue
        if current is None or in_array_table:
            continue

        # Anchor new keys to the last line with content on it. Anchoring to a
        # blank line instead would insert the key immediately before the next
        # section header and swallow the blank line separating them.
        if stripped:
            section_end[current] = index
        for path in list(remaining):
            section, _, key = path.partition(".")
            if section != current:
                continue
            found = _assignment(key).match(line)
            if not found:
                continue
            value = remaining.pop(path)
            if value is None:
                lines[index] = (
                    f"{found.group(1)}# {key}{found.group(2)}{found.group(3)}"
                )
            else:
                trailing = _trailing_comment(found.group(3))
                lines[index] = (
                    f"{found.group(1)}{key}{found.group(2)}"
                    f"{format_value(value)}{trailing}"
                )
            break

    # Whatever is left has no line yet: append it inside its section, creating
    # the section header if the file has never had one.
    for path, value in remaining.items():
        if value is None:
            continue
        section, _, key = path.partition(".")
        entry = f"{key} = {format_value(value)}"
        if section in section_end:
            at = section_end[section] + 1
            lines.insert(at, entry)
            section_end = {
                name: (index + 1 if index >= at else index)
                for name, index in section_end.items()
            }
            section_end[section] = at
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{section}]")
            lines.append(entry)
            section_end[section] = len(lines) - 1

    return "\n".join(lines) + "\n"


def _trailing_comment(value_part: str) -> str:
    """Preserve an inline `# ...` comment that followed the old value."""
    marker = value_part.find("#")
    if marker < 0:
        return ""
    # A '#' inside a quoted string is not a comment.
    if value_part.count('"', 0, marker) % 2:
        return ""
    return "  " + value_part[marker:].strip()


def config_path(config: Config) -> Path:
    """Where a save should go, whether or not a file exists yet."""
    if config.source_path:
        return Path(config.source_path)
    return Path.cwd() / CONFIG_NAME


def save(config: Config, updates: dict[str, Any], *, loader: Callable = load_config) -> Path:
    """
    Write `updates` into the config file, keeping a backup of the old one.

    The rewritten text is parsed back before it replaces anything: a patch that
    would produce a file the process cannot start from is rejected here rather
    than at the next launch, in the dark.
    """
    if config.simulated:
        # --fake rewrote the host/port fields to point at the in-process fakes.
        # Writing those back would replace the observatory's real addresses
        # with a loopback port that exists only inside a dead process.
        raise ConfigError(
            "running in simulation mode (--fake); settings are read-only here "
            "because the connection fields point at the simulator"
        )
    path = config_path(config)
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    patched = patch_toml(original, updates)

    scratch = path.with_suffix(path.suffix + ".new")
    scratch.write_text(patched, encoding="utf-8")
    try:
        loader(str(scratch))
    except ConfigError:
        scratch.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        scratch.unlink(missing_ok=True)
        raise ConfigError(f"the rewritten config did not parse: {exc}") from exc

    if original:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    scratch.replace(path)
    config.source_path = str(path)
    log.warning("configuration saved to %s (%s)", path, ", ".join(sorted(updates)))
    return path
