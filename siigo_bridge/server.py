"""Local bearer-authenticated native model APIs with readable public IDs."""
import asyncio
import hmac
import json
import math
import threading

from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response, StreamingResponse

from .auth import AuthManager, BridgeError, CredentialStore
from .model_registry import resolve_model
from .transport import Forward

BODY_LIMIT = 16 * 1024 * 1024
RESPONSE_LIMIT = 32 * 1024 * 1024


def _error(message, code, status):
    return BridgeError(message, code, status)


async def _close(forward):
    await asyncio.shield(asyncio.to_thread(forward.close))


class _OwnedStream(StreamingResponse):
    """Own the channel even if ASGI disconnects before iteration begins."""
    def __init__(self, forward, content, **kwargs):
        super().__init__(content, **kwargs)
        self.forward = forward

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _close(self.forward)


def _next_chunk(iterator):
    # StopIteration cannot cross an asyncio Future boundary.
    return next(iterator, None)


async def _chunks(forward):
    iterator = iter(forward.iter_bytes())
    while True:
        chunk = await asyncio.to_thread(_next_chunk, iterator)
        if chunk is None:
            return
        yield chunk


async def _disconnected(request):
    while not await request.is_disconnected():
        await asyncio.sleep(0.05)


async def _open(request, manager, payload):
    cancel = threading.Event()

    def construct():
        credentials = manager.credentials(refresh=True)
        return Forward(credentials.access, credentials.machine_id, payload, cancel_event=cancel)

    task = asyncio.create_task(asyncio.to_thread(construct))
    disconnected = asyncio.create_task(_disconnected(request))
    owned = False
    try:
        done, _ = await asyncio.wait((task, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            raise ClientDisconnect()
        forward = await asyncio.shield(task)
        owned = True
        return forward
    finally:
        disconnected.cancel()
        if not owned:
            cancel.set()

            def close_late(completed):
                if not completed.cancelled():
                    try:
                        result = completed.result()
                    except BaseException:
                        return
                    # Keep blocking WebSocket teardown off the event loop.
                    asyncio.create_task(_close(result))

            task.add_done_callback(close_late)


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


async def _bounded_body(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > BODY_LIMIT:
            raise _error("Request body too large", "request_too_large", 413)
        body.extend(chunk)
    try:
        return json.loads(body, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise _error("Invalid JSON body", "invalid_request", 400) from None


async def _reply(request, manager, payload, stream=False):
    forward = await _open(request, manager, payload)
    transferred = False
    try:
        headers = {key: value for key, value in forward.headers.items()
                   if key.lower() in {"content-type", "cache-control", "retry-after"}}
        if stream and 200 <= forward.status < 300:
            headers["content-type"] = "text/event-stream"
            response = _OwnedStream(forward, _chunks(forward), status_code=forward.status,
                                    headers=headers)
            transferred = True
            return response
        body = bytearray()
        async for chunk in _chunks(forward):
            if len(body) + len(chunk) > RESPONSE_LIMIT:
                raise _error("Upstream response too large", "response_too_large", 502)
            body.extend(chunk)
        if not 200 <= forward.status < 300:
            headers["content-type"] = "application/json"
        return Response(bytes(body), status_code=forward.status, headers=headers)
    finally:
        if not transferred:
            await _close(forward)


def create_app(manager: AuthManager | None = None) -> FastAPI:
    manager = manager if manager is not None else AuthManager(CredentialStore())
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(BridgeError)
    async def bridge_error(request, exc):
        return JSONResponse({"error": {"message": exc.message, "type": exc.code}},
                            status_code=exc.http_status)

    async def authorize(request):
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied:
            raise _error("Local bearer key required", "unauthorized", 401)
        credentials = await asyncio.to_thread(manager.credentials)
        if not hmac.compare_digest(supplied.encode("utf-8"), credentials.api_key.encode("utf-8")):
            raise _error("Invalid local bearer key", "unauthorized", 401)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path == "/v1" or request.url.path.startswith("/v1/"):
            try:
                await authorize(request)
            except BridgeError as exc:
                return await bridge_error(request, exc)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "protocol": "responses+chat-completions"}

    @app.get("/v1/models")
    async def models(request: Request):
        return await _reply(request, manager, {"action": "models"})

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await _bounded_body(request)
        if (not isinstance(body, dict) or not isinstance(body.get("model"), str)
                or not body["model"].strip() or "input" not in body
                or ("stream" in body and not isinstance(body["stream"], bool))):
            raise _error("Expected model, input and optional boolean stream", "invalid_request", 400)
        body["model"] = resolve_model(body["model"], "openai-responses")
        return await _reply(request, manager, {"action": "responses", "body": body},
                            stream=body.get("stream") is True)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await _bounded_body(request)
        if (not isinstance(body, dict) or not isinstance(body.get("model"), str)
                or not body["model"].strip() or not isinstance(body.get("messages"), list)
                or not body["messages"]
                or ("stream" in body and not isinstance(body["stream"], bool))):
            raise _error("Expected model, nonempty messages and optional boolean stream", "invalid_request", 400)
        body["model"] = resolve_model(body["model"], "openai-completions")
        return await _reply(request, manager, {"action": "chat", "body": body},
                            stream=body.get("stream") is True)

    return app
