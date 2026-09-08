"""
PHD2 client: line framing, id-correlated RPC, and settle-completing calls.

These run against a fake server that speaks the real wire protocol, so the
demultiplexing logic is exercised end to end rather than mocked out.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from astrocontroller.phd2.client import (
    Phd2Client,
    Phd2Disconnected,
    Phd2Error,
    Settle,
)


class FakePhd2:
    """A minimal PHD2 event server for tests."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.port = 0
        self.requests: list[dict] = []
        self.writer: asyncio.StreamWriter | None = None
        self.connected = asyncio.Event()
        # method -> result, or method -> ("error", code, message).
        # Defaults are plausible values so connect-time seeding leaves the
        # client in a realistic state.
        self.results: dict[str, object] = {
            "get_app_state": "Guiding",
            "get_pixel_scale": 1.6,
            "get_exposure": 2000,
            "get_dec_guide_mode": "Auto",
            "get_paused": False,
            "get_profile": {"id": 1, "name": "Test Rig"},
            "get_current_equipment": {"camera": {"name": "Guide Cam", "connected": True}},
            "get_algo_param_names": ["minMove", "hysteresis", "aggression"],
            "get_algo_param": 0.7,
        }
        self.autorespond = True

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Close the live connection first: Server.wait_closed() waits for
        # active handler tasks, and ours blocks in readline() until its peer
        # goes away.
        self.drop()
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 1.0)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.connected.set()
        await self.send_event({"Event": "Version", "PHDVersion": "2.6.13"})
        await self.send_event({"Event": "AppState", "State": "Guiding"})
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                req = json.loads(raw.decode())
                self.requests.append(req)
                if self.autorespond:
                    await self._respond(req)
        except (ConnectionResetError, asyncio.CancelledError):
            return

    async def _respond(self, req: dict) -> None:
        method = req.get("method")
        result = self.results.get(method, 0)
        if isinstance(result, tuple) and result and result[0] == "error":
            msg = {"jsonrpc": "2.0", "id": req["id"],
                   "error": {"code": result[1], "message": result[2]}}
        else:
            msg = {"jsonrpc": "2.0", "id": req["id"], "result": result}
        await self._write(msg)

    async def send_event(self, payload: dict) -> None:
        await self._write(payload)

    async def _write(self, payload: dict) -> None:
        if self.writer is None:
            return
        self.writer.write((json.dumps(payload) + "\r\n").encode())
        await self.writer.drain()

    def drop(self) -> None:
        if self.writer is not None:
            self.writer.close()


@pytest.fixture
async def phd2():
    server = FakePhd2()
    await server.start()
    yield server
    await server.stop()


async def _connected(server: FakePhd2, **kwargs) -> tuple[Phd2Client, asyncio.Task]:
    kwargs.setdefault("rpc_timeout", 1.0)
    kwargs.setdefault("connect_timeout", 2.0)
    client = Phd2Client("127.0.0.1", instance=1, **kwargs)
    # instance 1 -> 4400; override the port the fake actually bound.
    client.instance = server.port - 4400 + 1
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(server.connected.wait(), 2.0)
    await asyncio.sleep(0.05)  # let seeding finish
    return client, task


async def _shutdown(client: Phd2Client, task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Phd2Disconnected, ConnectionError):
        pass


def test_port_follows_instance_number():
    assert Phd2Client("h", instance=1).port == 4400
    assert Phd2Client("h", instance=3).port == 4402


async def test_rpc_result_is_matched_by_id(phd2):
    phd2.results["get_pixel_scale"] = 1.63
    client, task = await _connected(phd2)
    try:
        assert await client.get_pixel_scale() == pytest.approx(1.63)
    finally:
        await _shutdown(client, task)


async def test_concurrent_calls_do_not_cross_wires(phd2):
    # Responses must be routed by id, not by arrival order.
    client, task = await _connected(phd2)
    phd2.autorespond = False
    phd2.requests.clear()  # drop the connect-time seeding calls
    try:
        a = asyncio.create_task(client.call("get_exposure"))
        b = asyncio.create_task(client.call("get_pixel_scale"))
        await asyncio.sleep(0.05)

        ids = {r["method"]: r["id"] for r in phd2.requests}
        # Reply out of order, deliberately.
        await phd2._write({"id": ids["get_pixel_scale"], "result": 1.5})
        await phd2._write({"id": ids["get_exposure"], "result": 2500})

        assert await asyncio.wait_for(a, 1.0) == 2500
        assert await asyncio.wait_for(b, 1.0) == 1.5
    finally:
        await _shutdown(client, task)


async def test_error_response_raises(phd2):
    phd2.results["get_cooler_status"] = ("error", 1, "no cooler")
    client, task = await _connected(phd2)
    try:
        with pytest.raises(Phd2Error) as exc:
            await client.call("get_cooler_status")
        assert "no cooler" in str(exc.value)
    finally:
        await _shutdown(client, task)


async def test_events_update_state(phd2):
    client, task = await _connected(phd2)
    try:
        assert client.state.app_state == "Guiding"
        await phd2.send_event({"Event": "Paused"})
        await asyncio.sleep(0.05)
        assert client.state.paused is True
        await phd2.send_event({"Event": "Alert", "Type": "warning", "Msg": "boom"})
        await asyncio.sleep(0.05)
        assert client.state.last_alert == "boom"
    finally:
        await _shutdown(client, task)


async def test_dither_waits_for_settledone_not_the_rpc_result(phd2):
    client, task = await _connected(phd2)
    try:
        fut = asyncio.create_task(client.dither(3.0))
        await asyncio.sleep(0.05)
        # The RPC has already returned 0, but dither() must still be pending.
        assert not fut.done()

        await phd2.send_event({"Event": "SettleDone", "Status": 0,
                               "TotalFrames": 12, "DroppedFrames": 1})
        result = await asyncio.wait_for(fut, 1.0)
        assert result.ok and result.total_frames == 12
    finally:
        await _shutdown(client, task)


async def test_settledone_arriving_before_await_is_not_missed(phd2):
    """
    The race the arming order exists to prevent: PHD2 can emit SettleDone
    between the RPC response and the caller awaiting the waiter.
    """
    client, task = await _connected(phd2)
    try:
        async def respond_then_settle(req):
            await phd2._write({"id": req["id"], "result": 0})
            await phd2.send_event({"Event": "SettleDone", "Status": 0,
                                   "TotalFrames": 5, "DroppedFrames": 0})

        phd2.autorespond = False
        fut = asyncio.create_task(client.dither(3.0))
        await asyncio.sleep(0.05)
        await respond_then_settle(phd2.requests[-1])

        result = await asyncio.wait_for(fut, 1.0)
        assert result.ok
    finally:
        await _shutdown(client, task)


async def test_disconnect_fails_pending_calls_instead_of_hanging(phd2):
    client, task = await _connected(phd2)
    phd2.autorespond = False
    try:
        pending = asyncio.create_task(client.call("get_exposure"))
        await asyncio.sleep(0.05)
        phd2.drop()
        with pytest.raises((Phd2Disconnected, ConnectionError)):
            await asyncio.wait_for(pending, 2.0)
    finally:
        await _shutdown(client, task)


async def test_disconnect_fails_pending_settle(phd2):
    client, task = await _connected(phd2)
    try:
        fut = asyncio.create_task(client.dither(3.0))
        await asyncio.sleep(0.05)
        phd2.drop()
        with pytest.raises((Phd2Disconnected, ConnectionError)):
            await asyncio.wait_for(fut, 2.0)
    finally:
        await _shutdown(client, task)


async def test_set_algo_param_records_the_readback_not_the_request(phd2):
    # PHD2 clamps silently; the stored value is the truth.
    phd2.results["set_algo_param"] = 0
    phd2.results["get_algo_param"] = 0.85
    client, task = await _connected(phd2)
    try:
        stored = await client.set_algo_param("ra", "aggression", 2.0)
        assert stored == pytest.approx(0.85)
        assert client.state.algo_params[("ra", "aggression")] == pytest.approx(0.85)
    finally:
        await _shutdown(client, task)


async def test_refresh_discovers_available_params(phd2):
    phd2.results["get_algo_param_names"] = ["minMove", "hysteresis", "aggression"]
    phd2.results["get_algo_param"] = 0.7
    client, task = await _connected(phd2)
    try:
        params = await client.refresh_algo_params()
        assert ("ra", "hysteresis") in params
        assert "hysteresis" in client.state.available_params["ra"]
    finally:
        await _shutdown(client, task)


async def test_malformed_line_does_not_kill_the_reader(phd2):
    client, task = await _connected(phd2)
    try:
        phd2.writer.write(b"{not json\r\n")
        await phd2.writer.drain()
        await asyncio.sleep(0.05)
        # Still alive and serving.
        phd2.results["get_app_state"] = "Guiding"
        assert await client.get_app_state() == "Guiding"
    finally:
        await _shutdown(client, task)
