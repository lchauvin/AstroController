"""
NINA websocket listener.

The plugin pushes coarse state changes on ``ws://<host>:<port>/v2/socket``.
Every payload uses the same envelope as the REST API, with the interesting
part under ``Response``.

The load-bearing rule for this whole module: **the websocket carries change
notifications, REST carries truth.** A reconnect means an unknown number of
events were missed, so `on_connect` triggers a full REST resync rather than
any attempt to replay history.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

import websockets

log = logging.getLogger(__name__)

EventCallback = Callable[[str, dict], Awaitable[None] | None]

# Events that carry a full ImageStatistics block rather than just a name.
IMAGE_SAVE = "IMAGE-SAVE"

# Events the advisor's guardrails care about, beyond display.
DISTURBANCE_EVENTS = frozenset({
    "MOUNT-BEFORE-FLIP",
    "MOUNT-AFTER-FLIP",
    "AUTOFOCUS-STARTING",
    "AUTOFOCUS-FINISHED",
    "GUIDER-DITHER",
})


class NinaEventListener:
    """
    One supervised websocket connection.

    `run()` never returns normally: it raises when the socket closes so the
    Supervisor can apply backoff and reconnect.
    """

    def __init__(
        self,
        ws_base: str,
        on_event: EventCallback,
        *,
        on_connect: Optional[Callable[[], Awaitable[None]]] = None,
        channel: str = "/socket",
        ping_interval: float = 20.0,
    ) -> None:
        self.url = ws_base.rstrip("/") + channel
        self._on_event = on_event
        self._on_connect = on_connect
        self.ping_interval = ping_interval
        self.connected = False

    async def run(self) -> None:
        log.info("connecting to NINA websocket %s", self.url)
        async with websockets.connect(
            self.url,
            ping_interval=self.ping_interval,
            open_timeout=10.0,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self.connected = True
            log.info("NINA websocket connected")
            if self._on_connect:
                await self._on_connect()
            try:
                async for raw in ws:
                    await self._dispatch(raw)
            finally:
                self.connected = False
                log.info("NINA websocket closed")
        raise ConnectionError("NINA websocket closed")

    async def _dispatch(self, raw: Any) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("unparseable NINA websocket frame: %.200s", raw)
            return
        if not isinstance(msg, dict):
            return

        payload = msg.get("Response", msg)
        if isinstance(payload, str):
            # Some events send a bare event name as the response.
            payload = {"Event": payload}
        if not isinstance(payload, dict):
            return

        event = payload.get("Event")
        if not event:
            return

        try:
            result = self._on_event(str(event), payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - a bad handler must not drop the socket
            log.exception("NINA event handler failed for %s", event)


class TppaSession:
    """
    Three Point Polar Alignment over ``/v2/tppa``.

    Unlike the main event socket this is opened on demand -- connecting starts
    nothing, but the channel only streams while an alignment is running. The
    plugin publishes `{AzimuthError, AltitudeError, TotalError}` updates plus
    `{Status, Progress}` progress messages.

    Requires the separate TPPA plugin (>= 2.2.4.1) on the NINA side; when it is
    absent the connection simply fails and the UI panel stays hidden.
    """

    def __init__(self, ws_base: str, on_update: EventCallback) -> None:
        self.url = ws_base.rstrip("/") + "/tppa"
        self._on_update = on_update
        self._ws: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self.running = False
        self.last: dict = {}

    async def start(self, **options: Any) -> None:
        """Open the channel and begin an alignment run."""
        if self.running:
            raise RuntimeError("an alignment is already running")
        self._ws = await websockets.connect(self.url, open_timeout=10.0)
        self.running = True
        payload = {"Action": "start-alignment", **options}
        await self._ws.send(json.dumps(payload))
        self._task = asyncio.create_task(self._pump(), name="tppa-pump")

    async def send(self, action: str) -> None:
        if not self._ws:
            raise RuntimeError("TPPA channel is not open")
        await self._ws.send(json.dumps({"Action": action}))

    async def stop(self) -> None:
        if self._ws:
            with_suppressed = _suppress(self.send("stop-alignment"))
            await with_suppressed
        await self.aclose()

    async def aclose(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._ws:
            await _suppress(self._ws.close())
            self._ws = None

    async def _pump(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = msg.get("Response", msg) if isinstance(msg, dict) else msg
                if isinstance(payload, dict):
                    self.last = payload
                elif isinstance(payload, str):
                    self.last = {"Status": payload}
                else:
                    continue
                result = self._on_update("TPPA", dict(self.last))
                if asyncio.iscoroutine(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("TPPA channel ended: %s", exc)
        finally:
            self.running = False


async def _suppress(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception:  # noqa: BLE001
        pass
