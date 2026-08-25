# Gatekeeper — Integration Quick Start

A short guide for the dev team calling Gatekeeper from Azure. The endpoint is hosted on AWS; you reach it over the internet using your API key.

**The gateway is OpenAI-compatible.** If you already use the OpenAI SDK, integration is a base-URL change.

---

## What you receive from us

1. **Base URL** of the gateway (use the form ending in `/v1`)
2. **An API key** for your application — looks like `sk-gatekeeper-<random>`
3. **Your assigned rate limit** in requests per minute

The key replaces a password: never commit it, never share in plain Slack/email, never log it client-side.

---

## Current endpoint

```
Base URL:  http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/v1
```

> **POC note:** HTTP today, not HTTPS. Fine for testing and non-sensitive traffic only. We'll send the HTTPS URL on a proper domain within the week. **Don't put real user data through the HTTP endpoint.**

---

## Quickest start — OpenAI SDK (recommended)

### Python

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/v1",
    api_key="sk-gatekeeper-...",   # your key
)

resp = client.chat.completions.create(
    model="qwen3.5:9b",
    messages=[
        {"role": "system", "content": "You are a solar O&M assistant."},
        {"role": "user", "content": "In one sentence, what is panel soiling?"},
    ],
    max_tokens=200,
)
print(resp.choices[0].message.content)
```

That's it. Standard OpenAI SDK, just our `base_url` and `api_key`.

### Node.js

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/v1",
  apiKey: process.env.GATEKEEPER_KEY,
});

const resp = await client.chat.completions.create({
  model: "qwen3.5:9b",
  messages: [{ role: "user", content: "What is panel soiling?" }],
  max_tokens: 200,
});
console.log(resp.choices[0].message.content);
```

### C# / .NET (Azure.AI.OpenAI or OpenAI SDK)

```csharp
using OpenAI;
using OpenAI.Chat;
using System.ClientModel;

var client = new ChatClient(
    model: "qwen3.5:9b",
    credential: new ApiKeyCredential(Environment.GetEnvironmentVariable("GATEKEEPER_KEY")!),
    options: new OpenAIClientOptions {
        Endpoint = new Uri("http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/v1")
    });

ChatCompletion completion = await client.CompleteChatAsync(
    new UserChatMessage("What is panel soiling?"));
Console.WriteLine(completion.Content[0].Text);
```

### Streaming (Python)

```python
stream = client.chat.completions.create(
    model="qwen3.5:9b",
    messages=[{"role": "user", "content": "Explain MPPT in 3 sentences."}],
    max_tokens=300,
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

---

## Authentication

The gateway accepts your key in any of these headers — pick whichever your client uses:

```
api-key: sk-gatekeeper-...                  (Azure-style; what the Azure OpenAI SDK sends)
Authorization: Bearer sk-gatekeeper-...     (standard; what the stock OpenAI SDK sends)
X-API-Key: sk-gatekeeper-...                (convenience for curl/scripts)
```

The OpenAI SDK handles this for you when you pass `api_key=`. You don't need to set headers manually.

Don't put the key in the URL.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/chat/completions` | **Standard** — OpenAI chat completions (streaming + non-streaming) |
| GET | `/v1/models` | List available models (OpenAI format) |
| GET | `/v1/me` | Returns your app's record — use to verify your key |
| GET | `/healthz` | Liveness (no auth) |

Request/response on `/v1/chat/completions` is exactly the OpenAI Chat Completions shape — `messages` in, `choices[0].message.content` + `usage` out. Anything the OpenAI SDK sends (tools, `response_format`, multimodal content, `seed`, etc.) is accepted and forwarded.

**Model:** use `qwen3.5:9b` for now. Call `GET /v1/models` to see what's currently available.

---

## Raw curl (no SDK)

```bash
export GATEKEEPER_KEY="sk-gatekeeper-..."
export BASE="http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/v1"

# verify your key
curl -H "api-key: $GATEKEEPER_KEY" "$BASE/me"

# chat completion
curl -X POST "$BASE/chat/completions" \
  -H "api-key: $GATEKEEPER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5:9b",
    "messages": [{"role": "user", "content": "What is panel soiling?"}],
    "max_tokens": 200
  }'
```

Response (OpenAI shape):
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1782761540,
  "model": "qwen3.5:9b",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": 21, "completion_tokens": 60, "total_tokens": 81}
}
```

---

## Multi-turn conversations

The gateway is **stateless** — it keeps no memory between calls. For a conversation, your app stores the message history and sends the whole list each turn (this is exactly how the OpenAI API works):

```python
history = [{"role": "system", "content": "You are a solar O&M assistant."}]

def ask(user_text):
    history.append({"role": "user", "content": user_text})
    resp = client.chat.completions.create(model="qwen3.5:9b", messages=history, max_tokens=300)
    answer = resp.choices[0].message.content
    history.append({"role": "assistant", "content": answer})
    return answer
```

Watch cumulative tokens on long chats — past ~8000 tokens the model's recall of early turns degrades. Summarize or drop old turns when needed.

---

## Errors and retry

Errors are JSON with `request_id`:

```json
{"error": "rate limit exceeded for your-app; retry in 1.2s", "request_id": "abc123..."}
```

| Status | Meaning | What to do |
|---|---|---|
| 200 | OK | Use the response |
| 401 | Missing / invalid / deactivated key | Check the key; contact us |
| 422 | Request body invalid | Fix the JSON |
| 429 | Rate limit hit | Wait `Retry-After` seconds, retry |
| 502 | Model backend errored / unreachable | Retry with backoff; alert us if persists |
| 500 | Gateway internal error | Report with `request_id` |

The OpenAI SDK retries some of these automatically. If you call the API directly, back off exponentially on 429 and 502.

Every response carries `x-request-id`. **Include it in any bug report** — we find your call in our audit log by that ID.

---

## Azure-specific notes

**Store the key in Azure Key Vault**, referenced from your hosting:
- App Service / Functions: app setting `@Microsoft.KeyVault(VaultName=your-vault;SecretName=gatekeeper-api-key)`
- Container Apps: Container Apps secret → env var
- AKS: Kubernetes Secret → env var

**Egress:** outbound HTTP/HTTPS from Azure to AWS works by default. If your subscription restricts egress, allow outbound to the gateway hostname (allow by hostname; the ALB's IPs rotate).

**Latency:** Azure South India ↔ AWS Mumbai is ~5–15 ms RTT, negligible vs the 1–3 s model inference. No ExpressRoute/PrivateLink needed for the POC.

**Timeouts:** set client timeout to **at least 120 seconds** — LLM responses can take several seconds. The OpenAI SDK defaults are usually fine, but if you wrap calls, don't use a 30 s timeout.

---

## Interactive docs

Visit `/docs` on the base host (e.g. `http://gatekeeper-alb-.../docs`) for an interactive Swagger UI. Click **Authorize**, paste your key into the `api-key` field, and you can try endpoints from the browser.

---

## What's coming

- **HTTPS on a proper domain** — within the week (same paths, `https://`).
- **Classifier endpoint** (`/v1/classify`) for MPPT fault detection — when the model is trained.
- More models — call `GET /v1/models` for the current list.

---

## Getting help

Reach us with: (1) your `app` name, (2) the `request_id` from the response, (3) the request body if it's a 422, (4) the full error. We can find any request in our audit log by `request_id` in seconds.

---

**One-line summary:** point the OpenAI SDK at `base_url=".../v1"` with your `api_key`, call `chat.completions.create(model="qwen3.5:9b", messages=[...])`, expect 1–3 s responses, retry 429/502 with backoff.
