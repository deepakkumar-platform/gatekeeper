"""
Backend proxy.

The gateway terminates the dev team's request, then makes its own HTTP
call to the right backend. This indirection is what gives us:
  - a stable client-facing schema even if the backend changes
  - timeout control
  - per-request token / latency measurement
  - the ability to swap Ollama for vLLM/Triton later without breaking apps
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from .config import settings
from .models import (
    ChatRequest,
    ChatResponse,
    ClassifyRequest,
    ClassifyResponse,
    GenerateRequest,
    GenerateResponse,
    Message,
)

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.GATEKEEPER_REQUEST_TIMEOUT_SECONDS)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def call_ollama_generate(req: GenerateRequest) -> GenerateResponse:
    """
    Map our GenerateRequest to Ollama's /api/generate.

    Ollama returns rich metadata (prompt_eval_count, eval_count) which we
    surface as tokens_in / tokens_out so the dev team and the operator
    both have the numbers they need.

    `think` defaults to False because qwen3.5 (and other thinking models)
    otherwise burn the entire token budget on hidden reasoning, leaving
    nothing for the user-visible response.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate"
    payload = {
        "model": req.model,
        "prompt": req.prompt,
        "system": req.system,
        "stream": False,
        "think": req.think,
        "options": {
            "num_predict": req.max_tokens,
            "temperature": req.temperature,
        },
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    started = time.monotonic()
    resp = await get_client().post(url, json=payload)
    latency_ms = int((time.monotonic() - started) * 1000)
    resp.raise_for_status()
    data = resp.json()

    return GenerateResponse(
        model=data.get("model", req.model),
        response=data.get("response", ""),
        tokens_in=int(data.get("prompt_eval_count", 0)),
        tokens_out=int(data.get("eval_count", 0)),
        latency_ms=latency_ms,
    )


async def call_ollama_chat(req: ChatRequest) -> ChatResponse:
    """
    Map our ChatRequest to Ollama's /api/chat.

    Unlike /api/generate which takes a single prompt, /api/chat takes a
    full message array — system + user + assistant turns in order. The
    caller is responsible for storing conversation history and resending
    it; the gateway and the model are stateless.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    payload = {
        "model": req.model,
        "messages": [m.model_dump() for m in req.messages],
        "stream": False,
        "think": req.think,
        "options": {
            "num_predict": req.max_tokens,
            "temperature": req.temperature,
        },
    }

    started = time.monotonic()
    resp = await get_client().post(url, json=payload)
    latency_ms = int((time.monotonic() - started) * 1000)
    resp.raise_for_status()
    data = resp.json()

    msg = data.get("message") or {}
    return ChatResponse(
        model=data.get("model", req.model),
        message=Message(
            role=msg.get("role", "assistant"),
            content=msg.get("content", ""),
        ),
        tokens_in=int(data.get("prompt_eval_count", 0)),
        tokens_out=int(data.get("eval_count", 0)),
        latency_ms=latency_ms,
    )


async def call_ollama_openai(payload: dict) -> dict:
    """
    OpenAI-compatible chat completion (non-streaming).

    Forwards the request to Ollama's own OpenAI-compatible endpoint
    (/v1/chat/completions) and returns Ollama's response unchanged. Ollama
    produces the full OpenAI envelope (id, object, created, choices, usage),
    so the official `openai` SDK parses it directly. We do not reshape it.

    The caller reads `usage` from the returned dict for audit logging.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/v1/chat/completions"
    resp = await get_client().post(url, json=payload)
    resp.raise_for_status()
    return resp.json()


async def stream_ollama_openai(payload: dict):
    """
    OpenAI-compatible chat completion (streaming).

    Async generator that proxies the Server-Sent Events stream from Ollama's
    /v1/chat/completions straight through to the client, byte for byte. The
    `openai` SDK consumes this when called with stream=True.

    On a connection/transport error before any bytes are sent, the exception
    propagates to the caller (which turns it into a 502). Once streaming has
    begun, partial output may already have reached the client; that's inherent
    to streaming and matches how every OpenAI-compatible server behaves.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/v1/chat/completions"
    async with get_client().stream("POST", url, json=payload) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_raw():
            yield chunk


async def list_ollama_models() -> dict:
    """
    OpenAI-compatible model list. Proxies Ollama's /v1/models, which already
    returns the OpenAI {"object":"list","data":[...]} shape. Lets clients and
    tooling discover which models are loadable.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/v1/models"
    resp = await get_client().get(url)
    resp.raise_for_status()
    return resp.json()


async def call_classifier(req: ClassifyRequest) -> ClassifyResponse:
    """
    Placeholder forwarding to the FastAPI classifier server (not yet built).

    Once the data scientist hands over the model.pt, a separate small
    FastAPI service will load it and expose /predict. This function then
    just becomes a thin POST.
    """
    url = f"{settings.CLASSIFIER_BASE_URL.rstrip('/')}/predict"
    payload = req.model_dump()

    started = time.monotonic()
    resp = await get_client().post(url, json=payload)
    latency_ms = int((time.monotonic() - started) * 1000)
    resp.raise_for_status()
    data = resp.json()

    return ClassifyResponse(
        plant=req.plant,
        string_id=req.string_id,
        fault=data.get("fault", "unknown"),
        confidence=float(data.get("confidence", 0.0)),
        probabilities=data.get("probabilities", {}),
        latency_ms=latency_ms,
    )


async def ping_backend(base_url: str) -> str:
    """Return 'ok' if backend root responds, otherwise the error string."""
    try:
        resp = await get_client().get(base_url.rstrip("/") + "/", timeout=5.0)
        return "ok" if resp.status_code < 500 else f"http {resp.status_code}"
    except Exception as exc:
        return f"unreachable: {type(exc).__name__}"
