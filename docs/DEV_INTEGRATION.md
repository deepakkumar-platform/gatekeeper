# Gatekeeper — Integration Guide for Apps

This doc is for application developers calling Gatekeeper models. If you're the operator running the gateway, see `OPERATOR_RUNBOOK.md` instead.

## What you get

A single HTTPS endpoint that your app calls with its API key. Behind it, Gatekeeper routes to the right model backend (LLM today, the MPPT fault classifier soon). You don't manage GPUs, model versions, or model load — that's our side.

## What you need from us

1. The base URL of the gateway (e.g. `https://gatekeeper.example.internal`).
2. Your app's API key. We issue one per application. The key looks like `sk-gatekeeper-` followed by a random string.
3. Your rate limit (requests per minute). We set this when we issue the key. If you need more, ask.

Treat the key like a password. Store it in your secret manager, never commit it. If you suspect it's leaked, ping us and we'll rotate it.

## Sending the key

Pass it in one of these headers. Bearer is preferred.

```
Authorization: Bearer sk-gatekeeper-xxxxxxxxxxxxxxxxxxxx
```

or

```
X-API-Key: sk-gatekeeper-xxxxxxxxxxxxxxxxxxxx
```

Don't put the key in the URL. Don't log full request URLs that contain the key.

## Endpoints

### `GET /healthz`

Public. No auth. Returns whether each backend is reachable. Use this for liveness probes on your end if you want, but don't poll it from your hot path.

```bash
curl https://gatekeeper.example.internal/healthz
```

### `GET /v1/me`

Returns information about the calling app. Useful for debugging "is my key working".

```bash
curl -H "Authorization: Bearer $GATEKEEPER_KEY" \
     https://gatekeeper.example.internal/v1/me
```

Response:
```json
{
  "app": "prod-dashboard",
  "active": true,
  "rate_limit_per_min": 100,
  "created_at": "2026-06-29T07:03:24+00:00"
}
```

### `POST /v1/generate`

Text generation via the LLM backend. **One-shot** — no memory of previous calls. Use this for single-prompt tasks (work orders, summaries, classifications). For multi-turn conversations, use `/v1/chat` instead.

Request:
```json
{
  "model": "qwen3.5:9b",
  "prompt": "Write a one-line work order: string 12 at Bidar shows soiling at 0.87 confidence.",
  "system": "You are a solar plant O&M assistant. Respond in Hinglish.",
  "max_tokens": 200,
  "temperature": 0.4,
  "stream": false
}
```

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

Field notes:
- `model` — for the POC, use `qwen3.5:9b`. We'll publish the model catalogue in `API.md` when we add more.
- `system` — optional. If your app wants a consistent persona/instructions, put them here once per request.
- `max_tokens` — hard cap on the response. Be conservative; lower = faster + cheaper.
- `temperature` — 0.0 deterministic, 1.0 creative. For work orders, 0.3–0.5 is sensible.
- `stream` — not enabled yet; coming soon.
- `think` — defaults to `false`. `qwen3.5` is a reasoning model; with thinking on, much of `max_tokens` is spent on hidden reasoning. Leave off unless you specifically want it.

### `POST /v1/chat`

Multi-turn chat via the LLM backend. **The gateway and model are stateless** — your app stores conversation history and sends the full list every call.

Request:
```json
{
  "model": "qwen3.5:9b",
  "messages": [
    {"role": "system", "content": "You are a solar plant O&M assistant. Respond in Hinglish."},
    {"role": "user", "content": "String 12 at Bidar shows soiling 0.87. Write a work order."},
    {"role": "assistant", "content": "Bhai, String 12 pe soiling detect hui hai..."},
    {"role": "user", "content": "Crew kaha bheju, aur kaunsa equipment chahiye?"}
  ],
  "max_tokens": 400,
  "temperature": 0.3
}
```

Response:
```json
{
  "model": "qwen3.5:9b",
  "message": {
    "role": "assistant",
    "content": "Cleaning crew Bidar plant bhejo, aur DI water plus microfiber rollers chahiye..."
  },
  "tokens_in": 124,
  "tokens_out": 89,
  "latency_ms": 2150
}
```

How to use this:
1. Keep a `messages` array per user/conversation in your app's database or cache.
2. On every turn, append the user's new message, send the full array.
3. Get back `message` (role `assistant`), append it to your history.
4. Repeat.

Watch the cumulative `tokens_in`. Long conversations cost more — somewhere past ~8000 tokens the model's recall of early messages degrades. For long chats, periodically summarize older turns into a single system message and drop the originals.

### `POST /v1/classify`

Calls the MPPT fault classifier. **Not live yet** — the model.pt is still being built. The schema below is what we'll ship.

Request:
```json
{
  "plant": "bidar",
  "string_id": "S012",
  "window": [[480.1, 6.2, 41.0, 920.0], [480.4, 6.1, 41.2, 925.0]]
}
```

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

## Status codes

| Code | Meaning                                                          | What to do                          |
|------|------------------------------------------------------------------|-------------------------------------|
| 200  | OK                                                               | use response                        |
| 401  | missing / invalid / deactivated key                              | check your key; contact us          |
| 422  | malformed request body                                           | fix the JSON                        |
| 429  | rate limit hit; `Retry-After` header has wait time in seconds    | back off, then retry                |
| 502  | model backend is unreachable / errored                           | retry with backoff; alert us if it persists |
| 500  | unexpected error in the gateway itself                           | report to us with the `x-request-id` |

Every response carries `x-request-id`. Include it in any bug report.

## Examples

### curl

```bash
curl -s -X POST https://gatekeeper.example.internal/v1/generate \
  -H "Authorization: Bearer $GATEKEEPER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"qwen3.5:9b",
    "prompt":"Summarise: string 12 soiling 0.87",
    "max_tokens":120
  }'
```

### Python

```python
import os, httpx

key = os.environ["GATEKEEPER_KEY"]
client = httpx.Client(
    base_url="https://gatekeeper.example.internal",
    headers={"Authorization": f"Bearer {key}"},
    timeout=120,
)

resp = client.post("/v1/generate", json={
    "model": "qwen3.5:9b",
    "prompt": "Summarise: string 12 soiling 0.87",
    "max_tokens": 120,
})
resp.raise_for_status()
print(resp.json()["response"])
```

### Node

```js
const resp = await fetch(`${BASE}/v1/generate`, {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${process.env.GATEKEEPER_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "qwen3.5:9b",
    prompt: "Summarise: string 12 soiling 0.87",
    max_tokens: 120,
  }),
});
if (!resp.ok) throw new Error(`gateway ${resp.status}`);
const data = await resp.json();
console.log(data.response);
```

## Operational expectations

- **Latency.** First-token latency from cold cache: ~1.5–3s for short prompts on `qwen3.5:9b`. Subsequent calls are faster. The classifier endpoint is sub-100ms once it's live.
- **Concurrency.** A single GPU instance serializes generation. If multiple apps hit hard at once, you'll see latency rise rather than 429s — model load isn't free.
- **Cold model.** The first request after deployment may take an extra few seconds while the model warms.
- **Versioning.** We treat `/v1/*` as stable. Breaking changes will land at `/v2/*` with overlap.

## Asking for changes

- Need a higher rate limit → ping the operator.
- Key compromised or unsure → ping the operator immediately; we'll rotate.
- Need a new endpoint / model → file a ticket; we'll plan it.
