"""
FastAPI application: REST for control, SSE for telemetry.

Auth deserves a word. Neither NINA's API nor PHD2 has any authentication, and
this UI can stop a running sequence, so this process is the only place a check
can happen. Binding to a non-loopback address therefore requires a shared
secret; `Config.require_token` refuses to start otherwise. On loopback the
token is optional, because reaching the socket already means local access.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import Config, load_config
from ..hub import sse_format
from ..nina.events import TppaSession
from ..nina.rest import NinaError, NinaUnavailable
from ..phd2.client import Phd2Disconnected, Phd2Error, Settle
from ..runtime import Runtime

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"


# ── request models ─────────────────────────────────────────────────────


class ParamUpdate(BaseModel):
    axis: str = Field(pattern="^(ra|dec)$")
    param: str
    value: float


class DitherRequest(BaseModel):
    pixels: float = Field(default=3.0, gt=0, le=25)
    ra_only: bool = False
    settle_pixels: float = Field(default=1.5, gt=0, le=25)
    settle_time: float = Field(default=8.0, ge=0, le=120)
    settle_timeout: float = Field(default=40.0, ge=1, le=600)


class TuningUpdate(BaseModel):
    enabled: Optional[bool] = None
    mode: Optional[str] = Field(default=None, pattern="^(off|suggest|auto)$")


class TppaRequest(BaseModel):
    manual_mode: bool = False
    target_distance: Optional[int] = None
    move_rate: Optional[int] = None
    east_direction: Optional[bool] = None
    start_from_current_position: Optional[bool] = None
    filter: Optional[str] = None
    exposure_time: Optional[float] = None
    gain: Optional[int] = None
    binning: Optional[int] = None


def create_app(
    config: Optional[Config] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> FastAPI:
    """
    Build the application.

    `should_stop` lets long-lived streaming endpoints notice that the server is
    shutting down. It is normally wired to `uvicorn.Server.should_exit`; when
    omitted (tests) the stream simply runs until the client disconnects.
    """
    config = config or load_config()
    token = config.require_token()

    @contextlib.asynccontextmanager
    async def lifespan(instance: FastAPI):
        rt = Runtime(config=config)
        instance.state.runtime = rt
        await rt.start()
        try:
            yield
        finally:
            instance.state.runtime = None
            await rt.stop()

    app = FastAPI(title="AstroController", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.runtime = None
    app.state.should_stop = should_stop or (lambda: False)

    async def require_auth(request: Request) -> None:
        """
        Shared-secret check for anything that can change the rig's behaviour.

        Accepts either an `X-Auth-Token` header or a `token` query parameter --
        the query form exists because `EventSource` cannot set headers.
        """
        if token is None:
            return
        supplied = request.headers.get("X-Auth-Token") or request.query_params.get("token")
        if supplied != token:
            raise HTTPException(status_code=401, detail="invalid or missing token")

    def runtime() -> Runtime:
        rt = app.state.runtime
        if rt is None:
            raise HTTPException(status_code=503, detail="runtime is not started")
        return rt

    # ── state and stream ───────────────────────────────────────────────

    @app.get("/api/state", dependencies=[Depends(require_auth)])
    async def get_state() -> dict:
        return runtime().snapshot()

    @app.get("/api/stream", dependencies=[Depends(require_auth)])
    async def stream(request: Request) -> StreamingResponse:
        rt = runtime()

        async def events():
            sub = rt.hub.subscribe()
            last_ping = time.monotonic()
            try:
                # Every connection opens with a full snapshot, so no client
                # ever depends on having seen a continuous history.
                yield sse_format({"type": "snapshot", "data": rt.snapshot()})
                while True:
                    # Uvicorn's graceful shutdown waits for open connections
                    # *before* it runs lifespan shutdown, so this stream has to
                    # notice Ctrl+C itself. Without it the server hangs on
                    # "Waiting for connections to close" for as long as a
                    # browser tab stays open.
                    if app.state.should_stop():
                        return
                    if await request.is_disconnected():
                        return
                    try:
                        # Short wait so shutdown is noticed within a second;
                        # the keepalive still only goes out every 15s.
                        message = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        now = time.monotonic()
                        if now - last_ping >= 15.0:
                            last_ping = now
                            yield ": ping\n\n"
                        continue
                    if message.get("type") == "resync":
                        message = {"type": "snapshot", "data": rt.snapshot()}
                    yield sse_format(message)
            except asyncio.CancelledError:
                raise
            finally:
                rt.hub.unsubscribe(sub)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/health")
    async def health() -> dict:
        rt = app.state.runtime
        return {
            "ok": rt is not None,
            "tasks": rt.supervisor.snapshot() if rt else [],
        }

    # ── sequence control ───────────────────────────────────────────────

    @app.post("/api/sequence/{action}", dependencies=[Depends(require_auth)])
    async def sequence_action(action: str) -> dict:
        if action not in ("start", "stop", "skip", "reset"):
            raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
        rt = runtime()
        method = getattr(rt.rest, f"sequence_{action}")
        try:
            result = await method()
        except (NinaError, NinaUnavailable) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await rt._refresh_sequence()
        return {"ok": True, "action": action, "result": result}

    # ── guiding control ────────────────────────────────────────────────

    @app.post("/api/guiding/start", dependencies=[Depends(require_auth)])
    async def guiding_start(recalibrate: bool = Query(default=False)) -> dict:
        rt = runtime()
        try:
            result = await rt.phd2.guide(recalibrate=recalibrate)
        except (Phd2Error, Phd2Disconnected, asyncio.TimeoutError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": result.ok, "settle": result.__dict__}

    @app.post("/api/guiding/stop", dependencies=[Depends(require_auth)])
    async def guiding_stop() -> dict:
        return await _phd2_call(runtime().phd2.stop_capture())

    @app.post("/api/guiding/loop", dependencies=[Depends(require_auth)])
    async def guiding_loop() -> dict:
        return await _phd2_call(runtime().phd2.loop())

    @app.post("/api/guiding/pause", dependencies=[Depends(require_auth)])
    async def guiding_pause(paused: bool = Query(default=True)) -> dict:
        return await _phd2_call(runtime().phd2.set_paused(paused))

    @app.post("/api/guiding/dither", dependencies=[Depends(require_auth)])
    async def guiding_dither(body: DitherRequest) -> dict:
        rt = runtime()
        settle = Settle(
            pixels=body.settle_pixels,
            time=body.settle_time,
            timeout=body.settle_timeout,
        )
        try:
            result = await rt.phd2.dither(body.pixels, body.ra_only, settle)
        except (Phd2Error, Phd2Disconnected, asyncio.TimeoutError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": result.ok, "settle": result.__dict__}

    @app.post("/api/guiding/clear-calibration", dependencies=[Depends(require_auth)])
    async def clear_calibration(which: str = Query(default="both")) -> dict:
        return await _phd2_call(runtime().phd2.clear_calibration(which))

    @app.post("/api/guiding/param", dependencies=[Depends(require_auth)])
    async def set_param(body: ParamUpdate) -> dict:
        """
        Manual parameter edit.

        Deliberately bypasses the advisor's budget guardrails -- this is the
        user's own decision -- but it still invalidates any measurement in
        flight, because the trial was measuring a different change.
        """
        rt = runtime()
        bound = rt.config.bound_for(body.axis, body.param)
        if bound is None:
            raise HTTPException(
                status_code=400,
                detail=f"{body.axis}.{body.param} is not a tunable parameter",
            )
        resolved = bound.resolve(rt.phd2.state.pixel_scale)
        value = resolved.clamp(body.value)
        try:
            applied = await rt.phd2.set_algo_param(body.axis, body.param, value)
        except (Phd2Error, Phd2Disconnected) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        rt.trials.invalidate("manual parameter change")
        await rt._close_and_reopen_epoch("manual_change")
        return {"ok": True, "applied": applied, "requested": body.value}

    # ── advisor control ────────────────────────────────────────────────

    @app.get("/api/advisor", dependencies=[Depends(require_auth)])
    async def advisor_status() -> dict:
        return runtime().advisor.status()

    @app.post("/api/advisor/tuning", dependencies=[Depends(require_auth)])
    async def set_tuning(body: TuningUpdate) -> dict:
        advisor = runtime().advisor
        if body.enabled is not None:
            advisor.enabled = body.enabled
            log.warning("auto-apply %s by user", "enabled" if body.enabled else "disabled")
        if body.mode is not None:
            advisor.mode = body.mode
            log.warning("tuning mode set to %s by user", body.mode)
        return advisor.status()

    @app.post("/api/advisor/revert-last", dependencies=[Depends(require_auth)])
    async def revert_last() -> dict:
        result = await runtime().actuator.revert_last()
        if not result.ok:
            raise HTTPException(status_code=400, detail=result.error or "revert failed")
        return result.as_dict()

    @app.post("/api/advisor/revert-all", dependencies=[Depends(require_auth)])
    async def revert_all() -> dict:
        result = await runtime().actuator.revert_all()
        if not result.ok:
            raise HTTPException(status_code=400, detail=result.error or "revert failed")
        return result.as_dict()

    # ── polar alignment ────────────────────────────────────────────────

    @app.post("/api/tppa/start", dependencies=[Depends(require_auth)])
    async def tppa_start(body: TppaRequest) -> dict:
        rt = runtime()
        if rt.tppa and rt.tppa.running:
            raise HTTPException(status_code=409, detail="an alignment is already running")

        async def on_update(_event: str, payload: dict) -> None:
            rt.hub.update("tppa", {"running": True, **payload})

        rt.tppa = TppaSession(rt.config.nina.ws_base, on_update)
        options: dict[str, Any] = {}
        if body.manual_mode:
            options["ManualMode"] = True
        for key, value in (
            ("TargetDistance", body.target_distance),
            ("MoveRate", body.move_rate),
            ("EastDirection", body.east_direction),
            ("StartFromCurrentPosition", body.start_from_current_position),
            ("Filter", body.filter),
            ("ExposureTime", body.exposure_time),
            ("Gain", body.gain),
            ("Binning", body.binning),
        ):
            if value is not None:
                options[key] = value
        try:
            await rt.tppa.start(**options)
        except Exception as exc:  # noqa: BLE001 - the TPPA plugin may be absent
            rt.tppa = None
            raise HTTPException(
                status_code=502,
                detail=f"could not start polar alignment (is the TPPA plugin installed?): {exc}",
            ) from exc
        return {"ok": True}

    @app.post("/api/tppa/{action}", dependencies=[Depends(require_auth)])
    async def tppa_action(action: str) -> dict:
        if action not in ("stop", "pause", "resume"):
            raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
        rt = runtime()
        if not rt.tppa:
            raise HTTPException(status_code=409, detail="no alignment is running")
        if action == "stop":
            await rt.tppa.stop()
            rt.hub.update("tppa", {"running": False})
        else:
            await rt.tppa.send(f"{action}-alignment")
        return {"ok": True, "action": action}

    # ── images and logs ────────────────────────────────────────────────

    @app.get("/api/image/{index}", dependencies=[Depends(require_auth)])
    async def image(index: int, scale: float = Query(default=0.4, gt=0, le=1)) -> Response:
        try:
            data = await runtime().rest.image_bytes(index, scale=scale)
        except (NinaError, NinaUnavailable) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(content=data, media_type="image/jpeg")

    @app.get("/api/logs", dependencies=[Depends(require_auth)])
    async def logs(lines: int = Query(default=100, ge=1, le=1000),
                   level: str = Query(default="INFO")) -> dict:
        try:
            return {"lines": await runtime().rest.logs(lines, level)}
        except (NinaError, NinaUnavailable) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── static UI ──────────────────────────────────────────────────────

    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC / "index.html")

    return app


async def _phd2_call(awaitable) -> dict:
    try:
        await awaitable
    except (Phd2Error, Phd2Disconnected, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


def app() -> FastAPI:
    """
    Factory for ``uvicorn astrocontroller.server.app:app --factory``.

    A factory rather than a module-level instance so that importing this module
    -- in tests, or for `--help` -- never triggers config loading or the
    non-loopback token check.
    """
    return create_app()
