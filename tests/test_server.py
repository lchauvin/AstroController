"""
HTTP surface: routing, validation and the shared-secret guard.

The runtime is deliberately not started here (TestClient only runs lifespan
inside a context manager), so endpoints that need it return 503. That is
enough to prove routing and auth without touching a telescope.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from astrocontroller.config import Config, ConfigError, load_config
from astrocontroller.server.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Config()))


def test_expected_routes_exist(client):
    paths = {r.path for r in client.app.routes if hasattr(r, "path")}
    for path in (
        "/api/state",
        "/api/stream",
        "/api/health",
        "/api/sequence/{action}",
        "/api/guiding/start",
        "/api/guiding/dither",
        "/api/guiding/param",
        "/api/advisor/tuning",
        "/api/advisor/revert-all",
        "/api/tppa/start",
        "/api/image/{index}",
    ):
        assert path in paths, f"missing route {path}"


def test_health_works_without_a_runtime(client):
    body = client.get("/api/health").json()
    assert body == {"ok": False, "tasks": []}


def test_state_reports_503_until_the_runtime_starts(client):
    assert client.get("/api/state").status_code == 503


def test_unknown_sequence_action_is_rejected(client):
    # Validated before the runtime check, so this is a 400 not a 503.
    assert client.post("/api/sequence/destroy").status_code == 400


def test_dither_validates_its_body(client):
    assert client.post("/api/guiding/dither", json={"pixels": -1}).status_code == 422
    assert client.post("/api/guiding/dither", json={"pixels": 999}).status_code == 422


def test_param_update_validates_the_axis(client):
    resp = client.post(
        "/api/guiding/param", json={"axis": "up", "param": "aggression", "value": 0.5}
    )
    assert resp.status_code == 422


def test_tuning_mode_is_constrained(client):
    assert client.post("/api/advisor/tuning", json={"mode": "yolo"}).status_code == 422
    # A valid body still needs the runtime.
    assert client.post("/api/advisor/tuning", json={"mode": "auto"}).status_code == 503


# ── the shared secret ──────────────────────────────────────────────────


def test_loopback_needs_no_token():
    config = Config()
    assert config.server.is_loopback
    assert config.require_token() is None


def test_non_loopback_without_a_token_refuses_to_start(monkeypatch):
    # Neither NINA's API nor PHD2 has any authentication, and this UI can stop
    # a running sequence, so this process is the only place a check can happen.
    monkeypatch.delenv("ASTROCONTROLLER_TOKEN", raising=False)
    config = Config()
    config.server.host = "0.0.0.0"
    with pytest.raises(ConfigError) as exc:
        config.require_token()
    assert "shared secret is required" in str(exc.value)


def test_non_loopback_with_a_token_is_allowed(monkeypatch):
    monkeypatch.setenv("ASTROCONTROLLER_TOKEN", "s3cret")
    config = Config()
    config.server.host = "0.0.0.0"
    assert config.require_token() == "s3cret"


def test_token_is_enforced_on_every_protected_route(monkeypatch):
    monkeypatch.setenv("ASTROCONTROLLER_TOKEN", "s3cret")
    config = Config()
    config.server.host = "0.0.0.0"
    client = TestClient(create_app(config))

    assert client.get("/api/state").status_code == 401
    assert client.post("/api/sequence/stop").status_code == 401

    # Header form, used by fetch().
    assert client.get("/api/state", headers={"X-Auth-Token": "s3cret"}).status_code == 503
    # Query form, because EventSource cannot set headers.
    assert client.get("/api/state?token=s3cret").status_code == 503
    assert client.get("/api/state?token=wrong").status_code == 401


def test_health_stays_open_so_monitoring_still_works(monkeypatch):
    monkeypatch.setenv("ASTROCONTROLLER_TOKEN", "s3cret")
    config = Config()
    config.server.host = "0.0.0.0"
    client = TestClient(create_app(config))
    assert client.get("/api/health").status_code == 200


# ── config file loading ────────────────────────────────────────────────


def test_unknown_key_is_rejected_with_a_helpful_message(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text('[nina]\nhost = "1.2.3.4"\nprot = 1888\n', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(str(path))
    assert "unknown key(s): prot" in str(exc.value)
    assert "port" in str(exc.value)  # suggests the valid keys


def test_unknown_section_is_rejected(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text('[ninja]\nhost = "1.2.3.4"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(str(path))
    assert "unknown section(s): ninja" in str(exc.value)


def test_bounds_are_parsed_and_validated(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text(
        "[[tuning.bounds]]\n"
        'axis = "ra"\nparam = "aggression"\n'
        "lo = 0.4\nhi = 1.0\nmax_delta = 0.1\n",
        encoding="utf-8",
    )
    config = load_config(str(path))
    assert len(config.tuning.bounds) == 1
    assert config.bound_for("ra", "aggression").hi == 1.0


def test_inverted_bounds_are_rejected(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text(
        "[[tuning.bounds]]\n"
        'axis = "ra"\nparam = "aggression"\n'
        "lo = 1.0\nhi = 0.4\nmax_delta = 0.1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="lo .* must be < hi"):
        load_config(str(path))


def test_bad_axis_in_bounds_is_rejected(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text(
        "[[tuning.bounds]]\n"
        'axis = "alt"\nparam = "aggression"\n'
        "lo = 0.4\nhi = 1.0\nmax_delta = 0.1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="axis must be"):
        load_config(str(path))


def test_bad_tuning_mode_is_rejected(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text('[tuning]\nmode = "wild"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="mode must be one of"):
        load_config(str(path))


def test_model_string_must_name_a_provider(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text('[llm]\nmodel = "llama3.1"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="provider/model-id"):
        load_config(str(path))


def test_missing_explicit_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.toml"))


def test_phd2_port_follows_the_instance_number(tmp_path):
    path = tmp_path / "astrocontroller.toml"
    path.write_text('[phd2]\nhost = "10.0.0.5"\ninstance = 3\n', encoding="utf-8")
    config = load_config(str(path))
    assert config.phd2.port == 4402


# ── shutdown ───────────────────────────────────────────────────────────


class _FakeHub:
    def __init__(self):
        self.released = False

    def subscribe(self):
        import asyncio

        class _Sub:
            queue = asyncio.Queue()

        return _Sub()

    def unsubscribe(self, sub):
        self.released = True


class _FakeRuntime:
    def __init__(self):
        self.hub = _FakeHub()

    def snapshot(self):
        return {"phd2": {"connected": False}}


async def _drain_stream(app):
    """Call the /api/stream endpoint directly and collect what it yields."""
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": "/api/stream",
        "headers": [], "query_string": b"",
    }

    async def receive():
        return {"type": "http.request"}

    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/stream")
    response = await route.endpoint(Request(scope, receive))
    return [chunk async for chunk in response.body_iterator]


async def test_stream_exits_promptly_when_the_server_is_shutting_down():
    """
    Ctrl+C must not hang on "Waiting for connections to close".

    Uvicorn waits for open connections *before* running lifespan shutdown, so
    the SSE generator has to notice the shutdown itself. With `should_stop`
    already true it yields its snapshot and returns instead of looping forever.
    """
    app = create_app(Config(), should_stop=lambda: True)
    app.state.runtime = _FakeRuntime()

    chunks = await _drain_stream(app)

    assert len(chunks) == 1                      # snapshot, then a clean return
    assert '"snapshot"' in chunks[0]
    assert app.state.runtime.hub.released        # the subscriber is cleaned up


def test_should_stop_defaults_to_false():
    assert create_app(Config()).state.should_stop() is False
