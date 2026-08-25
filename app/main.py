"""
Gatekeeper — FastAPI entrypoint.

Routes:
  GET  /healthz                   public, no auth, backend reachability
  GET  /v1/me                     authenticated, the calling app's record
  POST /v1/chat/completions       authenticated, OpenAI-compatible chat (STANDARD)
  GET  /v1/models                 authenticated, OpenAI-compatible model list
  POST /v1/classify               authenticated, MPPT classifier (placeholder)
  POST /v1/generate               authenticated, LEGACY single-prompt (deprecated)
  POST /v1/chat                   authenticated, LEGACY chat (deprecated)

/v1/chat/completions is the standard endpoint: it speaks the OpenAI Chat
Completions protocol, so the official `openai` SDK works against this gateway
with only a base_url change. It forwards to Ollama's own OpenAI-compatible
backend and returns that response unchanged. Streaming (stream=true) is
supported via SSE passthrough.

Authentication accepts three headers (see auth.py): api-key (Azure style),
Authorization: Bearer (OpenAI style), and X-API-Key (curl convenience).

Every authenticated request:
  1. has its key validated against the keys file (auth.authenticate)
  2. is checked against the app's rate limit (token bucket)
  3. is forwarded to the appropriate backend
  4. is logged to /var/log/gatekeeper/requests.jsonl with no secrets

The gateway is the only thing exposed publicly. Backends are bound to
127.0.0.1 on the same host.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import authenticate
from .config import settings
from .models import (
    ChatRequest,
    ChatResponse,
    ClassifyRequest,
    ClassifyResponse,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    OpenAIChatRequest,
)
from .proxy import (
    call_classifier,
    call_ollama_chat,
    call_ollama_generate,
    call_ollama_openai,
    close_client,
    list_ollama_models,
    ping_backend,
    stream_ollama_openai,
)
from .rate_limit import try_consume
from .storage import AppRecord, append_request_log

logging.basicConfig(
    level=settings.GATEKEEPER_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("gatekeeper")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("gatekeeper starting on %s:%s", settings.GATEKEEPER_HOST, settings.GATEKEEPER_PORT)
    log.info("ollama backend:     %s", settings.OLLAMA_BASE_URL)
    log.info("classifier backend: %s", settings.CLASSIFIER_BASE_URL)
    log.info("keys file:          %s", settings.GATEKEEPER_KEYS_FILE)
    log.info("request log:        %s", settings.GATEKEEPER_LOG_FILE)
    yield
    await close_client()
    log.info("gatekeeper shutting down")


app = FastAPI(
    title="Gatekeeper",
    description="Authenticated, rate-limited gateway for self-hosted LLM/ML model backends.",
    version="0.4.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)


def _enforce_rate_limit(app_record: AppRecord) -> None:
    allowed, retry_after = try_consume(app_record.app_name, app_record.rate_limit_per_min)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"rate limit exceeded for {app_record.app_name}; retry in {retry_after:.1f}s",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def _enforce_model_allowed(app_record: AppRecord, model: str) -> None:
    """Reject a request whose model is not permitted for this key.

    An empty ``allowed_models`` means no restriction (all models allowed) —
    so keys issued before this feature, or issued without --models, keep
    working unchanged. When the list is non-empty, the requested model must
    match one of its entries exactly, otherwise 403.
    """
    allowed = app_record.allowed_models
    if allowed and model not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"model '{model}' is not permitted for this API key; "
                f"allowed: {', '.join(allowed)}"
            ),
        )


def _log_request(
    *,
    request_id: str,
    app_record: AppRecord | None,
    method: str,
    path: str,
    status_code: int,
    latency_ms: int,
    backend: str | None = None,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    error: str | None = None,
) -> None:
    record = {
        "ts": time.time(),
        "request_id": request_id,
        "app": app_record.app_name if app_record else None,
        "method": method,
        "path": path,
        "status": status_code,
        "latency_ms": latency_ms,
        "backend": backend,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "error": error,
    }
    append_request_log(record)


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.started_at = time.monotonic()
    response = await call_next(request)
    response.headers["x-request-id"] = request.state.request_id
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    latency_ms = int((time.monotonic() - request.state.started_at) * 1000)
    _log_request(
        request_id=request.state.request_id,
        app_record=getattr(request.state, "app_record", None),
        method=request.method,
        path=request.url.path,
        status_code=exc.status_code,
        latency_ms=latency_ms,
        error=str(exc.detail),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "request_id": request.state.request_id},
        headers=exc.headers or {},
    )


@app.get("/healthz", response_model=HealthResponse, tags=["meta"])
async def healthz() -> HealthResponse:
    ollama = await ping_backend(settings.OLLAMA_BASE_URL)
    classifier = await ping_backend(settings.CLASSIFIER_BASE_URL)
    overall = "ok" if ollama == "ok" or classifier == "ok" else "degraded"
    return HealthResponse(
        status=overall,
        backends={"ollama": ollama, "classifier": classifier},
    )


@app.get("/v1/me", tags=["meta"])
async def me(request: Request, app_record: AppRecord = Depends(authenticate)):
    request.state.app_record = app_record
    return {
        "app": app_record.app_name,
        "active": app_record.active,
        "rate_limit_per_min": app_record.rate_limit_per_min,
        "created_at": app_record.created_at,
    }


@app.post(
    "/v1/generate",
    response_model=GenerateResponse,
    tags=["legacy (deprecated)"],
    deprecated=True,
)
async def generate(
    body: GenerateRequest,
    request: Request,
    app_record: AppRecord = Depends(authenticate),
) -> GenerateResponse:
    request.state.app_record = app_record
    _enforce_rate_limit(app_record)
    _enforce_model_allowed(app_record, body.model)

    started = time.monotonic()
    try:
        result = await call_ollama_generate(body)
    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="POST",
            path="/v1/generate",
            status_code=502,
            latency_ms=latency_ms,
            backend="ollama",
            model=body.model,
            error=f"backend {exc.response.status_code}: {exc.response.text[:200]}",
        )
        raise HTTPException(status_code=502, detail="upstream model backend error")
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="POST",
            path="/v1/generate",
            status_code=502,
            latency_ms=latency_ms,
            backend="ollama",
            model=body.model,
            error=f"backend transport: {type(exc).__name__}",
        )
        raise HTTPException(status_code=502, detail="upstream model backend unreachable")

    latency_ms = int((time.monotonic() - started) * 1000)
    _log_request(
        request_id=request.state.request_id,
        app_record=app_record,
        method="POST",
        path="/v1/generate",
        status_code=200,
        latency_ms=latency_ms,
        backend="ollama",
        model=body.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
    return result


@app.post(
    "/v1/chat",
    response_model=ChatResponse,
    tags=["legacy (deprecated)"],
    deprecated=True,
)
async def chat(
    body: ChatRequest,
    request: Request,
    app_record: AppRecord = Depends(authenticate),
) -> ChatResponse:
    request.state.app_record = app_record
    _enforce_rate_limit(app_record)
    _enforce_model_allowed(app_record, body.model)

    started = time.monotonic()
    try:
        result = await call_ollama_chat(body)
    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="POST",
            path="/v1/chat",
            status_code=502,
            latency_ms=latency_ms,
            backend="ollama",
            model=body.model,
            error=f"backend {exc.response.status_code}: {exc.response.text[:200]}",
        )
        raise HTTPException(status_code=502, detail="upstream model backend error")
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="POST",
            path="/v1/chat",
            status_code=502,
            latency_ms=latency_ms,
            backend="ollama",
            model=body.model,
            error=f"backend transport: {type(exc).__name__}",
        )
        raise HTTPException(status_code=502, detail="upstream model backend unreachable")

    latency_ms = int((time.monotonic() - started) * 1000)
    _log_request(
        request_id=request.state.request_id,
        app_record=app_record,
        method="POST",
        path="/v1/chat",
        status_code=200,
        latency_ms=latency_ms,
        backend="ollama",
        model=body.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
    )
    return result


@app.post("/v1/chat/completions", tags=["chat (OpenAI-compatible)"])
@app.post("/chat/completions", include_in_schema=False)  # alias if base_url omits /v1
async def openai_chat_completions(
    body: OpenAIChatRequest,
    request: Request,
    app_record: AppRecord = Depends(authenticate),
):
    """
    OpenAI-compatible chat completions. **This is the standard endpoint** for
    all LLM access.

    Point the official `openai` SDK at this gateway:

        from openai import OpenAI
        client = OpenAI(base_url="https://<host>/v1", api_key="sk-gatekeeper-...")
        client.chat.completions.create(model="qwen3.5:9b", messages=[...])

    Request and response match OpenAI's /v1/chat/completions exactly (the
    request is forwarded to Ollama's OpenAI-compatible backend and its
    response returned unchanged). Supports streaming via stream=true.
    """
    request.state.app_record = app_record
    _enforce_rate_limit(app_record)
    _enforce_model_allowed(app_record, body.model)

    # Forward only what the client actually sent, plus required fields.
    payload = body.model_dump(exclude_unset=True, exclude_none=True)
    payload.setdefault("model", body.model)
    payload.setdefault("messages", body.messages)

    # --- thinking-off default for reasoning models ----------------------
    # qwen3.5, gemma4, and other reasoning models spend their token budget on
    # hidden "thinking" by default. With a normal max_tokens that leaves the
    # user-visible `content` empty (the answer never gets emitted). Most app
    # use cases — work orders, summaries, classification — don't want that.
    #
    # `reasoning_effort: "none"` is the OpenAI-standard control and is the
    # value confirmed to suppress thinking on this deployment's Ollama for
    # both gemma4 and qwen3.5. We set it unless the caller opted in. A caller
    # who WANTS reasoning sends reasoning_effort ("low"/"medium"/"high")
    # themselves and we leave their choice untouched.
    #
    # We deliberately do NOT inject Ollama's native `think` field here: this is
    # the OpenAI-compatible path, and an unrecognised top-level field can be
    # rejected by strict OpenAI servers. reasoning_effort is the portable knob.
    if "reasoning_effort" not in payload:
        payload["reasoning_effort"] = "none"

    # ---- streaming path -------------------------------------------------
    if body.stream:
        # We log once up front for streamed requests; token counts aren't
        # known until the stream ends, so they're omitted here.
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="POST",
            path="/v1/chat/completions",
            status_code=200,
            latency_ms=0,
            backend="ollama",
            model=body.model,
            error="streamed (token counts not captured)",
        )

        async def event_stream():
            try:
                async for chunk in stream_ollama_openai(payload):
                    yield chunk
            except httpx.HTTPError:
                # Connection died before/whilst streaming. Emit a final SSE
                # error frame so a well-behaved client sees a clean end.
                yield b'data: {"error":"upstream model backend unreachable"}\n\n'

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"x-request-id": request.state.request_id},
        )

    # ---- non-streaming path --------------------------------------------
    started = time.monotonic()
    try:
        data = await call_ollama_openai(payload)
    except httpx.HTTPStatusError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="POST",
            path="/v1/chat/completions",
            status_code=502,
            latency_ms=latency_ms,
            backend="ollama",
            model=body.model,
            error=f"backend {exc.response.status_code}: {exc.response.text[:200]}",
        )
        raise HTTPException(status_code=502, detail="upstream model backend error")
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="POST",
            path="/v1/chat/completions",
            status_code=502,
            latency_ms=latency_ms,
            backend="ollama",
            model=body.model,
            error=f"backend transport: {type(exc).__name__}",
        )
        raise HTTPException(status_code=502, detail="upstream model backend unreachable")

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = data.get("usage") or {}
    _log_request(
        request_id=request.state.request_id,
        app_record=app_record,
        method="POST",
        path="/v1/chat/completions",
        status_code=200,
        latency_ms=latency_ms,
        backend="ollama",
        model=data.get("model", body.model),
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
    )
    # Return Ollama's OpenAI-shaped body unchanged, plus our request id header.
    return JSONResponse(content=data, headers={"x-request-id": request.state.request_id})


@app.get("/v1/models", tags=["chat (OpenAI-compatible)"])
async def openai_models(
    request: Request,
    app_record: AppRecord = Depends(authenticate),
):
    """
    OpenAI-compatible model list. Returns {"object":"list","data":[...]} of
    models available on the backend. Some OpenAI tooling calls this to
    discover or validate model names.
    """
    request.state.app_record = app_record
    started = time.monotonic()
    try:
        data = await list_ollama_models()
    except httpx.HTTPError:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="GET",
            path="/v1/models",
            status_code=502,
            latency_ms=latency_ms,
            backend="ollama",
            error="backend unreachable",
        )
        raise HTTPException(status_code=502, detail="upstream model backend unreachable")

    # If this key is restricted to specific models, only list those — so the
    # discovery response matches what the key can actually call. No restriction
    # (empty allowed_models) returns the full list unchanged.
    if app_record.allowed_models and isinstance(data, dict) and isinstance(data.get("data"), list):
        allowed = set(app_record.allowed_models)
        data = {
            **data,
            "data": [m for m in data["data"] if m.get("id") in allowed],
        }

    latency_ms = int((time.monotonic() - started) * 1000)
    _log_request(
        request_id=request.state.request_id,
        app_record=app_record,
        method="GET",
        path="/v1/models",
        status_code=200,
        latency_ms=latency_ms,
        backend="ollama",
    )
    return JSONResponse(content=data, headers={"x-request-id": request.state.request_id})


@app.post("/v1/classify", response_model=ClassifyResponse, tags=["classifier"])
async def classify(
    body: ClassifyRequest,
    request: Request,
    app_record: AppRecord = Depends(authenticate),
) -> ClassifyResponse:
    request.state.app_record = app_record
    _enforce_rate_limit(app_record)

    started = time.monotonic()
    try:
        result = await call_classifier(body)
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _log_request(
            request_id=request.state.request_id,
            app_record=app_record,
            method="POST",
            path="/v1/classify",
            status_code=502,
            latency_ms=latency_ms,
            backend="classifier",
            error=f"backend transport: {type(exc).__name__}",
        )
        raise HTTPException(status_code=502, detail="classifier backend unreachable")

    latency_ms = int((time.monotonic() - started) * 1000)
    _log_request(
        request_id=request.state.request_id,
        app_record=app_record,
        method="POST",
        path="/v1/classify",
        status_code=200,
        latency_ms=latency_ms,
        backend="classifier",
    )
    return result
