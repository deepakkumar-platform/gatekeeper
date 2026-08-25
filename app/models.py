"""
Request and response schemas.

The gateway intentionally exposes a *narrow*, opinionated surface — not a
direct passthrough to Ollama. This way the dev team has a stable contract
that survives swapping the backend (e.g. Ollama -> vLLM later) without
breaking their code.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class GenerateRequest(BaseModel):
    model: str = Field(..., description="Backend model name, e.g. 'qwen3.5:9b'")
    prompt: str = Field(..., description="User prompt")
    system: Optional[str] = Field(None, description="Optional system prompt")
    max_tokens: int = Field(512, ge=1, le=8192)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    stream: bool = Field(False, description="If true, response is text/event-stream")
    think: bool = Field(
        False,
        description="Enable model thinking. Off by default for predictable latency and output.",
    )


class GenerateResponse(BaseModel):
    model: str
    response: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


class Message(BaseModel):
    role: str = Field(..., description="One of: system, user, assistant")
    content: str = Field(..., description="Message text")


class ChatRequest(BaseModel):
    """
    Multi-turn chat. The gateway and model are stateless — the calling app
    must store its own conversation history and resend it on each request.
    """

    model: str = Field(..., description="Backend model name, e.g. 'qwen3.5:9b'")
    messages: list[Message] = Field(
        ..., min_length=1, description="Conversation history, oldest first"
    )
    max_tokens: int = Field(512, ge=1, le=8192)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    think: bool = Field(False, description="Enable model thinking")


class ChatResponse(BaseModel):
    model: str
    message: Message
    tokens_in: int
    tokens_out: int
    latency_ms: int


class ClassifyRequest(BaseModel):
    """
    Placeholder for the upcoming MPPT fault classifier. The shape will firm
    up once the data scientist nails the window length. Kept loose for now.
    """

    plant: str
    string_id: str
    window: list[list[float]] = Field(
        ..., description="2D array of MPPT readings [time_steps][channels]"
    )


class ClassifyResponse(BaseModel):
    plant: str
    string_id: str
    fault: str
    confidence: float
    probabilities: dict[str, float]
    latency_ms: int


class HealthResponse(BaseModel):
    status: str
    backends: dict[str, str]


# ---------------------------------------------------------------------------
# OpenAI-compatible schema for POST /v1/chat/completions
#
# This is the standards-compatible surface. The official `openai` SDK (and any
# OpenAI-compatible client) works against this gateway with only a base_url
# change.
#
# Design: this request is forwarded almost verbatim to Ollama's own
# OpenAI-compatible endpoint (/v1/chat/completions), and Ollama's response is
# returned unchanged. So we keep the model permissive:
#   - `messages` is list[dict], not a strict shape, so multimodal content
#     (content as an array of text/image parts) and tool messages pass through.
#   - extra="allow" lets through any other OpenAI field the client sends
#     (tools, tool_choice, response_format, seed, stream_options, etc.) without
#     a 422. We don't have to enumerate the entire OpenAI surface.
# Only `model` and `messages` are required and lightly validated; everything
# else is the client's and the model's concern.
# ---------------------------------------------------------------------------


class OpenAIChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(..., description="Model name, e.g. 'qwen3.5:9b'")
    messages: list[dict] = Field(
        ..., min_length=1, description="OpenAI-format messages, oldest first"
    )
    # Documented common options (all optional; forwarded only if sent).
    max_tokens: Optional[int] = Field(None, ge=1, le=32768)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    stream: Optional[bool] = Field(
        None, description="If true, response is a streamed text/event-stream (SSE)"
    )
