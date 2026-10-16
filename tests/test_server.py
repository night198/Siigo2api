"""HTTP security, byte limits and channel lifetime regressions."""
import asyncio
from types import SimpleNamespace
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from siigo_bridge import server
from siigo_bridge.auth import BridgeError


class Manager:
    def credentials(self, *, refresh=False):
        return SimpleNamespace(api_key="local-secret", machine_id="0" * 24, access="test-access")


@pytest.fixture
def client():
    with TestClient(server.create_app(Manager())) as client:
        yield client


AUTH = {"Authorization": "Bearer local-secret"}


def test_health_has_no_credentials_and_v1_requires_bearer(client):
    assert client.get("/health").json() == {"status": "ok", "protocol": "responses+chat-completions"}
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Cookie": "api_key=local-secret"}):
        assert client.get("/v1/models", headers=headers).status_code == 401
        assert client.post("/v1/chat/completions", headers=headers, json={}).status_code == 401
    assert client.post("/v1/responses/compact", headers=AUTH).status_code == 404
    assert client.post("/login", headers=AUTH).status_code == 404
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": "https://example.com"}).headers


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b'{"model":"","input":[]}',
    b'{"model":"m"}', b'{"model":1,"input":[]}', b'{"model":"m","input":[],"stream":1}',
    b'{"model":"m","input":NaN}', b'{"model":"m","input":[],"max_output_tokens":1e400}'])
def test_invalid_bodies_never_open_transport(client, monkeypatch, body):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid request opened an upstream channel")
    monkeypatch.setattr(server, "Forward", unexpected)
    assert client.post("/v1/responses", headers=AUTH, content=body).status_code == 400


@pytest.mark.parametrize("body", [
    {"model": "m"}, {"model": "m", "messages": []}, {"model": "m", "messages": "hello"},
    {"model": "", "messages": [{"role": "user", "content": "hello"}]},
    {"model": "m", "messages": [{"role": "user", "content": "hello"}], "stream": 1},
])
def test_invalid_chat_messages_do_not_open_cloud_session(client, monkeypatch, body):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid chat request opened a cloud session")
    monkeypatch.setattr(server, "Forward", unexpected)
    assert client.post("/v1/chat/completions", headers=AUTH, json=body).status_code == 400


@pytest.mark.parametrize("route,body,status,code", [
    ("/v1/responses", {"model": "unknown-model", "input": []}, 404, "model_not_found"),
    ("/v1/responses", {"model": "6abc6c6212ee6ff04f7eaee3", "input": []}, 404, "model_not_found"),
    ("/v1/responses", {"model": "deepseek-v4.1-flash", "input": []}, 400, "model_api_mismatch"),
    ("/v1/chat/completions", {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "hello"}]}, 400, "model_api_mismatch"),
])
def test_unknown_and_wrong_protocol_models_do_not_open_cloud(client, monkeypatch, route, body, status, code):
    def unexpected(*args, **kwargs):
        pytest.fail("Rejected model opened a cloud session")
    monkeypatch.setattr(server, "Forward", unexpected)
    response = client.post(route, headers=AUTH, json=body)
    assert response.status_code == status
    assert response.json()["error"]["type"] == code


def test_actual_chunked_body_limit(monkeypatch):
    monkeypatch.setattr(server, "BODY_LIMIT", 32)
    async def exercise():
        async def chunks():
            yield b'{"model":"m","input":"'
            yield b"x" * 32
            yield b'"}'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.create_app(Manager())), base_url="http://test") as client:
            response = await client.post("/v1/responses", headers=AUTH, content=chunks())
            assert response.status_code == 413
    asyncio.run(exercise())


@pytest.mark.parametrize("route,body", [
    ("/v1/responses", {"model": "gpt-6.1-sol", "input": []}),
    ("/v1/chat/completions", {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hello"}]}),
])
def test_upstream_error_is_not_sse_and_preserves_status(client, monkeypatch, route, body):
    channels = []
    class Rejected:
        status = 429
        headers = {"content-type": "application/json", "retry-after": "3"}
        closed = False
        def __init__(self, *args, **kwargs):
            channels.append(self)
        def iter_bytes(self):
            yield b'{"error":{"type":"upstream_error"}}'
        def close(self):
            self.closed = True
    monkeypatch.setattr(server, "Forward", Rejected)
    response = client.post(route, headers=AUTH, json={**body, "stream": True})
    assert response.status_code == 429
    assert response.headers["content-type"] == "application/json"
    assert response.headers["retry-after"] == "3"
    assert response.json()["error"]["type"] == "upstream_error"
    assert channels[0].closed


def test_response_bound_closes_channel(client, monkeypatch):
    monkeypatch.setattr(server, "RESPONSE_LIMIT", 4)
    closed = threading.Event()
    class TooLarge:
        status = 200
        headers = {"content-type": "application/json"}
        def __init__(self, *args, **kwargs):
            pass
        def iter_bytes(self):
            yield b"123"
            yield b"45"
        def close(self):
            closed.set()
    monkeypatch.setattr(server, "Forward", TooLarge)
    response = client.get("/v1/models", headers=AUTH)
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "response_too_large"
    assert closed.is_set()


def test_open_failure_is_sanitized(client, monkeypatch):
    def failed(*args, **kwargs):
        raise BridgeError("Siigo transport failed", "transport_error", 502)
    monkeypatch.setattr(server, "Forward", failed)
    response = client.get("/v1/models", headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"error": {"message": "Siigo transport failed", "type": "transport_error"}}


def test_cancelled_construction_closes_late_channel(monkeypatch):
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Late:
        def __init__(self, *args, cancel_event, **kwargs):
            self.cancel_event = cancel_event
            started.set()
            release.wait(5)
        def close(self):
            assert self.cancel_event.is_set()
            closed.set()
    monkeypatch.setattr(server, "Forward", Late)
    class Connected:
        async def is_disconnected(self):
            return False
    async def exercise():
        task = asyncio.create_task(server._open(Connected(), Manager(), {"action": "models"}))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)
    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_response_owns_channel_before_generator_starts():
    closed = threading.Event()
    class Channel:
        def close(self):
            closed.set()
    async def untouched():
        pytest.fail("Body must not start")
        yield b""
    response = server._OwnedStream(Channel(), untouched())
    async def exercise():
        async def send(message):
            raise RuntimeError("Disconnected before body")
        async def receive():
            return {"type": "http.disconnect"}
        with pytest.raises(RuntimeError):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    asyncio.run(exercise())
    assert closed.is_set()


def test_disconnect_during_construction_closes_late_channel(monkeypatch):
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Late:
        def __init__(self, *args, cancel_event, **kwargs):
            self.cancel_event = cancel_event
            started.set()
            release.wait(5)
        def close(self):
            assert self.cancel_event.is_set()
            closed.set()
    monkeypatch.setattr(server, "Forward", Late)
    class Disconnected:
        async def is_disconnected(self):
            await asyncio.to_thread(started.wait, 2)
            return True
    async def exercise():
        with pytest.raises(server.ClientDisconnect):
            await server._open(Disconnected(), Manager(), {"action": "models"})
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)
    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_exact_body_limit_reaches_semantic_validation(client, monkeypatch):
    # Exact byte count is accepted; one extra whitespace byte exceeds the cap.
    body = b'{"input":[]}'
    monkeypatch.setattr(server, "BODY_LIMIT", len(body))
    accepted = client.post("/v1/responses", headers=AUTH, content=body)
    assert accepted.status_code == 400
    assert client.post("/v1/responses", headers=AUTH, content=body + b" ").status_code == 413
