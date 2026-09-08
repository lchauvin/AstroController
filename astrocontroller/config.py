"""
Configuration loading for AstroController.

TOML file with documented defaults, following the house pattern from
``astro_eval/config_loader.py`` and the strict-unknown-key style of
``D:/Python/AstroIntegration/gct/config.py``: a typo raises at startup rather
than silently reverting a setting to its default at 2am.

Secrets are never read from the TOML file -- API keys and the access token come
from the environment.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

CONFIG_NAME = "astrocontroller.toml"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ConfigError(ValueError):
    """Raised for a malformed configuration file or an unknown key."""


# ── sections ───────────────────────────────────────────────────────────


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 3005
    token_env: str = "ASTROCONTROLLER_TOKEN"
    """
    Env var holding the shared secret. A token is REQUIRED when `host` is not
    loopback: the UI can stop a sequence, so it must not be reachable by anyone
    who can find the port. See `require_token()`.
    """

    @property
    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    def token(self) -> Optional[str]:
        value = os.environ.get(self.token_env, "").strip()
        return value or None


@dataclass
class NinaConfig:
    host: str = "127.0.0.1"
    port: int = 1888
    """Port of the NINA 'Advanced API' plugin (its default is 1888)."""
    timeout_s: float = 10.0
    poll_interval_s: float = 10.0
    """
    Sequence progress is NOT pushed over the websocket -- NINA emits no
    per-step event -- so the step tree is polled at this interval.
    """

    @property
    def rest_base(self) -> str:
        return f"http://{self.host}:{self.port}/v2/api"

    @property
    def ws_base(self) -> str:
        return f"ws://{self.host}:{self.port}/v2"


@dataclass
class Phd2Config:
    host: str = "127.0.0.1"
    instance: int = 1
    """PHD2 instance number; the event server listens on 4400 + instance - 1."""
    rpc_timeout_s: float = 10.0
    connect_timeout_s: float = 5.0

    @property
    def port(self) -> int:
        return 4400 + self.instance - 1


@dataclass
class SiteConfig:
    """Observing site. When unset, it is derived from NINA's /profile/show."""

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    elevation_m: float = 0.0


@dataclass
class ImagesConfig:
    """
    Optional path to a readable copy of NINA's image directory (e.g. an SMB
    share). When set and readable, the astro-eval engine runs deep FITS
    analysis. When empty -- the normal case for a separate-machine deployment
    -- AstroController uses NINA's own per-frame statistics only.
    """

    share_path: str = ""
    max_analysis_seconds: float = 30.0


@dataclass
class WeatherConfig:
    enabled: bool = True
    poll_interval_s: int = 900
    forecast_days: int = 2


@dataclass
class LlmConfig:
    model: str = "ollama/llama3.1:8b"
    """'provider/model-id'. Providers: ollama, openrouter, openai, anthropic."""
    ollama_url: str = "http://localhost:11434"
    max_tokens: int = 1500
    timeout_s: float = 120.0
    context_profile: str = "small"
    """'small' for 7-8B local models, 'large' for cloud models."""
    min_seconds_between_calls: float = 600.0
    """Cost/latency guard: never call the model more often than this."""


@dataclass(frozen=True)
class ParamBound:
    """
    Hard limits for one tunable PHD2 guiding parameter.

    `minMove` is expressed in PIXELS by PHD2, but a sensible limit is angular.
    Setting `lo_arcsec`/`hi_arcsec` makes the bound scope-independent: it is
    converted with the guide camera's pixel scale at runtime.
    """

    axis: str
    param: str
    lo: float
    hi: float
    max_delta: float
    """Largest absolute change permitted in a single adjustment."""
    quantum: float = 0.01
    """Rounding step; a proposed change smaller than this is a no-op."""
    cooldown_s: float = 600.0
    lo_arcsec: Optional[float] = None
    hi_arcsec: Optional[float] = None

    def resolve(self, pixel_scale: Optional[float]) -> "ParamBound":
        """Return a copy with arcsec limits converted to pixels."""
        if pixel_scale is None or pixel_scale <= 0:
            return self
        if self.lo_arcsec is None and self.hi_arcsec is None:
            return self
        lo = self.lo_arcsec / pixel_scale if self.lo_arcsec is not None else self.lo
        hi = self.hi_arcsec / pixel_scale if self.hi_arcsec is not None else self.hi
        return ParamBound(
            axis=self.axis,
            param=self.param,
            lo=lo,
            hi=hi,
            max_delta=self.max_delta,
            quantum=self.quantum,
            cooldown_s=self.cooldown_s,
        )

    def clamp(self, value: float) -> float:
        return min(self.hi, max(self.lo, value))

    def quantize(self, value: float) -> float:
        if self.quantum <= 0:
            return value
        return round(round(value / self.quantum) * self.quantum, 6)

    def key(self) -> tuple[str, str]:
        return (self.axis, self.param)


DEFAULT_BOUNDS: tuple[ParamBound, ...] = (
    # Hysteresis is PHD2's RA default; ResistSwitch is its Dec default.
    # `fastSwitch` and `dec_guide_mode` are deliberately NOT tunable by the
    # model -- they are user-only.
    ParamBound("ra", "aggression", 0.40, 1.00, 0.10, 0.05, 600.0),
    ParamBound("ra", "hysteresis", 0.00, 0.30, 0.05, 0.05, 600.0),
    ParamBound("ra", "minMove", 0.05, 0.50, 0.05, 0.01, 600.0,
               lo_arcsec=0.10, hi_arcsec=0.70),
    ParamBound("dec", "aggression", 0.40, 1.00, 0.10, 0.05, 600.0),
    ParamBound("dec", "minMove", 0.05, 0.50, 0.05, 0.01, 600.0,
               lo_arcsec=0.10, hi_arcsec=0.70),
    # Lowpass2 exposes `aggressiveness` instead of `aggression`.
    ParamBound("ra", "aggressiveness", 0.40, 1.00, 0.10, 0.05, 600.0),
    ParamBound("dec", "aggressiveness", 0.40, 1.00, 0.10, 0.05, 600.0),
)

TUNING_MODES = ("off", "suggest", "auto")


@dataclass
class TuningConfig:
    mode: str = "suggest"
    """
    'off'     -- the advisor does not run.
    'suggest' -- proposals are surfaced in the UI but never applied.
    'auto'    -- guarded auto-apply (still gated by `enabled`).
    """
    enabled: bool = False
    """Global kill switch. Defaults OFF; auto-apply is opt-in from the UI."""

    tick_interval_s: float = 60.0
    max_changes_per_hour: int = 4
    max_changes_per_session: int = 12

    before_window_s: float = 300.0
    after_window_s: float = 300.0
    settle_lag_s: float = 30.0
    """Delay after a change before the 'after' window starts accumulating."""
    min_samples: int = 30
    min_stable_s: float = 180.0

    # Exclusion guards (seconds of telemetry discarded after each disturbance).
    settle_guard_s: float = 5.0
    dither_guard_s: float = 30.0
    flip_guard_s: float = 300.0
    star_lost_guard_s: float = 60.0
    global_cooldown_s: float = 300.0

    # Evidence thresholds.
    min_effect_sigma: float = 1.5
    """Below this, a measured difference is called 'neutral'."""
    hfd_confound_ratio: float = 0.20
    """Fractional change in guide-star HFD that marks a trial confounded."""
    auto_revert_ratio: float = 0.15
    """Auto-revert when the after-window is this much worse than before."""
    panic_rms_multiple: float = 3.0
    panic_window_s: float = 120.0

    # Learning / exploration.
    min_prior_support: float = 3.0
    """Weighted support needed before a learned baseline is trusted."""
    epoch_min_seconds: float = 300.0
    epoch_max_seconds: float = 1800.0
    explore_base_probability: float = 0.30
    explore_decay_trials: float = 8.0
    target_rms_arcsec: float = 0.60
    actionable_rms_ratio: float = 1.15
    """Only engage the advisor when RMS exceeds this multiple of best-known."""

    bounds: tuple[ParamBound, ...] = DEFAULT_BOUNDS


@dataclass
class StorageConfig:
    db_path: str = "astrocontroller.db"
    keep_raw_guidesteps: bool = False
    prune_conditions_after_days: int = 730


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    nina: NinaConfig = field(default_factory=NinaConfig)
    phd2: Phd2Config = field(default_factory=Phd2Config)
    site: SiteConfig = field(default_factory=SiteConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    tuning: TuningConfig = field(default_factory=TuningConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    source_path: Optional[str] = None
    """Path the config was loaded from; None when running on defaults."""

    def bound_for(self, axis: str, param: str) -> Optional[ParamBound]:
        for b in self.tuning.bounds:
            if b.axis == axis and b.param == param:
                return b
        return None

    def validate(self) -> None:
        """Cross-section checks that cannot be expressed per-field."""
        if self.tuning.mode not in TUNING_MODES:
            raise ConfigError(
                f"[tuning] mode must be one of {TUNING_MODES}, "
                f"got {self.tuning.mode!r}"
            )
        if self.llm.context_profile not in ("small", "large"):
            raise ConfigError(
                "[llm] context_profile must be 'small' or 'large', "
                f"got {self.llm.context_profile!r}"
            )
        if "/" not in self.llm.model:
            raise ConfigError(
                f"[llm] model must be 'provider/model-id', got {self.llm.model!r}"
            )
        if self.phd2.instance < 1:
            raise ConfigError("[phd2] instance must be >= 1")

    def require_token(self) -> Optional[str]:
        """
        Return the access token, raising if one is required but absent.

        Binding to a non-loopback address publishes a UI that can stop a
        running sequence. Neither NINA's API nor PHD2 has any authentication,
        so this process is the only place a check can happen.
        """
        token = self.server.token()
        if self.server.is_loopback:
            return token
        if not token:
            raise ConfigError(
                f"[server] host is {self.server.host!r} (not loopback), so a shared "
                f"secret is required.\n"
                f"Set the {self.server.token_env} environment variable, e.g.\n"
                f'  $env:{self.server.token_env} = '
                f'"{os.urandom(16).hex()}"\n'
                f"Or set host = \"127.0.0.1\" to listen on this machine only."
            )
        return token


# ── loading ────────────────────────────────────────────────────────────

# Top-level TOML section -> dataclass. Explicit rather than derived from type
# annotations, which are strings here because of `from __future__ import
# annotations` and would need runtime resolution to be useful.
SECTION_TYPES: dict[str, type] = {
    "server": ServerConfig,
    "nina": NinaConfig,
    "phd2": Phd2Config,
    "site": SiteConfig,
    "images": ImagesConfig,
    "weather": WeatherConfig,
    "llm": LlmConfig,
    "tuning": TuningConfig,
    "storage": StorageConfig,
}

# (owning dataclass name, field name) -> nested dataclass, for the same reason.
NESTED_TYPES: dict[tuple[str, str], type] = {
    ("Config", name): cls for name, cls in SECTION_TYPES.items()
}


def _build(cls: type, raw: Any, path: str) -> Any:
    """Instantiate a dataclass from a mapping, rejecting unknown keys."""
    if not isinstance(raw, dict):
        raise ConfigError(f"[{path}] expected a table, got {type(raw).__name__}")

    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(
            f"[{path}] unknown key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )

    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        # `from __future__ import annotations` makes field.type a string, so
        # nested dataclasses are resolved from an explicit registry rather than
        # by introspecting the annotation.
        nested = NESTED_TYPES.get((cls.__name__, name))
        if nested is not None:
            kwargs[name] = _build(nested, value, f"{path}.{name}")
        else:
            kwargs[name] = value
    return cls(**kwargs)


def _build_bounds(raw: Any) -> tuple[ParamBound, ...]:
    if not isinstance(raw, list):
        raise ConfigError("[tuning.bounds] expected an array of tables")
    out: list[ParamBound] = []
    seen: set[tuple[str, str]] = set()
    for i, entry in enumerate(raw):
        where = f"tuning.bounds[{i}]"
        bound = _build(ParamBound, entry, where)
        if bound.axis not in ("ra", "dec"):
            raise ConfigError(
                f"[{where}] axis must be 'ra' or 'dec', got {bound.axis!r}"
            )
        if bound.lo >= bound.hi:
            raise ConfigError(f"[{where}] lo ({bound.lo}) must be < hi ({bound.hi})")
        if bound.max_delta <= 0:
            raise ConfigError(f"[{where}] max_delta must be > 0")
        if bound.key() in seen:
            raise ConfigError(
                f"[{where}] duplicate bound for {bound.axis}/{bound.param}"
            )
        seen.add(bound.key())
        out.append(bound)
    return tuple(out)


def search_paths(explicit: Optional[str] = None) -> list[Path]:
    """Config search order: --config, cwd, project root, %APPDATA%."""
    if explicit:
        return [Path(explicit)]
    candidates = [
        Path.cwd() / CONFIG_NAME,
        Path(__file__).resolve().parent.parent / CONFIG_NAME,
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "AstroController" / CONFIG_NAME)
    return candidates


def load_config(explicit: Optional[str] = None) -> Config:
    """
    Load configuration, or return documented defaults when no file exists.

    Raises ConfigError on an unknown key, an invalid value, or an explicitly
    requested file that is missing.
    """
    for path in search_paths(explicit):
        if path.is_file():
            cfg = _parse(path)
            cfg.validate()
            return cfg

    if explicit:
        raise ConfigError(f"config file not found: {explicit}")
    log.info("no %s found; using built-in defaults", CONFIG_NAME)
    cfg = Config()
    cfg.validate()
    return cfg


def _parse(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc

    cfg = Config(source_path=str(path))
    unknown = sorted(set(raw) - set(SECTION_TYPES))
    if unknown:
        raise ConfigError(
            f"{path}: unknown section(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(sorted(SECTION_TYPES))}"
        )

    for name, value in raw.items():
        if name == "tuning":
            # tuning.bounds is an array of tables and needs its own handling.
            tuning_raw = dict(value)
            bounds_raw = tuning_raw.pop("bounds", None)
            tuning = _build(TuningConfig, tuning_raw, "tuning")
            if bounds_raw is not None:
                tuning.bounds = _build_bounds(bounds_raw)
            cfg.tuning = tuning
        else:
            setattr(cfg, name, _build(SECTION_TYPES[name], value, name))

    log.info("loaded config from %s", path)
    return cfg
