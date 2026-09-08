"""
A simulated observatory: fake PHD2 and fake NINA, in-process.

Started with ``astrocontroller --fake``. It exists so the entire stack -- guide
buffer, exclusion algebra, epochs, guardrails, advisor, SSE, UI -- can be
exercised in daylight, on a laptop, with no telescope attached. Nearly every
bug in this kind of system is a timing or reconnection bug, and those are
miserable to reproduce at 2am in a field.

The simulation is intentionally imperfect in useful ways: seeing wanders,
guiding responds (roughly) to the aggression and minMove parameters, dithers
happen on a schedule, and the star is occasionally lost. That gives the advisor
something real to react to and makes the exclusion masking observable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Imported at module scope on purpose. This module uses
# `from __future__ import annotations`, so FastAPI resolves endpoint
# annotations as strings against the *module* globals -- a function-local
# import leaves `WebSocket` unresolvable, and FastAPI silently downgrades the
# parameter to a query field, rejecting every connection with 403.
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


@dataclass
class SimState:
    """Shared physical truth behind both fake servers."""

    pixel_scale: float = 1.6
    seeing_arcsec: float = 2.0
    aggression: float = 0.70
    hysteresis: float = 0.10
    min_move_px: float = 0.15
    dec_aggression: float = 0.80
    dec_min_move_px: float = 0.18
    exposure_ms: int = 2000

    app_state: str = "Guiding"
    frame: int = 0
    altitude: float = 58.0
    azimuth: float = 142.0
    target: str = "M31"
    filter: str = "Ha"
    started: float = field(default_factory=time.monotonic)

    _ra_error: float = 0.0
    _dec_error: float = 0.0

    def ideal_aggression(self) -> float:
        """
        The 'right' answer the advisor should converge toward.

        Deliberately seeing-dependent: in poor seeing a lower aggression does
        better because the mount stops chasing atmospheric noise. This is what
        makes the learning store's condition bucketing meaningful rather than
        decorative.
        """
        return max(0.45, min(0.95, 1.05 - 0.18 * self.seeing_arcsec))

    def step(self) -> dict:
        """Advance one guide frame and produce a GuideStep payload."""
        self.frame += 1

        # Slow seeing drift plus occasional gusts.
        self.seeing_arcsec = max(
            0.9, min(5.0, self.seeing_arcsec + random.gauss(0, 0.03))
        )
        if random.random() < 0.01:
            self.seeing_arcsec += random.uniform(0.3, 1.0)

        # Atmospheric kick, scaled by seeing.
        noise = self.seeing_arcsec * 0.28
        self._ra_error += random.gauss(0, noise)
        self._dec_error += random.gauss(0, noise * 0.85)

        # Periodic error in RA, roughly a worm period.
        elapsed = time.monotonic() - self.started
        self._ra_error += 0.12 * math.sin(elapsed / 60.0)

        # Correction. Aggression far from ideal is penalised: too low leaves
        # drift uncorrected, too high overshoots and oscillates.
        penalty = 1.0 + 2.2 * abs(self.aggression - self.ideal_aggression())
        self._ra_error = self._apply(
            self._ra_error, self.aggression, self.min_move_px, penalty
        )
        self._dec_error = self._apply(
            self._dec_error, self.dec_aggression, self.dec_min_move_px, 1.0
        )

        return {
            "Event": "GuideStep",
            "Timestamp": time.time(),
            "Host": "sim",
            "Inst": 1,
            "Frame": self.frame,
            "Time": elapsed,
            "Mount": "Simulated Mount",
            "dx": self._ra_error / self.pixel_scale,
            "dy": self._dec_error / self.pixel_scale,
            "RADistanceRaw": self._ra_error / self.pixel_scale,
            "DECDistanceRaw": self._dec_error / self.pixel_scale,
            "RADuration": abs(self._ra_error) * 300,
            "RADirection": "East" if self._ra_error > 0 else "West",
            "DECDuration": abs(self._dec_error) * 300,
            "DECDirection": "North" if self._dec_error > 0 else "South",
            "StarMass": max(500.0, 5200 + random.gauss(0, 260)),
            "SNR": max(3.0, 27 - (self.seeing_arcsec - 2.0) * 5 + random.gauss(0, 1.6)),
            "HFD": self.seeing_arcsec / self.pixel_scale,
            "AvgDist": math.hypot(self._ra_error, self._dec_error) / self.pixel_scale,
            "RALimited": False,
            "DecLimited": False,
        }

    def _apply(
        self, error: float, aggression: float, min_move_px: float, penalty: float
    ) -> float:
        min_move_arcsec = min_move_px * self.pixel_scale
        if abs(error) < min_move_arcsec:
            return error  # inside the deadband; no correction issued
        correction = error * aggression / penalty
        return (error - correction) * (1 - self.hysteresis * 0.25)


class FakePhd2Server:
    """Speaks the real PHD2 wire protocol: JSON lines, CRLF, events + RPC."""

    def __init__(self, sim: SimState, host: str = "127.0.0.1", port: int = 0) -> None:
        self.sim = sim
        self.host = host
        self.port = port
        self._server: Optional[asyncio.Server] = None
        self._clients: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        asyncio.create_task(self._pump(), name="fake-phd2-pump")
        log.info("fake PHD2 listening on %s:%d", self.host, self.port)
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            with_suppressed = self._server.wait_closed()
            try:
                await asyncio.wait_for(with_suppressed, 1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.append(writer)
        await self._send(writer, {"Event": "Version", "PHDVersion": "2.6.13",
                                  "PHDSubver": "", "MsgVersion": 1})
        await self._send(writer, {"Event": "AppState", "State": self.sim.app_state})
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                try:
                    req = json.loads(raw.decode())
                except json.JSONDecodeError:
                    continue
                await self._send(writer, self._respond(req))
        except (ConnectionResetError, asyncio.CancelledError):
            return
        finally:
            if writer in self._clients:
                self._clients.remove(writer)

    def _respond(self, req: dict) -> dict:
        method = req.get("method")
        params = req.get("params") or []
        sim = self.sim
        result: Any = 0

        if method == "get_pixel_scale":
            result = sim.pixel_scale
        elif method == "get_app_state":
            result = sim.app_state
        elif method == "get_exposure":
            result = sim.exposure_ms
        elif method == "get_paused":
            result = False
        elif method == "get_dec_guide_mode":
            result = "Auto"
        elif method == "get_connected":
            result = True
        elif method == "get_profile":
            result = {"id": 1, "name": "Simulated Rig"}
        elif method == "get_current_equipment":
            result = {
                "camera": {"name": "Sim Guide Cam", "connected": True},
                "mount": {"name": "Simulated Mount", "connected": True},
            }
        elif method == "get_algo_param_names":
            axis = params[0] if params else "ra"
            result = (
                ["minMove", "hysteresis", "aggression"]
                if axis == "ra"
                else ["minMove", "aggression"]
            )
        elif method == "get_algo_param":
            result = self._get_param(params[0], params[1])
        elif method == "set_algo_param":
            self._set_param(params[0], params[1], float(params[2]))
        elif method == "dither":
            asyncio.create_task(self._do_settle(dithered=True))
        elif method == "guide":
            sim.app_state = "Guiding"
            asyncio.create_task(self._do_settle(dithered=False))
        elif method == "stop_capture":
            sim.app_state = "Stopped"
            asyncio.create_task(self._broadcast({"Event": "GuidingStopped"}))
        elif method == "loop":
            sim.app_state = "Looping"
        elif method == "set_paused":
            paused = bool(params[0]) if params else False
            sim.app_state = "Paused" if paused else "Guiding"
            asyncio.create_task(
                self._broadcast({"Event": "Paused" if paused else "Resumed"})
            )
        elif method == "clear_calibration":
            result = 0
        else:
            return {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32601, "message": f"unknown method {method}"}}

        return {"jsonrpc": "2.0", "id": req.get("id"), "result": result}

    def _get_param(self, axis: str, name: str) -> float:
        sim = self.sim
        table = {
            ("ra", "aggression"): sim.aggression,
            ("ra", "hysteresis"): sim.hysteresis,
            ("ra", "minMove"): sim.min_move_px,
            ("dec", "aggression"): sim.dec_aggression,
            ("dec", "minMove"): sim.dec_min_move_px,
        }
        return table.get((axis, name), 0.0)

    def _set_param(self, axis: str, name: str, value: float) -> None:
        sim = self.sim
        if axis == "ra":
            if name == "aggression":
                sim.aggression = value
            elif name == "hysteresis":
                sim.hysteresis = value
            elif name == "minMove":
                sim.min_move_px = value
        elif axis == "dec":
            if name == "aggression":
                sim.dec_aggression = value
            elif name == "minMove":
                sim.dec_min_move_px = value
        log.info("[sim] %s.%s = %.3f", axis, name, value)

    async def _do_settle(self, dithered: bool) -> None:
        if dithered:
            self.sim._ra_error += random.uniform(-4, 4)
            self.sim._dec_error += random.uniform(-4, 4)
            await self._broadcast({"Event": "GuidingDithered", "dx": 3.0, "dy": 3.0})
        await self._broadcast({"Event": "SettleBegin"})
        await asyncio.sleep(6.0)
        await self._broadcast(
            {"Event": "SettleDone", "Status": 0, "TotalFrames": 8, "DroppedFrames": 0}
        )

    async def _pump(self) -> None:
        """Emit guide steps, and occasionally something inconvenient."""
        next_dither = time.monotonic() + 240
        while True:
            await asyncio.sleep(self.sim.exposure_ms / 1000.0)
            if self.sim.app_state != "Guiding":
                continue

            await self._broadcast(self.sim.step())

            now = time.monotonic()
            if now >= next_dither:
                next_dither = now + random.uniform(200, 320)
                await self._do_settle(dithered=True)
            elif random.random() < 0.0015:
                await self._broadcast(
                    {"Event": "StarLost", "Frame": self.sim.frame, "SNR": 2.1,
                     "StarMass": 300, "Status": "low SNR"}
                )

    async def _broadcast(self, payload: dict) -> None:
        for writer in list(self._clients):
            try:
                await self._send(writer, payload)
            except Exception:  # noqa: BLE001
                if writer in self._clients:
                    self._clients.remove(writer)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write((json.dumps(payload) + "\r\n").encode())
        await writer.drain()


def build_fake_nina_app(sim: SimState):
    """A FastAPI app mimicking the parts of the Advanced API we consume."""

    app = FastAPI(title="Fake NINA Advanced API")
    sockets: list[WebSocket] = []
    image_index = {"n": 0}

    def envelope(response: Any) -> dict:
        return {
            "Response": response,
            "Error": "",
            "StatusCode": 200,
            "Success": True,
            "Type": "API",
        }

    def make_stats() -> dict:
        image_index["n"] += 1
        clouded = random.random() < 0.12
        return {
            "Index": image_index["n"],
            "ExposureTime": 300.0,
            "ImageType": "LIGHT",
            "Filter": sim.filter,
            "RmsText": f"{sim.seeing_arcsec * 0.3:.2f}",
            "Temperature": -10.0,
            "CameraName": "Sim ASI2600",
            "Gain": 100,
            "Offset": 50,
            "Date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "TelescopeName": "Sim RC8",
            "FocalLength": 1624.0,
            "StDev": 120.0,
            "Mean": 980.0,
            "Median": 970.0,
            "Stars": int((250 if clouded else 850) * random.uniform(0.9, 1.1)),
            "HFR": sim.seeing_arcsec * (1.6 if clouded else 1.15),
            "IsBayered": False,
            "Min": 100.0,
            "Max": 65535.0 if random.random() < 0.2 else 48000.0,
            "HFRStDev": 0.42,
            "TargetName": sim.target,
            "Filename": f"D:/Sim/{sim.target}_{sim.filter}_{image_index['n']:04d}.fits",
        }

    @app.get("/v2/api/version")
    async def version():
        return envelope("2.2.15-fake")

    @app.get("/v2/api/profile/show")
    async def profile():
        return envelope(
            {"AstrometrySettings": {"Latitude": 45.5, "Longitude": -73.6,
                                    "Elevation": 50.0}}
        )

    @app.get("/v2/api/sequence/json")
    async def sequence():
        done = min(4, int((time.monotonic() - sim.started) / 45))
        statuses = ["FINISHED"] * done + ["RUNNING"] + ["CREATED"] * 10
        return envelope([
            {"Name": "Start", "Status": "FINISHED", "Items": [
                {"Name": "Cool Camera", "Status": "FINISHED"},
                {"Name": "Unpark Scope", "Status": "FINISHED"},
            ]},
            {"Name": "Targets", "Status": "RUNNING",
             "Conditions": [{"Name": "Loop Until Altitude"}],
             "Triggers": [{"Name": "Meridian Flip"}],
             "Items": [
                 {"Name": sim.target, "Status": "RUNNING", "Items": [
                     {"Name": "Slew and Center", "Status": statuses[0]},
                     {"Name": "Run Autofocus", "Status": statuses[1]},
                     {"Name": f"Take Exposure {sim.filter}", "Status": statuses[2]},
                     {"Name": "Take Exposure OIII", "Status": statuses[3]},
                 ]},
             ]},
            {"GlobalTriggers": [{"Name": "Autofocus After HFR Increase"}]},
        ])

    @app.get("/v2/api/sequence/{action}")
    async def sequence_action(action: str):
        return envelope(f"sequence {action} accepted")

    @app.get("/v2/api/equipment/{device}/info")
    async def equipment(device: str):
        base = {"Connected": True, "Name": f"Sim {device}"}
        if device == "mount":
            base.update({
                "Altitude": sim.altitude, "Azimuth": sim.azimuth,
                "SideOfPier": "pierWest", "HoursToMeridian": 1.4,
                "TrackingEnabled": True,
            })
        elif device == "camera":
            base.update({"Temperature": -10.0, "CoolerOn": True})
        return envelope(base)

    @app.get("/v2/api/equipment/guider/graph")
    async def guider_graph():
        return envelope({"RMS": {"Total": sim.seeing_arcsec * 0.3}})

    @app.get("/v2/api/image-history")
    async def image_history(all: str = "true", count: str = "false", imageType: str = None):
        if count == "true":
            return envelope(image_index["n"])
        return envelope([make_stats() for _ in range(3)])

    @app.get("/v2/api/application/logs")
    async def logs(lineCount: int = 100, level: str = "INFO"):
        return envelope([
            {"Timestamp": datetime.now().isoformat(timespec="milliseconds"),
             "Level": "INFO", "Source": "Simulator.cs", "Member": "Tick",
             "Line": "1", "Message": f"simulated seeing {sim.seeing_arcsec:.2f}\""},
        ])

    @app.get("/v2/api/astro-util/moon-separation")
    async def moon_separation():
        return envelope(84.2)

    @app.websocket("/v2/socket")
    async def socket(ws: WebSocket):
        await ws.accept()
        sockets.append(ws)
        try:
            while True:
                await asyncio.sleep(30)
                await ws.send_json({"Response": {"Event": "HEARTBEAT"},
                                    "Success": True, "StatusCode": 200,
                                    "Type": "Socket"})
        except asyncio.CancelledError:
            # Server shutting down. CancelledError is a BaseException, so it
            # is not covered by the Exception clause below and would otherwise
            # print a traceback on every Ctrl+C.
            pass
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            if ws in sockets:
                sockets.remove(ws)

    async def emit_images() -> None:
        """Push an IMAGE-SAVE every so often, as a real session would."""
        while True:
            await asyncio.sleep(45)
            payload = {
                "Response": {"Event": "IMAGE-SAVE", "ImageStatistics": make_stats()},
                "Success": True, "StatusCode": 200, "Type": "Socket",
            }
            for ws in list(sockets):
                try:
                    await ws.send_json(payload)
                except Exception:  # noqa: BLE001
                    if ws in sockets:
                        sockets.remove(ws)

    @app.on_event("startup")
    async def _start_emitter():
        asyncio.create_task(emit_images(), name="fake-nina-images")

    return app
