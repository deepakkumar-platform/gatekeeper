# Part 3 — The Code, File by File (v0.3.1)

This is the final document in the series. Part 1 covered the infrastructure (VPC, GPU, OS). Part 2 covered the gateway's API and features as a black box (what it does and why). This document opens the box: every Python file, the interesting functions, the design decisions visible in the code, and what you'd change to extend or replace each piece.

**This reflects v0.3.1** — the version where `/v1/chat/completions` (OpenAI-compatible) became the standard endpoint, three auth headers are accepted, and reasoning models are handled by default. Where a file changed materially since the early build, the doc says so.

**Read this with the source open in another window.** I'll quote enough to make each section self-contained, but seeing the full files alongside makes things click faster.

**Covered here (dependency order, simplest first):**

- Directory layout recap
- `app/__init__.py`
- `app/config.py` — environment-driven settings
- `app/models.py` — Pydantic schemas (incl. the OpenAI passthrough model)
- `app/storage.py` — keys file + audit log (still the most interesting file)
- `app/rate_limit.py` — token bucket
- `app/auth.py` — three-header auth + OpenAPI security scheme
- `app/proxy.py` — backend forwarders (incl. the OpenAI passthrough + streaming)
- `app/main.py` — the FastAPI assembly, routes, the reasoning-off default
- `scripts/gatekeeper-keys` — admin CLI
- A trace of one OpenAI request through every file
- What to change for common extensions
- Closing notes on evolution and testing

---

## 0. Project layout

```
/opt/gatekeeper/
├── app/
│   ├── __init__.py          empty; marks app/ as a Python package
│   ├── config.py            ~25 lines — environment loader
│   ├── models.py            ~127 lines — Pydantic schemas
│   ├── storage.py           ~140 lines — keys file + audit log
│   ├── rate_limit.py        ~70 lines — token bucket
│   ├── auth.py              ~90 lines — three-header auth + security scheme
│   ├── proxy.py             ~211 lines — backend forwarders
│   └── main.py              ~518 lines — FastAPI assembly, routes, middleware
└── scripts/
    └── gatekeeper-keys         ~170 lines — admin CLI
```

About 1100 lines of application code. The whole thing still fits in your head once you've read it through.

The structure is deliberately flat — no nested packages, no clever abstractions. For ~1100 lines, depth costs more than it saves.

**What changed from the early build to v0.3.1:** `models.py`, `auth.py`, `proxy.py`, and `main.py` all grew to support the OpenAI-compatible surface. `config.py`, `storage.py`, `rate_limit.py`, and the CLI are essentially unchanged — they were right the first time.

---

## 1. `app/__init__.py`

```python
# (empty)
```

Marks `app/` as a Python package so `from .config import settings` and `from .storage import ...` work. Without it, Python 3 still mostly works thanks to implicit namespace packages, but explicit is better. Keep it empty — putting code here is a common antipattern (circular imports, surprising side effects on import).

---

## 2. `app/config.py` — settings loader

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    GATEKEEPER_HOST: str = "0.0.0.0"
    GATEKEEPER_PORT: int = 8080

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    CLASSIFIER_BASE_URL: str = "http://127.0.0.1:9000"

    GATEKEEPER_KEYS_FILE: str = "/etc/gatekeeper/keys.yaml"
    GATEKEEPER_LOG_FILE: str = "/var/log/gatekeeper/requests.jsonl"

    GATEKEEPER_DEFAULT_RATE_LIMIT_PER_MIN: int = 60
    GATEKEEPER_REQUEST_TIMEOUT_SECONDS: int = 120

    GATEKEEPER_LOG_LEVEL: str = "INFO"


settings = Settings()
```

### What this does

`BaseSettings` is pydantic's class for environment-driven config. When `Settings()` is called at import time, each field is populated from (1) the matching environment variable, (2) a line in `.env`, or (3) the default in the class. `extra="ignore"` means unknown env vars don't error — production-friendly.

### The singleton pattern

```python
settings = Settings()
```

Creating the instance at module level means every other module that does `from .config import settings` gets the *same* object. Settings are read once at startup; to change a value, edit the env file and restart.

### Type annotations matter

Each field has a Python type. Pydantic uses these to parse and validate the environment variable, so `GATEKEEPER_PORT=8080` (a string from the OS) becomes an `int`, and a bad value fails at startup with a clear error instead of crashing mysteriously later.

### Note for v0.3.1

Nothing about the OpenAI endpoint required a new setting. `OLLAMA_BASE_URL` already points at Ollama, and the OpenAI-compatible path just appends `/v1/chat/completions` to it (see `proxy.py`). That's a small but real payoff of keeping config minimal: new features didn't need new config knobs.

---

## 3. `app/models.py` — Pydantic schemas

The gateway's *contract* — what comes in, what goes out. There are two families here:

1. **The legacy shapes** (`GenerateRequest`/`GenerateResponse`, `ChatRequest`/`ChatResponse`) — our own narrow schema, used by the deprecated `/v1/generate` and `/v1/chat` endpoints.
2. **The OpenAI passthrough** (`OpenAIChatRequest`) — the v0.3.1 addition, used by the standard `/v1/chat/completions`.

### The legacy request/response (still present, still validated)

```python
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
```

`Field(512, ge=1, le=8192)` — the `ge`/`le` are validation constraints. A client sending `max_tokens: 999999` gets a 422 before any code runs. `description=` strings populate the `/docs` OpenAPI page.

`think` defaults to `False` because these models are reasoners — see the discussion in `main.py` about why thinking-on with a low token budget yields empty output. On the legacy `/v1/generate` path, this default is applied directly to Ollama's native `think` field.

`ChatRequest`/`ChatResponse` are the same idea for the legacy multi-turn endpoint, using a strict `Message` shape:

```python
class Message(BaseModel):
    role: str = Field(..., description="One of: system, user, assistant")
    content: str = Field(..., description="Message text")
```

Note `content: str` — a *string*. This is fine for the legacy endpoint, but it's exactly why the OpenAI endpoint needed a different model (multimodal content is an *array*, not a string). Which brings us to:

### The OpenAI passthrough model — the v0.3.1 heart of the file

```python
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
```

Three deliberate choices, each of which matters for OpenAI compatibility:

**`model_config = ConfigDict(extra="allow")`.** The OpenAI SDK sends far more fields than we enumerate — `tools`, `tool_choice`, `response_format`, `seed`, `stop`, `presence_penalty`, `frequency_penalty`, `stream_options`, `logprobs`, and more. With the pydantic default (`extra="ignore"` or `"forbid"`), unknown fields would be dropped or rejected. `extra="allow"` *keeps* them, and — critically — they're included in `model_dump()`, so they get forwarded to Ollama. We don't have to know or maintain the full OpenAI surface; whatever the SDK sends flows through.

**`messages: list[dict]`, not `list[Message]`.** A strict `Message` with `content: str` would 422 on multimodal requests, where `content` is an array of typed parts (`[{"type":"text",...},{"type":"image_url",...}]`). Since qwen3.5 and gemma4 are multimodal and the dev team may send images, we keep `messages` loose. Ollama validates the contents; we just ensure it's a non-empty list.

**Only `model` and `messages` are required.** Everything else is optional and forwarded only if the client sent it. This makes us a faithful *passthrough* rather than an opinionated reshaper — the whole point of the OpenAI endpoint is that it behaves exactly like OpenAI, and Ollama's own OpenAI backend produces the response envelope.

### Why there's no `OpenAIChatResponse` model

Earlier drafts had one (with `choices`, `usage`, etc.), and the code hand-built the envelope from Ollama's native `/api/chat` output. v0.3.1 dropped that: the OpenAI route forwards to Ollama's *own* OpenAI-compatible endpoint and returns its JSON unchanged. Ollama produces a real OpenAI envelope, so reconstructing it ourselves was redundant and fragile (it broke on multimodal, and any new OpenAI response field would need code changes). No response model = nothing to keep in sync. The route returns a raw `JSONResponse`.

**Lesson visible here:** when you're a proxy in front of something that already speaks the target protocol, *proxy it* — don't re-implement the protocol. Less code, fewer bugs, automatic forward-compatibility.

### `ClassifyRequest` — designed before the backend exists

```python
class ClassifyRequest(BaseModel):
    plant: str
    string_id: str
    window: list[list[float]] = Field(
        ..., description="2D array of MPPT readings [time_steps][channels]"
    )
```

Shape designed in conversation with the future data-scientist work, before the model exists. We ship `/v1/classify` returning 502s today; dev teams wire their integration; when the model lands, only `proxy.py:call_classifier` changes. Schema-first design unblocks parallel work.

---

## 4. `app/storage.py` — the most interesting file (unchanged in v0.3.1)

This file didn't need to change for the OpenAI work — the keys file, hashing, hot reload, and audit log are backend-agnostic. It's still where the most design thought lives. ~140 lines handling the keys file (load, parse, hot reload, hash-lookup) and the audit log (append-only, thread-safe, failure-tolerant).

### The dataclasses

```python
@dataclass
class AppRecord:
    app_name: str
    key_hash: str
    active: bool
    rate_limit_per_min: int
    created_at: str
    notes: str = ""


@dataclass
class KeysIndex:
    """Hash -> AppRecord lookup. Rebuilt whenever the file changes on disk."""

    by_hash: dict[str, AppRecord] = field(default_factory=dict)
    loaded_at: float = 0.0
    source_mtime: float = 0.0
```

`AppRecord` is one app's data; `KeysIndex` is the loaded-into-memory snapshot of the whole keys file.

**Why dataclasses, not dicts?** Type-checker catches `record.app_naem` typos at lint time (a dict typo is a runtime `KeyError`); IDE autocomplete works; the fields are self-documenting. **Why not Pydantic here?** Pydantic is for *external boundaries* (HTTP). For internal in-memory state, plain dataclasses are lighter and don't validate on every instantiation.

### The hash function

```python
def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
```

One line, one job. Separate function (not inlined) because the *same* function is used in `lookup_by_plaintext` (gateway) AND in `gatekeeper-keys` (CLI). They must produce identical hashes; centralizing means they can't drift. Also easy to swap (e.g. to HMAC with a salt later) and easy to unit-test.

### Loading the keys file

```python
def _load_keys_file(path: str) -> KeysIndex:
    p = Path(path)
    if not p.exists():
        return KeysIndex(loaded_at=time.time(), source_mtime=0.0)

    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    index = KeysIndex(loaded_at=time.time(), source_mtime=p.stat().st_mtime)
    for app_name, cfg in (data.get("apps") or {}).items():
        key_hash = cfg.get("key_hash")
        if not key_hash or key_hash == "REPLACE_WITH_SHA256_HEX":
            continue
        index.by_hash[key_hash] = AppRecord(
            app_name=app_name,
            key_hash=key_hash,
            active=bool(cfg.get("active", True)),
            rate_limit_per_min=int(
                cfg.get("rate_limit_per_min", settings.GATEKEEPER_DEFAULT_RATE_LIMIT_PER_MIN)
            ),
            created_at=str(cfg.get("created_at", "")),
            notes=str(cfg.get("notes", "")),
        )
    return index
```

Deliberate choices: **missing file is not an error** (fresh install serves `/healthz`, first authed request 401s — resilient); **`yaml.safe_load`** never executes embedded Python (always safe_load untrusted YAML); the **placeholder filter** skips template entries; **per-app default fallback** to `GATEKEEPER_DEFAULT_RATE_LIMIT_PER_MIN`; and the index is **rebuilt fresh each load** (no merge logic, no consistency bugs — the file is tiny).

### Hot reload via mtime

```python
def reload_if_changed() -> None:
    """Reload the keys file if its mtime has advanced. Cheap to call often."""
    global _keys_index
    path = settings.GATEKEEPER_KEYS_FILE
    try:
        mtime = Path(path).stat().st_mtime if Path(path).exists() else 0.0
    except OSError:
        return
    with _keys_lock:
        if mtime != _keys_index.source_mtime:
            _keys_index = _load_keys_file(path)
```

The hot-reload mechanism in 7 lines. Every authenticated request calls this. In steady state (file unchanged) it's one `os.stat()` syscall (~1µs) — negligible. When the CLI rewrites the file, mtime advances and the next request reloads. No inotify, no signals, no IPC.

This is what makes `gatekeeper-keys disable`, `rotate`, and rate-limit edits take effect in ~1 second with no restart. **This behavior is the same regardless of endpoint** — an OpenAI call and a legacy call both go through the same key lookup and both see key changes immediately.

### The lookup function — the heart of authentication

```python
def lookup_by_plaintext(plaintext_key: str) -> Optional[AppRecord]:
    """Hash the incoming key and return the AppRecord, or None."""
    reload_if_changed()
    key_hash = _hash_key(plaintext_key)
    with _keys_lock:
        return _keys_index.by_hash.get(key_hash)
```

Four lines embodying the whole auth-data design: reload if changed, hash the incoming key, look up by hash, return record or None. **This is the seam to swap for AWS Secrets Manager / Redis later** — nothing above it cares *how* the lookup happens.

### The audit log appender

```python
def append_request_log(record: dict) -> None:
    try:
        path = settings.GATEKEEPER_LOG_FILE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with _log_lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
```

`os.makedirs(..., exist_ok=True)` handles the fresh-install case. `separators=(",", ":")` writes compact JSON (smaller files, faster jq). **`except Exception: pass` is deliberate: a broken log path must NEVER break the request path.** You lose some audit visibility temporarily — far better than a global outage caused by a logging failure. Ugly in static review, correct in production.

---

## 5. `app/rate_limit.py` — token bucket (unchanged in v0.3.1)

The token bucket in ~70 lines. Didn't change for the OpenAI work — every endpoint (OpenAI, legacy, classify) calls the same limiter.

```python
@dataclass
class Bucket:
    capacity: float
    tokens: float
    refill_per_sec: float
    last_refill: float


def try_consume(app_name: str, rate_per_min: int, cost: float = 1.0) -> tuple[bool, float]:
    with _lock:
        bucket = _get_or_create(app_name, rate_per_min)
        now = time.monotonic()
        elapsed = max(0.0, now - bucket.last_refill)
        bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_sec)
        bucket.last_refill = now
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True, 0.0
        needed = cost - bucket.tokens
        retry_after = needed / bucket.refill_per_sec if bucket.refill_per_sec > 0 else 60.0
        return False, retry_after
```

**Why `time.monotonic` not `time.time`:** wall-clock can jump backwards (NTP, DST); monotonic only moves forward — correct for measuring elapsed durations. **The refill math:** add `elapsed × refill_rate` tokens, capped at capacity; if ≥ cost, allow and decrement, else compute `retry_after`. **Dynamic capacity update** (in `_get_or_create`, not shown) handles rate changes made via the CLI while running, clamping current tokens to the new capacity.

Two known trade-offs unchanged: **in-memory** (restart = one free burst; move to Redis when you scale to multiple replicas), and the **`cost` parameter** (today every request costs 1; the interface accommodates cost-weighted limiting later — e.g. charging a streaming 4000-token call more than a 50-token one).

---

## 6. `app/auth.py` — three-header auth + OpenAPI security scheme (rewritten in v0.3.1)

This file changed the most in spirit for v0.3.1: it now accepts three header styles and registers an OpenAPI security scheme so the `/docs` "Authorize" button works.

```python
from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from .storage import AppRecord, lookup_by_plaintext

_api_key_scheme = APIKeyHeader(name="api-key", auto_error=False)


def _extract_key(request: Request) -> Optional[str]:
    """Pull the key from Authorization: Bearer or X-API-Key (api-key is handled
    by the Security scheme and passed into authenticate separately)."""
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        return x_api_key.strip()
    return None


def authenticate(
    request: Request,
    api_key: Optional[str] = Security(_api_key_scheme),
) -> AppRecord:
    plaintext = (api_key.strip() if api_key else None) or _extract_key(request)
    if not plaintext:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key (send 'api-key: <key>' or 'Authorization: Bearer <key>')",
            headers={"WWW-Authenticate": "Bearer"},
        )

    app = lookup_by_plaintext(plaintext)
    if app is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not app.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key is deactivated",
        )
    return app
```

### The three headers, and why each exists

- **`api-key: <key>`** — the Azure-OpenAI convention. Handled via the `APIKeyHeader` Security scheme.
- **`Authorization: Bearer <key>`** — the standard OpenAI / OAuth2 convention. What the stock `openai` SDK sends. Parsed in `_extract_key`.
- **`X-API-Key: <key>`** — plain convenience for curl / scripts. Also in `_extract_key`.

Supporting all three means *any* common client authenticates with no custom wiring: OpenAI SDK (Bearer), Azure OpenAI SDK (api-key), ad-hoc curl (X-API-Key). This is the direct answer to "can we use api-key instead of Bearer" — yes, and Bearer still works too.

### Why `APIKeyHeader` for `api-key` specifically

```python
_api_key_scheme = APIKeyHeader(name="api-key", auto_error=False)
...
api_key: Optional[str] = Security(_api_key_scheme),
```

Two payoffs from registering `api-key` as a FastAPI Security scheme rather than just reading it from `request.headers`:

1. **It appears in the OpenAPI spec**, so the `/docs` page grows an **"Authorize" button**. Paste a key there and you can test endpoints from the browser — the interactive-docs testing path.
2. **`auto_error=False`** means it returns `None` when the header is absent instead of raising, so we can *fall back* to the other two headers. We combine: `(api_key ...) or _extract_key(request)`.

The other two headers are read manually in `_extract_key` because registering multiple auto-competing security schemes complicates the OpenAPI/Swagger UX; one visible scheme (`api-key`) is enough for the docs button, and the manual fallbacks cover the rest transparently.

### The Authorization parser

```python
scheme, _, token = auth.partition(" ")
if scheme.lower() == "bearer" and token:
    return token.strip()
```

`str.partition(" ")` returns `(before, sep, after)`. For `"Bearer sk-..."` → `("Bearer", " ", "sk-...")`. No space → `("Bearer", "", "")` and `token` is empty, falls through cleanly. `scheme.lower()` handles case (RFC says scheme names are case-insensitive).

### Three distinct 401 reasons + `WWW-Authenticate`

Missing / invalid / deactivated all return 401 but with distinct messages — useful for client debugging and captured in the audit log. `WWW-Authenticate: Bearer` (RFC 7235) is included on the missing/invalid cases so Bearer-aware clients know to retry; omitted on the deactivated case (no point telling a client with a *correct* key to "try a Bearer token").

### The dependency-injection pattern (unchanged)

`authenticate` is a FastAPI dependency. Routes declare `app_record: AppRecord = Depends(authenticate)`; FastAPI runs it before the handler, returns 401 if it raises, or passes the `AppRecord` in. Authentication becomes a framework-enforced contract the route writer can't forget.

---

## 7. `app/proxy.py` — backend forwarders (extended in v0.3.1)

The file that calls the actual backends. ~211 lines now. It has the legacy forwarders (`call_ollama_generate`, `call_ollama_chat`) plus the v0.3.1 OpenAI functions.

### The shared async client (unchanged, still critical)

```python
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
```

One `httpx.AsyncClient` per process, shared across all requests. Critical for performance: httpx keeps a connection pool, so TCP connections to Ollama are reused instead of re-opened per request. Opened lazily, closed at shutdown (lifespan in `main.py`).

### The OpenAI passthrough — non-streaming

```python
async def call_ollama_openai(payload: dict) -> dict:
    """
    OpenAI-compatible chat completion (non-streaming).

    Forwards the request to Ollama's own OpenAI-compatible endpoint
    (/v1/chat/completions) and returns Ollama's response unchanged. Ollama
    produces the full OpenAI envelope (id, object, created, choices, usage),
    so the official `openai` SDK parses it directly. We do not reshape it.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/v1/chat/completions"
    resp = await get_client().post(url, json=payload)
    resp.raise_for_status()
    return resp.json()
```

This is the whole thing. Note what it is *not*: there's no field-name translation, no envelope construction, no response model. Ollama's `/v1/chat/completions` already speaks OpenAI, so we POST the (already OpenAI-shaped) payload and hand back the (already OpenAI-shaped) JSON. `raise_for_status()` turns a backend 4xx/5xx into an exception the route converts to a 502. The route reads `usage` from the returned dict for logging.

Contrast with the legacy `call_ollama_generate`, which *does* translate (our `max_tokens` → Ollama's `num_predict`, Ollama's `eval_count` → our `tokens_out`, etc.). The legacy path reshapes because it exposes our own schema; the OpenAI path passes through because it exposes OpenAI's. Two philosophies, deliberately: the legacy endpoints are our opinionated surface, the OpenAI endpoint is a faithful mirror.

### The OpenAI passthrough — streaming

```python
async def stream_ollama_openai(payload: dict):
    """
    OpenAI-compatible chat completion (streaming).

    Async generator that proxies the Server-Sent Events stream from Ollama's
    /v1/chat/completions straight through to the client, byte for byte.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/v1/chat/completions"
    async with get_client().stream("POST", url, json=payload) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_raw():
            yield chunk
```

An **async generator**, not a regular function. `get_client().stream(...)` opens the response without buffering the whole body; `aiter_raw()` yields the raw bytes as they arrive; we `yield` each chunk onward. The route wraps this generator in a `StreamingResponse`, so the client receives Server-Sent Events exactly as Ollama emits them — this is what makes `client.chat.completions.create(..., stream=True)` work in the SDK.

**Why `aiter_raw()` and not `aiter_lines()` or `aiter_text()`:** raw bytes are a byte-for-byte passthrough with zero re-encoding or re-framing risk. We're not parsing the SSE — we're relaying it. Let the SDK on the other end parse; our job is to move bytes and not corrupt them.

`resp.raise_for_status()` inside the `async with` runs once the response headers are in. A pure connection failure before any bytes propagates as an exception; the route catches it and (in the streaming branch) emits a final SSE error frame so a well-behaved client sees a clean end rather than a dangling socket.

### The models list

```python
async def list_ollama_models() -> dict:
    """
    OpenAI-compatible model list. Proxies Ollama's /v1/models, which already
    returns the OpenAI {"object":"list","data":[...]} shape.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/v1/models"
    resp = await get_client().get(url)
    resp.raise_for_status()
    return resp.json()
```

Same passthrough philosophy for `GET /v1/models`. Ollama's `/v1/models` is already OpenAI-shaped, so we relay it. Some OpenAI tooling calls this to discover or validate model names.

### `call_classifier` and `ping_backend` (unchanged)

`call_classifier` still forwards to `CLASSIFIER_BASE_URL/predict` (502s today, since nothing answers there yet). `ping_backend` (used by `/healthz`) does a short-timeout GET and returns `"ok"` or a reason string rather than raising, so the health endpoint describes state instead of failing on it.

---

## 8. `app/main.py` — the FastAPI assembly (extended in v0.3.1)

The longest file (~518 lines), mostly because it now has seven route handlers. Underneath, the same three concepts: middleware, exception handling, and routes. Plus the v0.3.1 reasoning-off default.

### Imports — the dependency graph at a glance

```python
from .auth import authenticate
from .config import settings
from .models import (
    ChatRequest, ChatResponse, ClassifyRequest, ClassifyResponse,
    GenerateRequest, GenerateResponse, HealthResponse, OpenAIChatRequest,
)
from .proxy import (
    call_classifier, call_ollama_chat, call_ollama_generate,
    call_ollama_openai, close_client, list_ollama_models,
    ping_backend, stream_ollama_openai,
)
```

`main.py` pulls pieces from everything; nothing imports `main.py`. Standard dependency-inversion. The `StreamingResponse` import (`from fastapi.responses import JSONResponse, StreamingResponse`) is the new one enabling streamed replies.

### Lifespan, request-ID middleware, exception handler (unchanged from Part-2-era)

- **Lifespan** logs config at startup and calls `close_client()` at shutdown (closes the httpx pool — no FD leaks on restart).
- **`attach_request_id` middleware** generates or honors `x-request-id`, stashes it and the start time on `request.state`, and echoes the ID in the response header — so a dev team can quote a request ID and you can grep it in the audit log.
- **The custom `HTTPException` handler** logs every failure and returns a consistent `{"error": ..., "request_id": ...}` shape (not FastAPI's default `{"detail": ...}`), so there's one error shape across the whole API.

### The rate-limit and log helpers (unchanged)

`_enforce_rate_limit(app_record)` calls `try_consume` and raises 429 with a `Retry-After` header (rounded up) if over. `_log_request(...)` is the single place log rows are built (keyword-only args so fields can't be swapped; resolves `app_record` to just the app name). Every route calls these.

### The standard endpoint — `POST /v1/chat/completions`

This is the v0.3.1 centerpiece. The route declaration:

```python
@app.post("/v1/chat/completions", tags=["chat (OpenAI-compatible)"])
@app.post("/chat/completions", include_in_schema=False)  # alias if base_url omits /v1
async def openai_chat_completions(
    body: OpenAIChatRequest,
    request: Request,
    app_record: AppRecord = Depends(authenticate),
):
```

Two decorators stack two paths onto one handler: `/v1/chat/completions` (the documented one) and `/chat/completions` (a hidden alias, in case a client sets `base_url` without the `/v1` suffix). `include_in_schema=False` keeps the alias out of `/docs` so it isn't shown twice.

The body:

```python
    request.state.app_record = app_record
    _enforce_rate_limit(app_record)

    # Forward only what the client actually sent, plus required fields.
    payload = body.model_dump(exclude_unset=True, exclude_none=True)
    payload.setdefault("model", body.model)
    payload.setdefault("messages", body.messages)
```

`model_dump(exclude_unset=True, exclude_none=True)` is the passthrough key: it serializes only the fields the client actually sent (plus the required two), *including* the `extra="allow"` fields from the model. So `tools`, `response_format`, `seed`, etc. all survive into `payload`, but our own optional defaults don't get injected. We forward what they sent, nothing more.

### The reasoning-off default — the v0.3.1 fix

```python
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
```

Why this exists: reasoning models default to "thinking", which consumed the whole token budget and returned an empty `content` (the answer sat in a separate `reasoning` field with `finish_reason: "length"`). The dev team would see blank responses and file a bug. Defaulting `reasoning_effort` to `"none"` makes `content` reliably populated.

Two subtle correctness points captured in the comment:
- We use **only `reasoning_effort`** (the OpenAI-standard, confirmed-working control), not Ollama's native `think` field — mixing an unrecognized top-level field into an OpenAI-compatible request risks a 400.
- We only inject it **if the caller didn't set it**, so anyone who *wants* reasoning (`reasoning_effort: "high"`) keeps their choice. This preserves passthrough purity for the opt-in case while fixing the default case.

### The streaming branch

```python
    if body.stream:
        _log_request(
            request_id=request.state.request_id, app_record=app_record,
            method="POST", path="/v1/chat/completions", status_code=200,
            latency_ms=0, backend="ollama", model=body.model,
            error="streamed (token counts not captured)",
        )

        async def event_stream():
            try:
                async for chunk in stream_ollama_openai(payload):
                    yield chunk
            except httpx.HTTPError:
                yield b'data: {"error":"upstream model backend unreachable"}\n\n'

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"x-request-id": request.state.request_id},
        )
```

If `stream=true`, we return a `StreamingResponse` wrapping the proxy generator. We log once up front (token counts aren't known until the stream ends, so they're omitted and flagged). The inner `try/except` converts a backend failure into a final SSE error frame so the client sees a clean end rather than a truncated stream. `media_type="text/event-stream"` is the SSE content type the SDK expects.

**Honest note recorded in the doc:** streaming audit rows don't carry token counts. Capturing them would mean parsing the final SSE `usage` chunk mid-stream — deferred as not worth the complexity for the POC. Non-streaming rows have full token accounting.

### The non-streaming branch

```python
    started = time.monotonic()
    try:
        data = await call_ollama_openai(payload)
    except httpx.HTTPStatusError as exc:
        # ... log 502 with backend status + body snippet ...
        raise HTTPException(status_code=502, detail="upstream model backend error")
    except httpx.HTTPError as exc:
        # ... log 502 with transport error class ...
        raise HTTPException(status_code=502, detail="upstream model backend unreachable")

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = data.get("usage") or {}
    _log_request(
        request_id=request.state.request_id, app_record=app_record,
        method="POST", path="/v1/chat/completions", status_code=200,
        latency_ms=latency_ms, backend="ollama",
        model=data.get("model", body.model),
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
    )
    return JSONResponse(content=data, headers={"x-request-id": request.state.request_id})
```

Same structure as the legacy handlers — auth → rate limit → call backend (two except classes for status vs transport errors) → log outcome → return. The differences: we read `usage.prompt_tokens`/`completion_tokens` from Ollama's OpenAI response for the log, and we return a raw `JSONResponse` (Ollama's body unchanged) plus the request-id header, rather than a typed `response_model`.

### `GET /v1/models`

```python
@app.get("/v1/models", tags=["chat (OpenAI-compatible)"])
async def openai_models(request: Request, app_record: AppRecord = Depends(authenticate)):
    ...
    data = await list_ollama_models()
    ...
    return JSONResponse(content=data, headers={"x-request-id": request.state.request_id})
```

Authenticated, logs like everything else, passthrough of Ollama's OpenAI-shaped model list.

### The legacy routes — deprecated but alive

```python
@app.post("/v1/generate", response_model=GenerateResponse,
          tags=["legacy (deprecated)"], deprecated=True)
...
@app.post("/v1/chat", response_model=ChatResponse,
          tags=["legacy (deprecated)"], deprecated=True)
```

`deprecated=True` renders them struck-through in `/docs` under a "legacy" section. They still function (backward compatibility for anything integrated before v0.3.x) but signal that new work should use `/v1/chat/completions`. They'll be removed once confirmed unused.

### On tags (since you asked)

`tags=[...]` groups endpoints into sections on the `/docs` (Swagger UI) and `/redoc` pages. They do **not** affect routing or how clients call the API — the OpenAI SDK and curl ignore them entirely. They're a documentation/testing convenience. v0.3.1 uses four: `chat (OpenAI-compatible)`, `meta` (healthz, me), `classifier`, and `legacy (deprecated)`. Combined with the `api-key` security scheme, the `/docs` page is now genuinely usable for browser testing: open it, click Authorize, paste a key, try the chat endpoint.

### Why the route handlers are still verbose-but-parallel

All the handlers share structure (auth → rate limit → backend → log → return). You could factor this into a decorator. **Still don't** — each route differs (which backend, which response shape, streaming vs not, which error text), and premature abstraction would either over-generalize (an unreadable 6-parameter decorator) or under-consolidate. Verbose-but-parallel stays easy to read, easy to modify per-route, easy to grep. Extract only when you have many routes with *truly* identical bodies.

---

## 9. `scripts/gatekeeper-keys` — the admin CLI (unchanged in v0.3.1)

Self-contained Python, ~170 lines, no dependency beyond PyYAML. The OpenAI work didn't touch it — keys are keys regardless of which endpoint consumes them.

### argparse subcommand dispatch

```python
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gatekeeper-keys", ...)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", ...).set_defaults(func=cmd_list)
    p_issue = sub.add_parser("issue", ...)
    p_issue.add_argument("app_name")
    p_issue.add_argument("--rate", type=int, default=DEFAULT_RATE)
    p_issue.add_argument("--notes", default="")
    p_issue.set_defaults(func=cmd_issue)
    # ... rotate, disable, enable, remove ...
    return p
```

`set_defaults(func=...)` is the clean subcommand idiom: each subparser records which function handles it; `main()` reads `args.func` and calls it. No if/elif chain.

### The atomic write pattern

```python
def _save(data: dict) -> None:
    p = Path(KEYS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
```

Write to `.tmp`, then `os.replace()` (atomic on the same filesystem), then restore mode 600. **Why atomic:** the gateway is *reading* this file concurrently; an in-place write could expose a half-written file and crash the reader or drop half the apps. `os.replace` swaps in the new file as one operation — the gateway sees old or new, never partial. The mtime bump from the replace is exactly what triggers the gateway's hot reload.

### Key generation and the "shown once" rule

```python
def _generate_key() -> tuple[str, str]:
    raw = secrets.token_urlsafe(36)
    plaintext = f"{KEY_PREFIX}{raw}"     # KEY_PREFIX = "sk-gatekeeper-"
    h = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return plaintext, h
```

`secrets.token_urlsafe(36)` = 36 bytes from the OS CSPRNG ≈ 288 bits of entropy. The function returns both plaintext and hash; `issue`/`rotate` print the plaintext exactly once and store only the hash. Once the process exits, the plaintext is unrecoverable — which is why the CLI prints "store this key now."

---

## 10. The full data flow — one OpenAI request, traced

Tracing `client.chat.completions.create(model="qwen3.5:9b", messages=[...])` from the OpenAI SDK, non-streaming:

```
1. SDK POSTs to  {base_url}/chat/completions  →  /v1/chat/completions
   with header  Authorization: Bearer sk-gatekeeper-...  (or api-key: ...)
                                                ↓
2. ALB → gateway:8080 → uvicorn → FastAPI
                                                ↓
3. attach_request_id middleware: set request.state.request_id + started_at
                                                ↓
4. openai_chat_completions(body, request, app_record = Depends(authenticate))
   FastAPI validates body against OpenAIChatRequest (model + messages required;
   extra fields allowed), then runs authenticate first
                                                ↓
5. auth.authenticate:
   - api-key header via Security scheme, else _extract_key (Bearer / X-API-Key)
   - lookup_by_plaintext(plaintext)
                                                ↓
6. storage.lookup_by_plaintext:
   - reload_if_changed() (stat keys.yaml; reload if mtime advanced)
   - _hash_key(plaintext) → SHA-256
   - _keys_index.by_hash.get(hash) → AppRecord | None
                                                ↓
7. authenticate: None → 401; inactive → 401; else return AppRecord
                                                ↓
8. handler: request.state.app_record = app_record;  _enforce_rate_limit(...)
                                                ↓
9. rate_limit.try_consume: refill bucket, decrement or 429
                                                ↓
10. build payload = model_dump(exclude_unset, exclude_none) + model + messages
    inject reasoning_effort:"none" unless caller set it
                                                ↓
11. body.stream is false → call_ollama_openai(payload)
                                                ↓
12. proxy.call_ollama_openai: POST payload to
    http://127.0.0.1:11434/v1/chat/completions  (Ollama's OpenAI backend)
    via the shared httpx client (pooled connection)
                                                ↓
13. Ollama runs qwen3.5:9b on the A10G, returns an OpenAI-shaped JSON
    (id, object, created, choices[0].message.content, usage)
                                                ↓
14. proxy returns that dict unchanged
                                                ↓
15. handler reads usage.prompt_tokens / completion_tokens for the log
                                                ↓
16. _log_request → storage.append_request_log → one JSONL line
                                                ↓
17. handler returns JSONResponse(content=<Ollama's body>, x-request-id header)
                                                ↓
18. middleware echoes x-request-id → uvicorn → ALB → SDK
                                                ↓
19. SDK parses choices[0].message.content — the dev sees a normal OpenAI object
```

The streaming variant diverges at step 11 (`body.stream` true): the handler returns a `StreamingResponse` wrapping `stream_ollama_openai(payload)`, and steps 13–17 become a byte-for-byte SSE relay instead of a single JSON body.

---

## 11. What you'd change for common extensions

### Swap Ollama for vLLM or TensorRT-LLM

Because vLLM and TensorRT-LLM *also* expose OpenAI-compatible `/v1/chat/completions`, this is now almost trivial for the standard endpoint: point `OLLAMA_BASE_URL` (or add a `VLLM_BASE_URL`) at the new server. `call_ollama_openai`, `stream_ollama_openai`, and `list_ollama_models` are already OpenAI passthroughs — they work against any OpenAI-compatible backend unchanged. The legacy `/v1/generate` path (which uses Ollama's native `/api/generate`) would need its own adjustment, but the standard path is the easy case. This is a direct payoff of the passthrough design.

### Swap YAML keys for AWS Secrets Manager

`storage.py` — `_load_keys_file` becomes a `boto3` call; `lookup_by_plaintext` keeps its signature. Add `boto3` to requirements, an IAM role to the EC2. Everything above the lookup is untouched.

### Add token-budget rate limiting

The audit log already records `tokens_in`/`tokens_out`. Add a per-app daily budget check alongside `try_consume` (read the day's total, compare, 429 if exceeded). The `cost` parameter in `try_consume` also lets you weight requests by size.

### Add Prometheus metrics

`pip install prometheus-client`, create `app/metrics.py` with counters/histograms, increment them from `_log_request`, expose a `/metrics` route, allow the scraper in the security group. ~100 lines.

### Capture token counts on streamed requests

Parse the final SSE chunk in the streaming branch: OpenAI-compatible servers emit a terminal chunk with `usage` when `stream_options.include_usage` is set. Accumulate it in `event_stream()` and log after the generator finishes. Deferred today; this is where you'd add it.

---

## 12. Closing — evolution and testing

### Test plan you'd want, eventually

Per module: `test_storage.py` (load known hashes; hot-reload via mtime), `test_rate_limit.py` (allowed:rejected ratio; dynamic capacity), `test_auth.py` (all three headers; four 401 reasons), `test_proxy.py` (mock httpx; assert the payload sent to Ollama and passthrough of its response), `test_main.py` (FastAPI `TestClient` against the whole app with a mocked proxy, including the reasoning_effort injection and the streaming branch). `pytest` + `pytest-asyncio` + `pytest-httpx`, ~400 lines for ~1100 lines of code.

The v0.3.1 smoke test that was actually run before shipping covered: all three auth headers, 401 on missing/bad key, 422 on empty messages and missing model, extra-fields and multimodal passthrough (no 422), the `/chat/completions` alias, `/v1/models`, the streaming path (clean SSE, no crash), and rate limiting — plus a live check on the box confirming `reasoning_effort:"none"` populates `content`.

### When to break files apart

`main.py` over ~600 lines → split routes into a `routes/` subpackage (one file per resource: `routes/chat.py`, `routes/legacy.py`, `routes/meta.py`). `storage.py` mixing more concerns → `storage/keys.py` + `storage/audit.py`. Not yet — everything's still small enough to hold in one place.

### What I'd do differently next time

- **Tests from day one**, even a single smoke test.
- **Terraform from day one** for the AWS resources.
- **`/metrics` from day one** — cheap early, annoying to bolt on later.
- **The OpenAI passthrough from day one** — the early build hand-rolled a `/v1/generate` schema and later had to add the OpenAI surface anyway. Starting OpenAI-compatible would have saved the legacy endpoints existing at all.

These are v0.4 items, not v0.3.1 regrets. Every project has a "ship now, harden later" budget; this one spent it on the right things — a working, OpenAI-compatible, authenticated, audited gateway that a dev team can integrate against with a stock SDK.

---

## Series wrap-up

Three documents describe everything built:

- **Part 1 — Infrastructure & GPU VM:** AWS networking, GPU stack, OS setup, disaster recovery, cost model.
- **Part 2 — The Gateway: API, Features, Cloud Independence:** endpoints, auth model, rate limiting, audit, CLI, portability.
- **Part 3 — The Code, File by File (this doc, v0.3.1):** every module, the OpenAI-compatible surface, the reasoning-off default, every design trade-off.

Most projects ship code and skip the reasoning. The reasoning is what makes the code maintainable — and what lets you (or the next engineer) rebuild this on a different stack without re-deriving every decision.
