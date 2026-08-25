# Changelog

## v0.4.0 — 2026-08-21

### Added
- **Per-key model restrictions.** A key can be limited to specific model(s); requests for any other model are rejected with `403`. Set at issue time with `gatekeeper-keys issue <app> --models gemma4:e4b` (multiple allowed: `--models a b`), changed later with `gatekeeper-keys rotate <app> --models ...` (pass `--models` with no values to clear the restriction). A key with no `allowed_models` (the default) may call any model, so existing keys are unaffected. Enforced on `/v1/chat/completions` (streaming and non-streaming), `/v1/generate`, and `/v1/chat`, before the request reaches the backend; `GET /v1/models` also filters its list to the key's allowed models. Matching is exact on the model name. The rate-limit check runs before the model check, so requests for a disallowed model still count against the key's rate budget.

## v0.3.1 — 2026-06-30

### Fixed
- **Empty `content` from reasoning models.** qwen3.5 and gemma4 default to "thinking", which consumed the whole token budget and left the user-visible `content` empty (the answer stayed in a separate `reasoning` field, with `finish_reason: "length"`). The OpenAI endpoint now injects `reasoning_effort: "none"` by default so `content` is reliably populated. Callers who want reasoning opt in by sending `reasoning_effort` ("low"/"medium"/"high") themselves; their choice is preserved.

## v0.3.0 — 2026-06-30

### Added
- **`POST /v1/chat/completions` — OpenAI-compatible chat endpoint, now the standard.** The official `openai` SDK works against the gateway with only a `base_url` change. Implemented as a passthrough to Ollama's own OpenAI-compatible backend, so the response is a genuine OpenAI envelope (`id`, `object`, `created`, `choices`, `usage`) and multimodal content arrays plus tool/`response_format`/`seed`/etc. fields pass straight through (no 422 on unknown OpenAI fields).
- **Streaming** on `/v1/chat/completions` via `stream: true` (SSE passthrough).
- **`GET /v1/models`** — OpenAI-compatible model list.
- **`api-key` header** for auth (Azure-OpenAI convention), alongside `Authorization: Bearer` (OpenAI) and `X-API-Key` (curl). Registered as an OpenAPI security scheme so the `/docs` "Authorize" button works.
- **`/chat/completions` alias** (without `/v1`) for clients that set `base_url` without the version suffix.

### Changed
- OpenAPI `/docs` tags reorganized: `chat (OpenAI-compatible)`, `meta`, `classifier`, `legacy (deprecated)`.

### Deprecated
- `POST /v1/generate` and `POST /v1/chat` marked deprecated (still functional). New integrations use `/v1/chat/completions`.

### Backward compatibility
- All v0.1.0 and v0.2.0 calls continue to work.

## v0.2.0 — 2026-06-30

### Added
- `POST /v1/chat` — multi-turn chat endpoint taking a messages array (system / user / assistant). Caller stores history; gateway is stateless.
- `think` parameter on `/v1/generate` (defaults to `false`) — opt in to model thinking when desired.

### Fixed
- `/v1/generate` was returning empty `response` strings with thinking-model defaults. Now passes `think: false` to Ollama by default so the token budget yields visible output, not hidden reasoning.

### Backward compatibility
- All v0.1.0 calls continue to work unchanged.

## v0.1.0 — 2026-06-29

Initial release:
- `GET /healthz`, `GET /v1/me`
- `POST /v1/generate` (Ollama backend)
- `POST /v1/classify` (placeholder; classifier not yet built)
- Per-app API keys (SHA-256-only storage, hot reload, admin CLI)
- Token-bucket rate limiting, per-app
- JSONL audit log
- Docker + systemd deployment
