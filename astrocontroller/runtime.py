"""
The runtime: owns every background task and the shared state they feed.

Task inventory, all supervised with backoff:

* ``nina_ws``    -- websocket events, with a full REST resync on every connect
* ``nina_poll``  -- sequence tree and equipment (NINA emits no per-step event)
* ``phd2``       -- guide steps and RPC
* ``weather``    -- Open-Meteo plus locally computed moon
* ``advisor``    -- the tuning tick
* ``sampler``    -- one condition row per minute, plus epoch bookkeeping
* ``preview``    -- notices new frames landing on the image share
* ``coalescer``  -- rate-limits high-frequency SSE topics
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .advisor.actuator import Actuator
from .advisor.loop import Advisor
from .config import Config
from .hub import TelemetryHub
from .imaging.preview import SharePreviewer
from .imaging.preview import available as preview_available
from .learning.store import EpochRecord, LearningStore, params_hash
from .metrics.conditions import ConditionVector, bucket_key, build_condition_vector
from .metrics.exclusions import ExclusionTracker
from .metrics.guiding import GuideBuffer
from .metrics.trials import TrialTracker
from .nina.equipment import summarize as summarize_equipment
from .nina.events import NinaEventListener, TppaSession
from .nina.rest import ImageStats, NinaRest, NinaUnavailable
from .nina.sequence import parse_sequence
from .phd2.client import Phd2Client
from .quality.frames import FrameAnalyzer
from .supervise import Backoff, Supervisor
from .weather.moon import moon_info
from .weather.openmeteo import fetch_forecast

log = logging.getLogger(__name__)


@dataclass
class Epoch:
    """A stretch during which the guiding parameters did not change."""

    start_mono: float
    start_utc: str
    params: dict
    bucket: str

    def duration(self, now: float) -> float:
        return now - self.start_mono


@dataclass
class Runtime:
    config: Config
    hub: TelemetryHub = field(default_factory=TelemetryHub)
    supervisor: Supervisor = field(default_factory=Supervisor)

    def __post_init__(self) -> None:
        cfg = self.config
        self.buffer = GuideBuffer()
        self.exclusions = ExclusionTracker(
            settle_guard_s=cfg.tuning.settle_guard_s,
            dither_guard_s=cfg.tuning.dither_guard_s,
            flip_guard_s=cfg.tuning.flip_guard_s,
            star_lost_guard_s=cfg.tuning.star_lost_guard_s,
            param_change_lag_s=cfg.tuning.settle_lag_s,
        )
        self.trials = TrialTracker(
            before_window_s=cfg.tuning.before_window_s,
            after_window_s=cfg.tuning.after_window_s,
            settle_lag_s=cfg.tuning.settle_lag_s,
            min_samples=cfg.tuning.min_samples,
            min_effect_sigma=cfg.tuning.min_effect_sigma,
            hfd_confound_ratio=cfg.tuning.hfd_confound_ratio,
        )
        self.frames = FrameAnalyzer()

        self.rest = NinaRest(cfg.nina.rest_base, timeout=cfg.nina.timeout_s)
        self.phd2 = Phd2Client(
            cfg.phd2.host,
            cfg.phd2.instance,
            connect_timeout=cfg.phd2.connect_timeout_s,
            rpc_timeout=cfg.phd2.rpc_timeout_s,
            on_event=self._on_phd2_event,
            on_connect=self._on_phd2_connect,
            on_disconnect=self._on_phd2_disconnect,
        )
        self.nina_ws = NinaEventListener(
            cfg.nina.ws_base, self._on_nina_event, on_connect=self._resync_nina
        )
        self.tppa: Optional[TppaSession] = None

        self.store = LearningStore(cfg.storage.db_path)
        self.rig_id: Optional[int] = None
        self.session_id: Optional[int] = None

        self.actuator = Actuator(
            self.phd2, cfg.tuning, self.trials, store=self.store
        )
        self.advisor = Advisor(cfg, self.actuator, store=self.store)

        self.site: Optional[tuple[float, float, float]] = None
        if cfg.site.latitude is not None and cfg.site.longitude is not None:
            self.site = (cfg.site.latitude, cfg.site.longitude, cfg.site.elevation_m)

        self.previewer = SharePreviewer(cfg.images.share_path)
        self._preview_wake = asyncio.Event()

        # Guide-camera FITS dump: files this runtime itself prompted PHD2 to
        # write, newest first. Pruning deletes the previous prompt's file only
        # once its successor has landed, so a manual PHD2 save is never touched.
        self._prompted_guide_saves: deque[str] = deque(maxlen=8)
        self.guide_field: Optional[dict] = None

        self.epoch: Optional[Epoch] = None
        self.nina_flipping = False
        self.nina_autofocusing = False
        self.last_frame: Optional[ImageStats] = None
        self.recent_frames: list[dict] = []
        self._epoch_samples: list[tuple[float, float]] = []
        self._guide_activity: Optional[tuple[str, float]] = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        log.info(
            "starting AstroController: NINA %s, PHD2 %s:%d",
            self.config.nina.rest_base, self.config.phd2.host, self.config.phd2.port,
        )
        self.supervisor.spawn(
            "phd2", self._run_phd2, backoff=Backoff(maximum=30.0),
            manages_connection=True,
        )
        self.supervisor.spawn(
            "nina_ws", self.nina_ws.run, backoff=Backoff(maximum=30.0),
            manages_connection=True,
        )
        self.supervisor.spawn("nina_poll", self._poll_nina, manages_connection=True)
        self.supervisor.spawn(
            "weather", self._poll_weather, backoff=Backoff(maximum=300.0),
            manages_connection=True,
        )
        self.supervisor.spawn("advisor", self._run_advisor)
        self.supervisor.spawn("guide_saver", self._run_guide_saver)
        self.supervisor.spawn("sampler", self._run_sampler)
        self.supervisor.spawn("preview", self._watch_share)
        self.supervisor.spawn("coalescer", self.hub.run_coalescer)

    async def stop(self) -> None:
        await self._close_epoch("shutdown")
        if self.session_id is not None:
            with contextlib.suppress(Exception):
                self.store.end_session(self.session_id)
        if self.tppa:
            await self.tppa.aclose()
        await self.supervisor.shutdown()
        with contextlib.suppress(Exception):
            await self.rest.aclose()
        with contextlib.suppress(Exception):
            self.store.close()
        log.info("AstroController stopped")

    async def _run_phd2(self) -> None:
        try:
            await self.phd2.run()
        finally:
            self.supervisor.mark_unhealthy("phd2", "disconnected")

    # ── PHD2 ───────────────────────────────────────────────────────────

    async def _on_phd2_connect(self) -> None:
        self.supervisor.mark_healthy("phd2")
        self.exclusions.on_reconnect()
        self.buffer.set_pixel_scale(self.phd2.state.pixel_scale)
        await self._ensure_session()
        if self.actuator.baseline_snapshot is None and self.phd2.state.algo_params:
            self.actuator.capture_baseline(self.phd2.state.algo_params)
        self._open_epoch("connect")
        self._publish_phd2()

    async def _on_phd2_disconnect(self) -> None:
        self.supervisor.mark_unhealthy("phd2", "disconnected")
        # A disconnect is an unknown gap: any measurement spanning it is void.
        self.exclusions.on_disconnect()
        self.trials.invalidate("PHD2 disconnected")
        await self._close_epoch("phd2_disconnect")
        self._publish_phd2()

    def _on_phd2_event(self, msg: dict) -> None:
        event = str(msg.get("Event", ""))
        self.exclusions.on_phd2_event(event)

        if event == "GuideStep":
            sample = self.buffer.add_guide_step(msg)
            if sample is not None:
                self._remember_activity("guiding")
                self.hub.publish_coalesced("guiding", self._guiding_payload())
            return

        # Disturbances the dashboard calls out by name on the guide-star card.
        # Exclusion intervals expire by wall clock; the activity stamps expire
        # the same way but carry a friendlier label.
        if event in ("GuidingDithered", "SettleBegin"):
            if event == "GuidingDithered":
                self._remember_activity("dithering", for_s=self.config.tuning.dither_guard_s)
        elif event in ("StarLost", "LockPositionLost"):
            self._remember_activity("star lost", for_s=self.config.tuning.star_lost_guard_s)

        if event in ("ConfigurationChange", "GuidingStopped", "StartGuiding"):
            asyncio.create_task(self._close_and_reopen_epoch(event.lower()))

        self._publish_phd2()

    def _publish_phd2(self) -> None:
        self.hub.update("phd2", self.phd2.state.as_dict())

    def _remember_activity(self, label: str, for_s: float = 3.0) -> None:
        """Stamp what the mount is doing so short states are visible.

        A dither or star loss is over in a frame, but the operator needs the
        word on screen long enough to notice -- so the payload carries an
        expiry instead of a boolean, and the UI treats a past expiry as
        'guiding'."""
        now = time.monotonic()
        self._guide_activity = (label, now + for_s)

    def _activity(self) -> Optional[str]:
        if not self._guide_activity:
            return None
        label, until = self._guide_activity
        return label if time.monotonic() < until else None

    def _guiding_payload(self) -> dict:
        stats = self.buffer.recent(300.0, self.exclusions.intervals)
        return {
            "rms": stats.as_dict() if stats else None,
            "graph": self.buffer.graph_series(600.0),
            "disturbed": self.exclusions.intervals.open_reasons,
            "activity": self._activity(),
        }

    # ── NINA ───────────────────────────────────────────────────────────

    async def _resync_nina(self) -> None:
        """
        A reconnect means missed events, so re-fetch authoritative state.

        The websocket carries change notifications; REST carries truth. Nothing
        here tries to replay history.
        """
        self.supervisor.mark_healthy("nina_ws")
        await self._refresh_sequence()
        await self._refresh_images()
        await self._ensure_site()

    async def _on_nina_event(self, event: str, payload: dict) -> None:
        self.exclusions.on_nina_event(event)

        if event == "MOUNT-BEFORE-FLIP":
            self.nina_flipping = True
            self.trials.invalidate("meridian flip")
            await self._close_epoch("meridian_flip")
        elif event == "MOUNT-AFTER-FLIP":
            self.nina_flipping = False
        elif event == "AUTOFOCUS-STARTING":
            self.nina_autofocusing = True
        elif event == "AUTOFOCUS-FINISHED":
            self.nina_autofocusing = False
        elif event == "IMAGE-SAVE":
            self._ingest_frame(payload.get("ImageStatistics") or {})
        elif event in ("SEQUENCE-STARTING", "SEQUENCE-FINISHED"):
            await self._refresh_sequence()

        self.hub.publish("nina_event", {"event": event, "at": time.time()})

    def _ingest_frame(self, raw: dict) -> None:
        stats = ImageStats.from_payload(raw)
        flags = self.frames.analyze(stats)
        self.last_frame = stats

        record = stats.as_dict()
        record["flags"] = flags.as_dict()
        record["baseline"] = self.frames.baseline(stats.filter)
        self.recent_frames.insert(0, record)
        del self.recent_frames[40:]

        if self.session_id is not None:
            with contextlib.suppress(Exception):
                self.store.add_frame(self.session_id, stats, flags.as_dict())

        self.hub.update("frames", self.recent_frames)
        self._preview_wake.set()
        if flags.any_flag:
            log.info("frame flagged: %s", ", ".join(flags.notes) or "see flags")

    async def _poll_nina(self) -> None:
        """Sequence progress is polled -- NINA emits no per-step event."""
        while True:
            try:
                await self._refresh_sequence()
                await self._refresh_equipment()
                self.supervisor.mark_healthy("nina_poll")
            except NinaUnavailable as exc:
                self.supervisor.mark_unhealthy("nina_poll", str(exc))
                self.hub.merge("nina", {"connected": False, "error": str(exc)})
            await asyncio.sleep(self.config.nina.poll_interval_s)

    async def _refresh_sequence(self) -> None:
        try:
            tree = parse_sequence(await self.rest.sequence_json())
        except NinaUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - an uninitialised sequencer is normal
            self.hub.update("sequence", {"available": False, "error": str(exc)})
            return
        self.hub.update("sequence", tree.as_dict())

    async def _refresh_equipment(self) -> None:
        equipment = await self.rest.all_equipment()
        self.hub.update(
            "nina",
            {
                "connected": True,
                "equipment": equipment,
                "devices": summarize_equipment(equipment),
            },
        )

    # -- image share ----------------------------------------------------

    async def _watch_share(self) -> None:
        """
        Notice new frames landing on the image share.

        Only the *identity* of the newest file is polled here -- name, mtime,
        size. Rendering happens on request in the HTTP handler, so a dashboard
        nobody has open costs one directory listing every few seconds and no
        network reads at all.

        A scan is also kicked off by IMAGE-SAVE, because a frame that appears
        one second after a poll should not wait for the next one.
        """
        reason = preview_available()
        last: Optional[tuple] = None
        while True:
            self.previewer.share_path = self.config.images.share_path
            share = self.previewer.share_path

            found = None
            if share and not reason:
                found = await asyncio.to_thread(self.previewer.scan)

            index = self.recent_frames[0].get("index") if self.recent_frames else None
            identity = (share, found and found["path"], found and found["mtime"], index)
            if identity != last:
                last = identity
                self.hub.update("preview", self._preview_payload(found, reason, index))

            # Woken by IMAGE-SAVE; otherwise a slow poll, since a sub is
            # minutes long and a directory listing over SMB is not free.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._preview_wake.wait(), timeout=8.0)
            self._preview_wake.clear()

    def _preview_payload(
        self, found: Optional[dict], reason: Optional[str], index: Optional[int]
    ) -> dict:
        """
        What the browser needs to decide whether to re-fetch the frame.

        `token` changes exactly when the image does, and is the whole
        cache-busting story: the browser appends it to the image URL and its
        own HTTP cache does the rest.
        """
        share = self.config.images.share_path
        if found is not None:
            return {
                "source": "share",
                "available": True,
                "share_path": share,
                "filename": found["filename"],
                "path": found["path"],
                "mtime": found["mtime"],
                "size": found["size"],
                "token": str(int(found["mtime"] * 1000)),
                "nina_index": index,
            }
        return {
            "source": "nina" if index is not None else None,
            "available": index is not None,
            "share_path": share,
            "reason": reason
            or (
                f"no FITS files under {share}"
                if share
                else "no image share configured -- set it in Settings"
            ),
            "nina_index": index,
            "token": str(index) if index is not None else None,
        }

    async def _refresh_images(self) -> None:
        with contextlib.suppress(Exception):
            history = await self.rest.image_history(all_images=True)
            for stats in history[-10:]:
                if stats.filename and all(
                    f.get("filename") != stats.filename for f in self.recent_frames
                ):
                    self._ingest_frame_stats(stats)

    def _ingest_frame_stats(self, stats: ImageStats) -> None:
        flags = self.frames.analyze(stats)
        record = stats.as_dict()
        record["flags"] = flags.as_dict()
        self.recent_frames.insert(0, record)
        del self.recent_frames[40:]
        self.last_frame = stats
        self.hub.update("frames", self.recent_frames)
        self._preview_wake.set()

    async def _ensure_site(self) -> None:
        if self.site is not None:
            return
        location = await self.rest.site_location()
        if location:
            self.site = location
            log.info("site from NINA profile: %.4f, %.4f", location[0], location[1])

    # ── weather ────────────────────────────────────────────────────────

    async def _poll_weather(self) -> None:
        while True:
            if not self.config.weather.enabled:
                await asyncio.sleep(3600)
                continue
            await self._ensure_site()
            if self.site is None:
                self.hub.update("weather", {"error": "observing site unknown"})
                await asyncio.sleep(120)
                continue

            lat, lon, elev = self.site
            forecast = await fetch_forecast(
                lat, lon, forecast_days=self.config.weather.forecast_days
            )
            self.hub.update("weather", forecast.as_dict())
            self.hub.update(
                "sky",
                moon_info(latitude=lat, longitude=lon, elevation_m=elev).as_dict(),
            )
            if forecast.error:
                self.supervisor.mark_unhealthy("weather", forecast.error)
            else:
                self.supervisor.mark_healthy("weather")
            await asyncio.sleep(self.config.weather.poll_interval_s)

    # ── conditions, epochs, sampling ───────────────────────────────────

    def conditions(self) -> ConditionVector:
        latest = self.buffer.latest
        stats = self.buffer.recent(300.0, self.exclusions.intervals)
        weather = (self.hub.state.get("weather") or {}).get("current") or {}
        sky = self.hub.state.get("sky") or {}
        mount = ((self.hub.state.get("nina") or {}).get("equipment") or {}).get(
            "mount"
        ) or {}
        frame = {
            "target": self.last_frame.target if self.last_frame else None,
            "filter": self.last_frame.filter if self.last_frame else None,
            "exposure_s": self.last_frame.exposure_s if self.last_frame else None,
        }
        return build_condition_vector(
            guide_hfd_px=latest.hfd_px if latest else None,
            pixel_scale=self.buffer.pixel_scale,
            guide_snr=stats.snr_med if stats else None,
            guide_star_mass=stats.star_mass_med if stats else None,
            rms_total=stats.rms_total if stats else None,
            rms_ra=stats.rms_ra if stats else None,
            rms_dec=stats.rms_dec if stats else None,
            mount=mount,
            weather={
                "wind_ms": weather.get("wind_ms"),
                "gust_ms": weather.get("gust_ms"),
                "temp_c": weather.get("temp_c"),
                "humidity_pct": weather.get("humidity"),
                "dewpoint_c": weather.get("dewpoint_c"),
                "cloud_pct": weather.get("cloud_total"),
            },
            sky={
                "moon_illum": sky.get("illumination"),
                "moon_sep_deg": sky.get("separation_deg"),
            },
            frame=frame,
        )

    async def _ensure_session(self) -> None:
        if self.session_id is not None:
            return
        equipment = self.phd2.state.equipment or {}
        self.rig_id = await asyncio.to_thread(
            self.store.ensure_rig,
            mount=_name(equipment.get("mount")),
            guide_camera=_name(equipment.get("camera")),
            main_camera=self.last_frame.camera_name if self.last_frame else None,
            focal_length_mm=self.last_frame.focal_length if self.last_frame else None,
            pixel_scale=self.phd2.state.pixel_scale,
            phd2_profile=self.phd2.state.profile_name,
        )
        self.session_id = await asyncio.to_thread(
            self.store.start_session,
            self.rig_id,
            site_lat=self.site[0] if self.site else None,
            site_lon=self.site[1] if self.site else None,
            baseline_params={
                f"{a}.{p}": v for (a, p), v in self.phd2.state.algo_params.items()
            },
        )
        self.actuator.session_id = self.session_id
        self.actuator.rig_id = self.rig_id
        self.advisor.session_id = self.session_id
        self.advisor.rig_id = self.rig_id
        log.info("session %s on rig %s", self.session_id, self.rig_id)

    def _open_epoch(self, reason: str) -> None:
        """
        Begin a stable-settings epoch, if there is anything worth recording.

        Nothing is opened until the seeing is known. Right after a connect
        there are no guide steps yet, so the bucket would be `see:?` -- an
        epoch filed under that is retrievable by nothing and would only dilute
        the store. The sampler opens one as soon as conditions arrive.
        """
        params = {
            f"{a}.{p}": v for (a, p), v in self.phd2.state.algo_params.items()
        }
        if not params:
            return
        bucket = bucket_key(self.conditions())
        if "see:?" in bucket:
            log.debug("epoch not opened (%s): conditions not known yet", reason)
            return
        self.epoch = Epoch(
            start_mono=time.monotonic(),
            start_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            params=params,
            bucket=bucket,
        )
        self._epoch_samples = []
        log.debug("epoch opened (%s) in bucket %s", reason, bucket)

    async def _close_epoch(self, reason: str) -> None:
        """
        Record a stretch of stable settings as evidence.

        Epochs shorter than the configured minimum are discarded: a two-minute
        run is not evidence, and storing it would let noise outvote real data.
        """
        epoch = self.epoch
        self.epoch = None
        if epoch is None or self.session_id is None or self.rig_id is None:
            return

        now = time.monotonic()
        duration = epoch.duration(now)
        stats = self.buffer.window(
            epoch.start_mono,
            now,
            exclusions=self.exclusions.intervals,
            min_samples=self.config.tuning.min_samples,
        )
        if stats is None or stats.usable_seconds < self.config.tuning.epoch_min_seconds:
            log.debug(
                "epoch discarded (%s): %.0fs wall, %s clean",
                reason, duration, stats.usable_seconds if stats else 0,
            )
            return

        cond = self.conditions()
        record = EpochRecord(
            session_id=self.session_id,
            rig_id=self.rig_id,
            start_utc=epoch.start_utc,
            end_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            params=epoch.params,
            bucket_key=epoch.bucket,
            close_reason=reason,
            usable_seconds=stats.usable_seconds,
            n_samples=stats.n,
            rms_total=stats.rms_total,
            rms_ra=stats.rms_ra,
            rms_dec=stats.rms_dec,
            guide_hfd_med=stats.hfd_med,
            altitude_med=cond.altitude_deg,
            conditions=cond.as_dict(),
        )
        await asyncio.to_thread(self.store.add_epoch, record)
        log.info(
            "epoch recorded (%s): %.0fs clean at %.2f\" in %s",
            reason, stats.usable_seconds, stats.rms_total, epoch.bucket,
        )

    async def _close_and_reopen_epoch(self, reason: str) -> None:
        await self._close_epoch(reason)
        self._open_epoch(reason)

    async def _run_sampler(self) -> None:
        """One condition row a minute, plus epoch boundary maintenance."""
        while True:
            await asyncio.sleep(60.0)
            if self.session_id is None:
                continue

            cond = self.conditions()
            stats = self.buffer.recent(300.0, self.exclusions.intervals)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.store.add_condition_sample,
                    self.session_id,
                    cond,
                    n_samples=stats.n if stats else None,
                    guide_hfd_px=self.buffer.latest.hfd_px if self.buffer.latest else None,
                    nina_hfr=self.last_frame.hfr if self.last_frame else None,
                    nina_stars=self.last_frame.stars if self.last_frame else None,
                    app_state=self.phd2.state.app_state,
                )

            self.exclusions.intervals.prune()
            if self.epoch is None and self.phd2.state.connected:
                # Conditions may only just have become known.
                self._open_epoch("conditions_ready")
            else:
                await self._maybe_roll_epoch(cond)

    async def _maybe_roll_epoch(self, cond: ConditionVector) -> None:
        """
        Close an epoch when it gets too long or the conditions change bucket.

        Bucket changes need hysteresis: seeing hovering exactly on a boundary
        would otherwise produce a stream of one-minute epochs that are all too
        short to record.
        """
        epoch = self.epoch
        if epoch is None:
            return
        now = time.monotonic()
        if epoch.duration(now) >= self.config.tuning.epoch_max_seconds:
            await self._close_and_reopen_epoch("max_duration")
            return

        current_bucket = bucket_key(cond)
        if current_bucket == epoch.bucket:
            self._epoch_samples = []
            return
        self._epoch_samples.append((now, 1.0))
        if len(self._epoch_samples) >= 3:
            await self._close_and_reopen_epoch("bucket_change")

    # ── advisor ────────────────────────────────────────────────────────

    async def _run_advisor(self) -> None:
        while True:
            await asyncio.sleep(self.config.tuning.tick_interval_s)
            try:
                result = await self.advisor.tick(
                    conditions=self.conditions(),
                    stats=self.buffer.recent(300.0, self.exclusions.intervals),
                    buffer=self.buffer,
                    exclusions=self.exclusions.intervals,
                    nina_flipping=self.nina_flipping,
                    nina_autofocusing=self.nina_autofocusing,
                )
                if result.action in ("baseline", "llm"):
                    await self._close_and_reopen_epoch("param_change")
                self.hub.update("advisor", self.advisor.status())
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                log.exception("advisor tick failed")

    # ── guide-camera FITS dump ─────────────────────────────────────────

    def _guide_share_dir(self) -> Optional[Path]:
        root = self.config.images.share_path
        if not root:
            return None
        return Path(root) / self.config.guide_camera.share_subdir

    async def _run_guide_saver(self) -> None:
        """
        Ask PHD2 to dump the guide frame as a FITS every `interval_s`.

        PHD2's event API has no full-frame call; `save_image` is the only way
        to one. The file lands in PHD2's image directory on the rig, and the
        dashboard reads it back over the share. Each file we prompted is
        deleted once its successor has landed, so the folder never accumulates
        -- a manual PHD2 save is never in the list and is never touched.
        """
        cfg = self.config.guide_camera
        while True:
            await asyncio.sleep(max(2.0, cfg.interval_s))
            if not cfg.enabled or not self.phd2.connected:
                continue
            # Saving from PHD2 while it is not looping produces nothing.
            if self.phd2.state.app_state not in ("Guiding", "Looping", "Paused"):
                continue
            try:
                result = await self.phd2.call("save_image", timeout=5.0)
            except Exception as exc:  # noqa: BLE001 - a refused save is a state, not a fault
                log.debug("guide save_image refused: %s", exc)
                continue
            await self._note_guide_save(result)

    def _resolve_guide_path(self, filename: str) -> Optional[Path]:
        """Where PHD2's save lands, as seen from *this* machine over the share."""
        share_dir = self._guide_share_dir()
        if share_dir is None:
            return None
        return share_dir / Path(filename).name

    async def _note_guide_save(self, result) -> None:
        """
        Record a prompted save and prune the one before it.

        PHD2 returns `{"filename": "<absolute path on the rig>"}`. The share
        is keyed off the basename only: the absolute path belongs to the rig's
        filesystem, not ours.
        """
        if not isinstance(result, dict):
            return
        filename = result.get("filename")
        if not filename:
            return

        local = self._resolve_guide_path(str(filename))
        if local is None:
            return
        self._prompted_guide_saves.append(str(local))

        # Wait a beat for the write to finish over the network, then publish.
        await asyncio.sleep(0.4)
        try:
            stat = await asyncio.to_thread(os.stat, local)
        except OSError:
            return

        self.guide_field = {
            "path": str(local),
            "filename": local.name,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "token": str(int(stat.st_mtime * 1000)),
        }
        self.hub.update("guide_image", self.guide_field)

        # Delete the previous prompt's file now that this one has landed.
        prompted = list(self._prompted_guide_saves)
        keep = prompted[-2:]  # the one just saved + the one before as overlap
        for old in prompted[:-2]:
            if old in keep:
                continue
            with contextlib.suppress(OSError):
                os.unlink(old)
        while True:
            await asyncio.sleep(self.config.tuning.tick_interval_s)
            try:
                result = await self.advisor.tick(
                    conditions=self.conditions(),
                    stats=self.buffer.recent(300.0, self.exclusions.intervals),
                    buffer=self.buffer,
                    exclusions=self.exclusions.intervals,
                    nina_flipping=self.nina_flipping,
                    nina_autofocusing=self.nina_autofocusing,
                )
                if result.action in ("baseline", "llm"):
                    await self._close_and_reopen_epoch("param_change")
                self.hub.update("advisor", self.advisor.status())
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                log.exception("advisor tick failed")

    # ── snapshot ───────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        self.hub.state["guiding"] = self._guiding_payload()
        self.hub.state["phd2"] = self.phd2.state.as_dict()
        self.hub.state["advisor"] = self.advisor.status()
        self.hub.state["health"] = self.supervisor.snapshot()
        if self.guide_field is not None:
            self.hub.state["guide_image"] = self.guide_field
        self.hub.state["session"] = {
            "session_id": self.session_id,
            "rig_id": self.rig_id,
            "site": self.site,
            "store": self.store.stats(),
        }
        if self.tppa:
            self.hub.state["tppa"] = {
                "running": self.tppa.running,
                **(self.tppa.last or {}),
            }
        return self.hub.snapshot()


def _name(entry) -> Optional[str]:
    if isinstance(entry, dict):
        return entry.get("name") or entry.get("Name")
    if isinstance(entry, str):
        return entry
    return None
