"""
Asyncio client for the PHD2 event server.

PHD2 listens on TCP 4400 + (instance - 1) and speaks two interleaved protocols
down one socket, each message a single JSON object terminated by CRLF:

* **event notifications** -- objects carrying an ``Event`` key, pushed
  asynchronously (``GuideStep``, ``SettleDone``, ``StarLost``, ...);
* **JSON-RPC 2.0 responses** -- objects carrying an ``id`` matching a request
  we sent.

The reader loop demultiplexes on that shape. The subtle part is that
``guide`` and ``dither`` return their RPC result as soon as settling *starts*;
the operation actually completes with a later ``SettleDone`` event. Those calls
therefore arm a waiter **before** issuing the RPC, so a fast ``SettleDone``
cannot arrive in the gap and be missed.

The server has no authentication and binds all interfaces, so this connects
without credentials -- and is why AstroController must not be exposed beyond
the observatory LAN.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

EventHandler = Callable[[dict], None]


class Phd2Error(RuntimeError):
    """PHD2 returned a JSON-RPC error."""

    def __init__(self, method: str, code: Any, message: str) -> None:
        super().__init__(f"{method}: [{code}] {message}")
        self.method = method
        self.code = code
        self.message = message


class Phd2Disconnected(ConnectionError):
    """The socket closed while a call or settle was outstanding."""


@dataclass
class Settle:
    """Settling criteria shared by `guide` and `dither`."""

    pixels: float = 1.5
    time: float = 8.0
    timeout: float = 40.0

    def as_dict(self) -> dict:
        return {"pixels": self.pixels, "time": self.time, "timeout": self.timeout}


@dataclass
class SettleResult:
    status: int
    error: Optional[str]
    total_frames: int
    dropped_frames: int

    @property
    def ok(self) -> bool:
        return self.status == 0


@dataclass
class Phd2State:
    """Everything the guardrails need to know about PHD2's current condition."""

    connected: bool = False
    app_state: str = "Unknown"
    pixel_scale: Optional[float] = None
    exposure_ms: Optional[int] = None
    settling: bool = False
    calibrating: bool = False
    paused: bool = False
    dec_guide_mode: Optional[str] = None
    profile_name: Optional[str] = None
    equipment: dict = field(default_factory=dict)
    algo_params: dict[tuple[str, str], float] = field(default_factory=dict)
    available_params: dict[str, tuple[str, ...]] = field(default_factory=dict)

    settle_done_mono: Optional[float] = None
    last_dither_mono: Optional[float] = None
    last_star_lost_mono: Optional[float] = None
    last_rpc_ok_mono: Optional[float] = None
    guiding_since_mono: Optional[float] = None
    last_alert: Optional[str] = None

    @property
    def guiding(self) -> bool:
        return self.app_state == "Guiding"

    def as_dict(self) -> dict:
        now = time.monotonic()
        return {
            "connected": self.connected,
            "app_state": self.app_state,
            "pixel_scale": self.pixel_scale,
            "exposure_ms": self.exposure_ms,
            "settling": self.settling,
            "calibrating": self.calibrating,
            "paused": self.paused,
            "dec_guide_mode": self.dec_guide_mode,
            "profile_name": self.profile_name,
            "equipment": self.equipment,
            "params": {f"{a}.{p}": v for (a, p), v in sorted(self.algo_params.items())},
            "available_params": {k: list(v) for k, v in self.available_params.items()},
            "guiding_for_s": (
                round(now - self.guiding_since_mono, 1)
                if self.guiding_since_mono
                else None
            ),
            "last_alert": self.last_alert,
        }


class Phd2Client:
    """
    One supervised connection to PHD2.

    `run()` is the long-lived coroutine handed to the Supervisor; it connects,
    seeds state, and reads until the socket drops. Callers use `call()` and the
    typed helpers, which raise `Phd2Disconnected` rather than hanging when the
    connection goes away.
    """

    def __init__(
        self,
        host: str,
        instance: int = 1,
        *,
        connect_timeout: float = 5.0,
        rpc_timeout: float = 10.0,
        on_event: Optional[EventHandler] = None,
        on_connect: Optional[Callable[[], Any]] = None,
        on_disconnect: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.host = host
        self.instance = instance
        self.connect_timeout = connect_timeout
        self.rpc_timeout = rpc_timeout
        self.state = Phd2State()

        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._write_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._settle_waiters: list[asyncio.Future] = []

    @property
    def port(self) -> int:
        return 4400 + self.instance - 1

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # ── lifecycle ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, seed state, and pump the reader until the socket closes."""
        log.info("connecting to PHD2 at %s:%d", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )
        self.state.connected = True
        log.info("PHD2 connected")

        reader_task = asyncio.create_task(self._read_loop(), name="phd2-reader")
        heartbeat_task = asyncio.create_task(self._heartbeat(), name="phd2-heartbeat")
        try:
            # Seed state from RPCs. The event stream reports only *changes*,
            # so a fresh connection knows nothing until it asks.
            await self._seed_state()
            if self._on_connect:
                await _maybe_await(self._on_connect())
            await reader_task
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
            # Fail everything outstanding synchronously and first. If this is
            # left until after an await, a cancellation or a reader exception
            # can skip it and leave callers hanging on futures that will never
            # be resolved.
            self._fail_pending(Phd2Disconnected("PHD2 connection closed"))
            reader_task.cancel()
            # The reader usually finished by raising; suppress whatever it was.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader_task
            await self._teardown()

    async def _heartbeat(self, interval: float = 15.0) -> None:
        """
        Periodically prove the request path still works.

        Events flowing only shows that PHD2 can talk to us. Before writing a
        guiding parameter the guardrails need to know we can talk to *it*, and
        without this the "no successful call recently" check would be
        permanently unsatisfiable on an otherwise healthy connection -- the
        app is event-driven and would never otherwise make a request.

        `get_app_state` is the cheapest call that also refreshes something
        worth having.
        """
        while True:
            await asyncio.sleep(interval)
            if not self.connected:
                return
            try:
                state = await self.call("get_app_state", timeout=5.0)
            except (Phd2Error, Phd2Disconnected):
                return  # the reader will notice and the supervisor reconnects
            except Exception:  # noqa: BLE001
                return
            if isinstance(state, str):
                self._set_app_state(state, time.monotonic())

    def _fail_pending(self, exc: Exception) -> None:
        """Resolve every outstanding future with `exc`. Must stay synchronous."""
        self.state.connected = False
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        for fut in list(self._settle_waiters):
            if not fut.done():
                fut.set_exception(exc)
        self._settle_waiters.clear()

    async def _teardown(self) -> None:
        self._fail_pending(Phd2Disconnected("PHD2 connection closed"))
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer.wait_closed()
        if self._on_disconnect:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await _maybe_await(self._on_disconnect())
        log.info("PHD2 disconnected")

    async def aclose(self) -> None:
        await self._teardown()

    # ── reader ─────────────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise Phd2Disconnected("PHD2 closed the connection")
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("unparseable line from PHD2: %.200s", line)
                continue
            if not isinstance(msg, dict):
                continue

            if "Event" in msg:
                self._handle_event(msg)
            elif "id" in msg:
                self._resolve(msg)
            else:
                log.debug("unclassified PHD2 message: %.200s", line)

    def _resolve(self, msg: dict) -> None:
        fut = self._pending.pop(msg.get("id"), None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            fut.set_exception(
                Phd2Error(
                    method=getattr(fut, "_method", "?"),
                    code=err.get("code"),
                    message=err.get("message", "unknown error"),
                )
            )
        else:
            self.state.last_rpc_ok_mono = time.monotonic()
            fut.set_result(msg.get("result"))

    def _handle_event(self, msg: dict) -> None:
        event = msg.get("Event", "")
        now = time.monotonic()

        if event == "AppState":
            self._set_app_state(msg.get("State", "Unknown"), now)
        elif event == "StartGuiding":
            self._set_app_state("Guiding", now)
        elif event == "GuidingStopped":
            self._set_app_state("Stopped", now)
        elif event == "StartCalibration":
            self.state.calibrating = True
            self._set_app_state("Calibrating", now)
        elif event in ("CalibrationComplete", "CalibrationFailed"):
            self.state.calibrating = False
        elif event == "Paused":
            self.state.paused = True
        elif event == "Resumed":
            self.state.paused = False
        elif event in ("SettleBegin", "Settling"):
            self.state.settling = True
        elif event == "SettleDone":
            self.state.settling = False
            self.state.settle_done_mono = now
            self._resolve_settle(msg)
        elif event == "GuidingDithered":
            self.state.last_dither_mono = now
        elif event == "StarLost":
            self.state.last_star_lost_mono = now
        elif event == "Alert":
            self.state.last_alert = msg.get("Msg")
            log.warning("PHD2 alert (%s): %s", msg.get("Type"), msg.get("Msg"))
        elif event == "ConfigurationChange":
            # Settings changed underneath us; cached params are no longer
            # trustworthy. Re-read them rather than acting on stale values.
            asyncio.create_task(self._safe_refresh_params())
        elif event == "GuideStep" and self.state.guiding_since_mono is None:
            self.state.guiding_since_mono = now

        if self._on_event:
            try:
                self._on_event(msg)
            except Exception:  # noqa: BLE001 - a bad handler must not kill the reader
                log.exception("PHD2 event handler failed for %s", event)

    def _set_app_state(self, state: str, now: float) -> None:
        if state == self.state.app_state:
            return
        log.info("PHD2 app state %s -> %s", self.state.app_state, state)
        self.state.app_state = state
        if state == "Guiding":
            if self.state.guiding_since_mono is None:
                self.state.guiding_since_mono = now
        else:
            self.state.guiding_since_mono = None

    def _resolve_settle(self, msg: dict) -> None:
        result = SettleResult(
            status=int(msg.get("Status", 0) or 0),
            error=msg.get("Error"),
            total_frames=int(msg.get("TotalFrames", 0) or 0),
            dropped_frames=int(msg.get("DroppedFrames", 0) or 0),
        )
        for fut in list(self._settle_waiters):
            if not fut.done():
                fut.set_result(result)
        self._settle_waiters.clear()

    def _fail_settle(self, exc: Exception) -> None:
        for fut in list(self._settle_waiters):
            if not fut.done():
                fut.set_exception(exc)
        self._settle_waiters.clear()

    # ── RPC ────────────────────────────────────────────────────────────

    async def call(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Issue a JSON-RPC call and await its id-matched response."""
        if not self.connected:
            raise Phd2Disconnected(f"cannot call {method}: not connected")

        req_id = next(self._ids)
        payload: dict[str, Any] = {
            "method": method,
            "id": req_id,
            "jsonrpc": "2.0",
        }
        if params is not None:
            payload["params"] = params

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        fut._method = method  # type: ignore[attr-defined]
        self._pending[req_id] = fut

        line = (json.dumps(payload) + "\r\n").encode("utf-8")
        try:
            async with self._write_lock:
                assert self._writer is not None
                self._writer.write(line)
                await self._writer.drain()
            return await asyncio.wait_for(fut, timeout or self.rpc_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise Phd2Disconnected(f"{method}: timed out after {timeout or self.rpc_timeout}s")
        except Exception:
            self._pending.pop(req_id, None)
            raise

    # ── settle-completing operations ───────────────────────────────────

    def _arm_settle(self) -> asyncio.Future:
        """Register a settle waiter BEFORE the RPC that triggers settling."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._settle_waiters.append(fut)
        return fut

    def _disarm_settle(self, fut: asyncio.Future) -> None:
        with contextlib.suppress(ValueError):
            self._settle_waiters.remove(fut)

    async def dither(
        self,
        amount: float = 3.0,
        ra_only: bool = False,
        settle: Optional[Settle] = None,
        *,
        timeout: float = 180.0,
    ) -> SettleResult:
        settle = settle or Settle()
        waiter = self._arm_settle()
        try:
            await self.call(
                "dither",
                {"amount": amount, "raOnly": ra_only, "settle": settle.as_dict()},
            )
        except Exception:
            self._disarm_settle(waiter)
            raise
        return await asyncio.wait_for(waiter, timeout)

    async def guide(
        self,
        settle: Optional[Settle] = None,
        recalibrate: bool = False,
        *,
        timeout: float = 300.0,
    ) -> SettleResult:
        settle = settle or Settle()
        waiter = self._arm_settle()
        try:
            await self.call(
                "guide",
                {"settle": settle.as_dict(), "recalibrate": recalibrate},
            )
        except Exception:
            self._disarm_settle(waiter)
            raise
        return await asyncio.wait_for(waiter, timeout)

    # ── typed helpers ──────────────────────────────────────────────────

    async def stop_capture(self) -> None:
        await self.call("stop_capture")

    async def loop(self) -> None:
        await self.call("loop")

    async def set_paused(self, paused: bool, full: bool = False) -> None:
        params: list[Any] = [paused]
        if paused and full:
            params.append("full")
        await self.call("set_paused", params)

    async def clear_calibration(self, which: str = "both") -> None:
        await self.call("clear_calibration", [which])

    async def get_pixel_scale(self) -> Optional[float]:
        value = await self.call("get_pixel_scale")
        return float(value) if value else None

    async def get_app_state(self) -> str:
        return str(await self.call("get_app_state"))

    async def get_star_image(self, size: int = 15) -> dict:
        """
        The guide camera's crop around the current star.

        PHD2 returns ``{frame, width, height, star_pos, pixels}`` where
        ``pixels`` is base64 little-endian uint16. It errors when nothing is
        selected or the camera is not looping, which is a normal state and not
        worth logging -- callers surface it as "no star".
        """
        result = await self.call("get_star_image", {"size": max(15, int(size))})
        if not isinstance(result, dict):
            raise Phd2Error("get_star_image", 0, "unexpected response shape")
        return result

    async def get_algo_param_names(self, axis: str) -> tuple[str, ...]:
        names = await self.call("get_algo_param_names", [axis])
        return tuple(names or ())

    async def get_algo_param(self, axis: str, name: str) -> float:
        return float(await self.call("get_algo_param", [axis, name]))

    async def set_algo_param(self, axis: str, name: str, value: float) -> float:
        """
        Set a guiding parameter and return what PHD2 actually stored.

        PHD2 clamps silently, so the readback -- not the requested value -- is
        recorded as truth. A divergence is logged because it usually means our
        configured bounds disagree with PHD2's own limits.
        """
        await self.call("set_algo_param", [axis, name, value])
        readback = await self.get_algo_param(axis, name)
        self.state.algo_params[(axis, name)] = readback
        if abs(readback - value) > 1e-6:
            log.warning(
                "PHD2 clamped %s.%s: requested %.4f, stored %.4f",
                axis, name, value, readback,
            )
        return readback

    async def refresh_algo_params(self) -> dict[tuple[str, str], float]:
        """Discover which parameters the current algorithms actually expose."""
        params: dict[tuple[str, str], float] = {}
        available: dict[str, tuple[str, ...]] = {}
        for axis in ("ra", "dec"):
            try:
                names = await self.get_algo_param_names(axis)
            except (Phd2Error, Phd2Disconnected) as exc:
                log.warning("could not list %s algo params: %s", axis, exc)
                continue
            available[axis] = names
            for name in names:
                try:
                    params[(axis, name)] = await self.get_algo_param(axis, name)
                except Phd2Error as exc:
                    log.debug("skipping %s.%s: %s", axis, name, exc)
        self.state.algo_params = params
        self.state.available_params = available
        return params

    async def _safe_refresh_params(self) -> None:
        try:
            await self.refresh_algo_params()
        except Exception as exc:  # noqa: BLE001
            log.debug("param refresh after ConfigurationChange failed: %s", exc)

    async def _seed_state(self) -> None:
        """
        Populate state that events alone never provide.

        Called on every (re)connect: a reconnect means we missed an unknown
        number of events, so the authoritative values are re-fetched rather
        than inferred.
        """
        with contextlib.suppress(Exception):
            self.state.app_state = await self.get_app_state()
        with contextlib.suppress(Exception):
            self.state.pixel_scale = await self.get_pixel_scale()
        with contextlib.suppress(Exception):
            self.state.exposure_ms = int(await self.call("get_exposure"))
        with contextlib.suppress(Exception):
            self.state.dec_guide_mode = await self.call("get_dec_guide_mode")
        with contextlib.suppress(Exception):
            self.state.paused = bool(await self.call("get_paused"))
        with contextlib.suppress(Exception):
            profile = await self.call("get_profile")
            if isinstance(profile, dict):
                self.state.profile_name = profile.get("name")
        with contextlib.suppress(Exception):
            equipment = await self.call("get_current_equipment")
            if isinstance(equipment, dict):
                self.state.equipment = equipment
        with contextlib.suppress(Exception):
            await self.refresh_algo_params()
        log.info(
            "PHD2 seeded: state=%s scale=%s params=%d",
            self.state.app_state,
            self.state.pixel_scale,
            len(self.state.algo_params),
        )


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value
