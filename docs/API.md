# Gatekeeper — API Reference v1

Base URL is whatever the gateway is exposed at (typically `https://<alb-hostname>`).

## Stateless model — important

Both `/v1/generate` and `/v1/chat` are stateless. The gateway and the model retain no memory between calls. Your application is responsible for storing conversation state and re-sending it on each request when you need continuity. Use `/v1/chat` (which takes a message array) for multi-turn flows; use `/v1/generate` for one-shot prompts.

## Authentication

All `/v1/*` endpoints require one of:

```
Authorization: Bearer sk-gatekeeper-<random>
X-API-Key: sk-gatekeeper-<random>
```

## Endpoints

### `GET /healthz`

No auth. Backend reachability snapshot.

```json
{
  "status": "ok",
  "backends": {"ollama": "ok", "classifier": "ok"}
}
```

### `GET /v1/me`

Returns the calling app's record.

```json
{
  "app": "prod-dashboard",
  "active": true,
  "rate_limit_per_min": 100,
  "created_at": "2026-06-29T07:03:24+00:00"
}
```

### `POST /v1/generate`

LLM text generation via Ollama backend. One-shot prompt; no memory of previous calls.

| Field         | Type   | Required | Default | Notes                              |
|---------------|--------|----------|---------|------------------------------------|
| model         | string | yes      |         | e.g. `qwen3.5:9b`                  |
| prompt        | string | yes      |         |                                    |
| system        | string | no       |         | system prompt                      |
| max_tokens    | int    | no       | 512     | 1–8192                             |
| temperature   | float  | no       | 0.7     | 0.0–2.0                            |
| stream        | bool   | no       | false   | not yet implemented                |
| think         | bool   | no       | false   | enable model thinking (slower)     |

Response:

```json
{
  "model": "qwen3.5:9b",
  "response": "...",
  "tokens_in": 47,
  "tokens_out": 38,
  "latency_ms": 1240
}
```

### `POST /v1/chat`

Multi-turn conversation. Send the full message history every call — the gateway and model are stateless.

| Field         | Type            | Required | Default | Notes                              |
|---------------|-----------------|----------|---------|------------------------------------|
| model         | string          | yes      |         | e.g. `qwen3.5:9b`                  |
| messages      | array of Message| yes      |         | conversation history, oldest first, ≥1 entry |
| max_tokens    | int             | no       | 512     | 1–8192                             |
| temperature   | float           | no       | 0.7     | 0.0–2.0                            |
| think         | bool            | no       | false   | enable model thinking              |

`Message`:

| Field    | Type    | Notes                                  |
|----------|---------|----------------------------------------|
| role     | string  | one of: `system`, `user`, `assistant`  |
| content  | string  | message text                           |

Request:

```json
{
  "model": "qwen3.5:9b",
  "messages": [
    {"role": "system", "content": "You are a solar plant O&M assistant."},
    {"role": "user", "content": "What is soiling?"},
    {"role": "assistant", "content": "Soiling is the accumulation of dust..."},
    {"role": "user", "content": "How do I detect it from MPPT data?"}
  ],
  "max_tokens": 400
}
```

Response:

```json
{
  "model": "qwen3.5:9b",
  "message": {"role": "assistant", "content": "From MPPT data, soiling typically shows up as..."},
  "tokens_in": 124,
  "tokens_out": 89,
  "latency_ms": 2150
}
```

The client appends this `message` to its own history and sends the full list on the next turn.

### `POST /v1/classify`  *(coming soon)*

MPPT fault classifier.

| Field      | Type             | Required | Notes                                            |
|------------|------------------|----------|--------------------------------------------------|
| plant      | string           | yes      | plant code, e.g. `bidar`                         |
| string_id  | string           | yes      | e.g. `S012`                                      |
| window     | array of arrays  | yes      | `[time_steps][channels]` of MPPT readings        |

Response:

```json
{
  "plant": "bidar",
  "string_id": "S012",
  "fault": "soiling",
  "confidence": 0.87,
  "probabilities": {"soiling": 0.87, "shading": 0.04, "degradation": 0.06, "hardware": 0.03},
  "latency_ms": 32
}
```

## Errors

All errors return JSON with `error` and `request_id` fields:

```json
{"error": "rate limit exceeded for prod-dashboard; retry in 1.2s", "request_id": "..."}
```

| Status | Meaning                                         |
|--------|-------------------------------------------------|
| 401    | missing / invalid / deactivated key             |
| 403    | model not permitted for this key (see below)    |
| 422    | malformed request body                          |
| 429    | rate limit exceeded (see `Retry-After` header)  |
| 502    | upstream model backend error                    |
| 500    | gateway internal error                          |

## Per-key model restrictions

A key may be restricted to specific models (set with `gatekeeper-keys issue <app> --models ...`). If a restricted key requests any other model, the gateway returns **403** before contacting the backend, e.g.:

```json
{"error": "model 'gemma4:12b' is not permitted for this API key; allowed: gemma4:e4b", "request_id": "..."}
```

`GET /v1/models` returns only the models a key is allowed to use. A key with no restriction (the default) may call any model and sees the full list.

## Headers

| Header           | Direction | Notes                                                       |
|------------------|-----------|-------------------------------------------------------------|
| `x-request-id`   | both      | sent back on every response; include in bug reports        |
| `Retry-After`    | response  | seconds until rate limit window resets (on 429 only)       |

## Versioning policy

`/v1/*` is stable. Breaking changes go to `/v2/*` with at least 30 days of overlap.
