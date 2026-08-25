# Part 2 — The Gateway: API, Features, and Cloud Independence

This is the second of three documents. Part 1 covered everything below the application — AWS networking, the GPU instance, OS-level setup. This document covers the application itself: what the gateway does, how each feature works under the hood, what design decisions were made and why, and crucially why none of those decisions tie you to AWS.

**What this document covers:**
- The API surface — every endpoint, every request/response field
- The authentication model — per-app keys, header conventions, hashed storage, hot reload
- Rate limiting — token bucket math, when 429s fire, sizing per app
- The audit log — schema, jq query recipes, future log-shipping
- The admin CLI — all commands, the cryptographic decisions, what makes rotation safe
- The configuration system — environment variables vs config files
- Cloud independence — what changes if you redeploy on Azure, GCP, or on-prem
- Honest gaps — what this design *isn't* yet, and what production hardening looks like

**Reference back to Part 1** when you need infrastructure or OS-level context. **Coming in Part 3:** code walkthrough, file by file, function by function.

---

## 1. What the gateway does, in one paragraph

The gateway is the single doorway between any application and the LLM (and soon, the classifier). It validates that the caller's API key belongs to a real registered app; checks the app's per-minute rate limit; forwards the request to the appropriate backend (Ollama for LLM today, the FastAPI classifier later); logs the call to disk with everything needed to attribute usage; and returns the backend's response in a stable schema that won't change if you swap the backend underneath. It does this in ~600 lines of Python, runs as a single systemd-managed process on a single instance, and has no dependencies on any cloud service. That last point — no cloud dependencies — is intentional and is what makes the design portable.

---

## 2. The API surface

Five endpoints, three of them live today.

```
GET    /healthz              public           backend reachability snapshot
GET    /v1/me                authenticated    info about the calling app
POST   /v1/generate          authenticated    LLM single-shot completion
POST   /v1/chat              authenticated    LLM multi-turn conversation
POST   /v1/classify          authenticated    MPPT fault classifier (placeholder)
```

### 2.1 `GET /healthz`

The only unauthenticated endpoint. Used by the ALB target-group health check and by anything else that wants to know if the gateway is alive.

```bash
curl http://your-alb-0000000000.ap-south-1.elb.amazonaws.com/healthz
```

Response:
```json
{
  "status": "ok",
  "backends": {
    "ollama": "ok",
    "classifier": "unreachable: ConnectError"
  }
}
```

Two things to know:
- **The ALB only cares about the 200**, not the body content. Even if every backend is down, this endpoint still returns 200 — the gateway itself is up, which is what the ALB is asking.
- **The body distinguishes "gateway up but backend down" from "gateway down."** If you ever see a 503 from the ALB (no healthy targets), it's the gateway; if you see `{"status":"degraded","backends":{...}}`, the gateway is fine but a backend isn't.

If you wanted the target to fail when backends are down (so the ALB takes the instance out of rotation during an Ollama outage), you'd change the target group's health check path to one that returns non-200 when degraded. Default chosen for resilience: an Ollama crash shouldn't make `/healthz` fail in a way that hides the fact that authentication still works.

### 2.2 `GET /v1/me`

Returns the calling app's record. Pure debugging endpoint — confirms a key is valid and reads correctly.

```bash
curl -H "Authorization: Bearer sk-gatekeeper-..." \
  http://gatekeeper-alb-.../v1/me
```

Response:
```json
{
  "app": "prod-dashboard",
  "active": true,
  "rate_limit_per_min": 100,
  "created_at": "2026-06-29T19:21:06+00:00"
}
```

Use cases:
- Dev team's "is my key working?" sanity check before integration.
- Operator's quick check after issuing a key.
- CI smoke tests for an integration pipeline.

### 2.3 `POST /v1/generate`

Single-prompt completion via Ollama's `/api/generate`. Stateless — no memory of previous calls.

Request body:
```json
{
  "model": "qwen3.5:9b",
  "prompt": "What is solar panel soiling?",
  "system": "You are a solar O&M assistant.",
  "max_tokens": 200,
  "temperature": 0.4,
  "stream": false,
  "think": false
}
```

Response:
```json
{
  "model": "qwen3.5:9b",
  "response": "Solar panel soiling is the accumulation of dust...",
  "tokens_in": 47,
  "tokens_out": 38,
  "latency_ms": 1240
}
```

Field-by-field notes:

| Field | Required | Default | Notes |
|---|---|---|---|
| `model` | yes | — | Backend model name, e.g. `qwen3.5:9b` |
| `prompt` | yes | — | The user's text |
| `system` | no | — | System prompt; persona/instructions |
| `max_tokens` | no | 512 | Hard cap on response length, 1–8192 |
| `temperature` | no | 0.7 | 0.0 = deterministic, 1.0+ = creative |
| `stream` | no | false | Not implemented yet |
| `think` | no | false | Enable model thinking (slower, see below) |

**The `think` flag** is critical for qwen3.5 (and other reasoning models). With thinking on, the model spends tokens internally reasoning before producing the visible response — and with low `max_tokens` (e.g. 60), the entire budget can be eaten by reasoning, leaving you with an empty `response`. We default it to `false` for this reason. Pass `true` only when you want the model to deliberate more carefully on hard problems and you've set `max_tokens` high enough (400+) to leave room for actual output.

**Why our schema differs from Ollama's raw API:** Ollama returns a verbose response with timing fields, model metadata, evaluation durations, etc. The gateway picks out just the fields the dev team needs and gives them stable names. If you ever swap Ollama for vLLM or Triton, the gateway adapter changes but the dev team's contract doesn't.

### 2.4 `POST /v1/chat`

Multi-turn conversation. The gateway and model are stateless — your application stores the conversation history and resends it every turn.

Request body:
```json
{
  "model": "qwen3.5:9b",
  "messages": [
    {"role": "system", "content": "You are a solar plant O&M assistant. Respond in Hinglish."},
    {"role": "user", "content": "String 12 at Bidar shows soiling 0.87. Write a work order."},
    {"role": "assistant", "content": "Bhai, String 12 pe soiling detect hui hai..."},
    {"role": "user", "content": "Crew kahan bheju, aur kaunsa equipment chahiye?"}
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
    "content": "Cleaning crew Bidar plant bhejo, DI water aur microfiber rollers chahiye..."
  },
  "tokens_in": 124,
  "tokens_out": 89,
  "latency_ms": 2150
}
```

**The pattern** the client uses:

```
turn 1:
  client maintains: [system, user1]
  → sends [system, user1] to /v1/chat
  ← receives assistant1
  client now stores: [system, user1, assistant1]

turn 2:
  user types user2
  client maintains: [system, user1, assistant1, user2]
  → sends [system, user1, assistant1, user2] to /v1/chat
  ← receives assistant2
  client now stores: [system, user1, assistant1, user2, assistant2]
```

The gateway holds nothing between calls; the client carries the conversation. This is the same shape as OpenAI's `/v1/chat/completions` for a reason — it's the simplest stateless design that supports multi-turn.

**Context-length caveat:** the model has a finite context window (32k tokens for qwen3.5:9b in theory; effective recall degrades past ~8k in practice). Long conversations eventually need to either drop oldest turns or summarize them. The client owns that policy — the gateway doesn't truncate or summarize automatically. Document this expectation when you onboard the dev team.

### 2.5 `POST /v1/classify` (placeholder)

The schema for the MPPT fault classifier endpoint, defined now even though the backend isn't built yet, so dev team can wire their app against the contract.

Request body:
```json
{
  "plant": "bidar",
  "string_id": "S012",
  "window": [
    [480.1, 6.2, 41.0, 920.0],
    [480.4, 6.1, 41.2, 925.0],
    ...
  ]
}
```

Response:
```json
{
  "plant": "bidar",
  "string_id": "S012",
  "fault": "soiling",
  "confidence": 0.87,
  "probabilities": {
    "soiling": 0.87,
    "shading": 0.04,
    "degradation": 0.06,
    "hardware": 0.03
  },
  "latency_ms": 32
}
```

The `window` shape is `[time_steps][channels]` — a sliding window of MPPT readings, channels being Vmpp / Impp / module-temp / irradiance per scan. The schema is intentionally loose until the data scientist nails down window length and channel order, but the *envelope* (plant, string, window in, fault+confidence+probabilities out) is stable.

Today calling this endpoint returns a 502 because the backend is unreachable. That's the right error: the route exists, auth works, schema validation works — only the backend's missing.

### 2.6 The versioning promise

All authenticated endpoints live under `/v1/`. The promise to the dev team:

- **`/v1/*` is stable.** No breaking changes to existing endpoints — fields may be added but never renamed, removed, or repurposed. Required fields stay required.
- **Breaking changes go to `/v2/*`** with at least 30 days of overlap where both work.

This is the same contract OpenAI, Anthropic, GitHub, and Stripe make. It's the only thing that lets dev teams build against your API without fear.

**`/healthz` is unversioned** — it's an operational endpoint that the ALB and operators use, not part of the dev-facing contract.

---

## 3. The authentication model

### 3.1 Per-app keys

Each calling application gets exactly one key. The key looks like:

```
sk-gatekeeper-<48 url-safe random characters>
```

The `sk-gatekeeper-` prefix is a convention copied from OpenAI (`sk-`) and is purely cosmetic — it makes keys identifiable at a glance in logs and code. The random tail is generated with Python's `secrets.token_urlsafe(36)`, which produces 36 bytes of cryptographically random data encoded as ~48 url-safe characters. That's roughly 288 bits of entropy — well beyond what brute-force attacks could touch in the lifetime of the universe.

**One key per app, not one key per user.** The gateway authenticates *applications*, not end users. If your dashboard has 50 human users, all 50 share the dashboard's key. The dashboard is responsible for its own user authentication (typically OAuth/SSO via your corporate IdP). This separation matters: it means the gateway doesn't need user identity, doesn't store PII, and doesn't need to talk to your IdP.

### 3.2 Header conventions

The gateway accepts the key in either of two headers:

```http
Authorization: Bearer sk-gatekeeper-...
X-API-Key: sk-gatekeeper-...
```

**Bearer is preferred** because it's the IETF standard for token-based auth (RFC 6750) and most HTTP clients support it natively. `X-API-Key` is accepted as a fallback because some legacy clients have standardized on it.

**What the gateway will not accept:**
- Key in the URL (query parameter) — URLs end up in access logs, browser history, Slack pastes
- Key in a cookie — meant for browser-driven sessions, not service-to-service auth
- Key in a JSON body field — non-standard, harder to firewall/audit

If you forget the header entirely, you get a 401 with `WWW-Authenticate: Bearer` (also RFC 6750 compliant), which tells well-behaved clients exactly what to do next.

### 3.3 What's actually stored — the SHA-256 pattern

This is the most important security decision in the design and worth understanding completely.

**What the keys file looks like on disk:**

```yaml
apps:
  prod-dashboard:
    key_hash: 7b545f95f0675c0c769f87b56378d3830ed655a959a9f7272884a9a475aa0390
    active: true
    rate_limit_per_min: 100
    created_at: '2026-06-29T19:21:06+00:00'
    notes: first integration test
  mobile-backend:
    key_hash: 40aaf2a18d516037b91140769f008b1bf8d0f593f0aae793b9e649e0fed835e7
    active: true
    rate_limit_per_min: 30
    created_at: '2026-06-29T19:25:00+00:00'
    notes: mobile team
```

Notice: **only the SHA-256 hash of each key is stored**. The plaintext `sk-gatekeeper-...` is never written to disk by the gateway.

**The flow on `issue`:**
1. CLI generates plaintext: `secrets.token_urlsafe(36)` → `sk-gatekeeper-...`
2. CLI computes `hashlib.sha256(plaintext).hexdigest()` → 64-char hex string
3. CLI writes only the hex hash to `keys.yaml`
4. CLI prints the plaintext to the terminal **once**
5. Operator captures the plaintext, hands it to the dev team out-of-band, discards their copy

**The flow on every authenticated request:**
1. Gateway extracts the plaintext from the `Authorization` header
2. Gateway hashes it (same SHA-256)
3. Gateway scans `keys.yaml` for a matching hash
4. If found, the request proceeds as that app

**Why this matters — the threat model:**
- If `keys.yaml` leaks (filesystem misconfiguration, backup compromise, accidental git commit), the attacker has the *hashes* but not the *keys*. They can't authenticate as any app.
- SHA-256 is one-way; you can't derive the plaintext from the hash. The only way to find the plaintext is brute force, which is computationally infeasible at 288 bits.
- This is the same reason `/etc/shadow` stores hashed passwords, not plaintext.

**What it costs:** there is no key recovery. If a dev team loses their key, they cannot get it back — you must `rotate` (which issues a new plaintext and replaces the hash) and re-deliver. This is the right trade-off; the alternative (recoverable keys, encrypted with a master key) just moves the problem to "where do we store the master key" and adds operational complexity.

### 3.4 Hot reload — changes without restart

The gateway reads `keys.yaml` from disk on every authenticated request (almost — see below). When you change the file via the CLI, the next request picks up the change without restart.

**How it actually works** (see `app/storage.py` for the code; covered in Part 3):

```
in-memory cache:
  by_hash: { sha256_hex: AppRecord, ... }
  source_mtime: float (file's mtime when last loaded)

on every authenticated request:
  current_mtime = stat(keys.yaml).st_mtime
  if current_mtime != source_mtime:
    reload the file, replace the cache
  look up incoming key's hash in the cache
```

The check is just `os.stat()` — cheap. The full file is only re-read when the file actually changed on disk. So in steady state, lookups are O(1) hash-table reads with zero I/O.

**Why this matters operationally:** you can disable a compromised app's key, rotate it, or change its rate limit *immediately*, with no service interruption. The CLI updates the file; the next request flows under the new rules.

**One subtle case:** if you edit `keys.yaml` by hand and don't change the mtime (e.g., copy-on-write filesystem quirk, manual `cp -p`), the reload won't trigger. Don't edit by hand. The CLI uses a write-tmp-then-rename pattern that always updates the mtime correctly.

### 3.5 Why the "plaintext shown once" rule

The CLI prints the new plaintext key exactly once, on `issue` or `rotate`:

```
issued key for 'prod-dashboard':
  sk-gatekeeper-Khix8nQ9Ow3...

store this key now — it cannot be recovered later.
```

After that command exits, the plaintext is gone — not in any file, not in any process memory still running. Only the hash remains.

**The discipline this enforces:**
- Operator must capture the key *now*, not "I'll grab it later." Encourages immediately moving it to 1Password / sealed message / secure share.
- Removes the temptation to leave keys in the shell history "in case I need them." (Pro tip: also clear shell history after issuing keys — `history -c` for bash, or use a shell that supports `HISTCONTROL=ignorespace` and prefix the command with a space.)
- Makes it impossible for a compromised operator account to retroactively dump all keys.

It also means **every rotation is a fresh start.** If you suspect any kind of compromise, `rotate` issues a new plaintext, invalidates the old hash, and you re-deliver. No ambiguity about which keys are current.

---

## 4. Rate limiting

### 4.1 What problem it solves

Three real problems:

1. **One badly-behaved app shouldn't impact others.** If a dev team accidentally hammers the LLM with 1000 requests/second from a loop, the GPU saturates, and *every* other app sees latency spikes. Rate limits force isolation — no single app can monopolize the resource.

2. **Cost / capacity protection.** GPU inference isn't free. A runaway client could burn through enormous compute. The limit caps the worst case.

3. **Soft signal for capacity planning.** If an app regularly hits its limit, that's a measurable signal that it needs more or that its usage pattern needs reshaping.

### 4.2 Token bucket explained from first principles

The classic algorithm. Conceptually:

```
                            ╔══════════════════════════════╗
   tokens drip in           ║                              ║
   continuously, at the     ║         capacity = N         ║
   "refill rate"            ║         (max tokens          ║
                            ║          you can hold)       ║
                            ║                              ║
                            ║  current tokens: X / N       ║
                            ║                              ║
                            ╚══════════════╤═══════════════╝
                                           │
                                           ▼
                                   request arrives,
                                   "spends" 1 token
                                   (or rejected if X < 1)
```

For a `rate_limit_per_min = 60`:
- **Capacity:** 60 tokens (the bucket can hold up to 60)
- **Refill rate:** 60 / 60 = **1 token per second**
- Starts full at 60 tokens

**Bursty traffic:** if you've been idle, the bucket fills up. If 60 requests arrive at once, all 60 succeed (your bucket had 60 tokens). The 61st request gets a 429 because the bucket is now empty and the next token arrives ~1 second later.

**Steady traffic:** if you're sending 1 request per second, every request finds at least 1 token in the bucket and refills are keeping up.

**Sustained over-rate:** if you send 2 requests per second against a 1-per-second refill, the bucket drains in 60 seconds and then every other request 429s.

**Why this is the right algorithm:**
- It naturally allows brief bursts (which real apps do — startup spikes, batched operations) while still capping sustained rate.
- It's stateless except for two numbers per app (token count and last-refill timestamp). Easy to reason about, easy to scale to Redis later.
- It's the same algorithm AWS API Gateway, Nginx, Stripe, GitHub, and almost everyone else uses.

### 4.3 When 429 fires — and what the client should do

When the bucket has fewer than 1 token, the gateway returns:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 2
Content-Type: application/json

{
  "error": "rate limit exceeded for prod-dashboard; retry in 1.2s",
  "request_id": "..."
}
```

**The `Retry-After` header is the client's contract:** wait at least that many seconds before retrying. A good client implements exponential backoff: wait, then if you 429 again, wait longer.

**What a bad client does** (and you'll see this in the audit log):
- Retries immediately, gets 429 again, hammers in a tight loop. Don't tolerate this — point them to the documentation and have them implement backoff.

**Production handling pattern** for the dev team's client:
```python
import time, httpx

def call_with_retry(client, url, **kwargs):
    delay = 1
    while True:
        resp = client.post(url, **kwargs)
        if resp.status_code != 429:
            return resp
        wait = float(resp.headers.get("Retry-After", delay))
        time.sleep(wait)
        delay = min(delay * 2, 60)  # exponential backoff, capped
```

### 4.4 Per-app limits and rationale

Each app gets its own bucket. Limits are set when you issue the key and can be changed via `rotate --rate` (or by editing the rate field in keys.yaml directly, which hot-reloads).

**Suggested starting points:**

| Workload | Suggested `rate_limit_per_min` | Reasoning |
|---|---|---|
| Internal dashboard, interactive | 60–100 | Humans don't click that fast; spikes from page loads |
| Mobile app backend | 30–60 | User-driven; rate-limited at the user level already |
| Batch processing | 100–500 | Burst high, sustained acceptable |
| Background indexing/embeddings | 1000+ | High throughput, single client |
| Probe / monitoring | 5–10 | Low intentionally |

**The math you should do for each app:** what's the peak concurrent users × peak requests-per-user-per-minute? Then add 50% headroom. That's your number.

**Why per-app and not global:** so one app can't starve another. The GPU itself is a global resource (one model serving at a time), and concurrent inference *does* slow each other down — but the rate limit at least prevents one app from queueing 1000 requests deep while others wait.

### 4.5 What rate limiting doesn't catch — token budgets

A single request can consume vastly different amounts of compute. A prompt of 50 tokens with `max_tokens: 50` is cheap. A prompt of 8000 tokens with `max_tokens: 4000` is *200x* more expensive. The rate limit treats both as one request.

**The right second layer** (not implemented yet, a v0.3 candidate) is a **token budget**: per app, X total tokens per day. The audit log already captures `tokens_in` and `tokens_out` per request — adding a budget check is a small addition. When it lands, you'll be able to say "App B gets 500 requests/min AND 10 million tokens/day."

Until then, watch the log for any app whose `tokens_out` averages are wildly high — that's the kind of thing that quietly burns money.

---

## 5. The audit log

### 5.1 Schema of one request

Every request, regardless of outcome, produces one line in `/var/log/gatekeeper/requests.jsonl`. One JSON object per line — that's it, no headers, no XML, no nested structures.

```json
{
  "ts": 1782761540.4289117,
  "request_id": "6f1c4dbf27284453a71c086e7d6d1194",
  "app": "laptop-test",
  "method": "POST",
  "path": "/v1/generate",
  "status": 200,
  "latency_ms": 1554,
  "backend": "ollama",
  "model": "qwen3.5:9b",
  "tokens_in": 21,
  "tokens_out": 60,
  "error": null
}
```

Field by field:

| Field | Type | Meaning |
|---|---|---|
| `ts` | float | Unix epoch seconds (with sub-second precision) |
| `request_id` | string | Unique 32-char hex; echoes to `x-request-id` response header |
| `app` | string \| null | App name (from key lookup); null if auth failed before app resolution |
| `method` | string | HTTP method |
| `path` | string | Request path (no query string) |
| `status` | int | HTTP status code returned |
| `latency_ms` | int | Total time inside the gateway (auth + rate-check + backend call + response build) |
| `backend` | string \| null | Which backend was called (`ollama`, `classifier`, or null for meta endpoints) |
| `model` | string \| null | Model name (LLM calls only) |
| `tokens_in` | int \| null | Prompt tokens (LLM calls only) |
| `tokens_out` | int \| null | Response tokens (LLM calls only) |
| `error` | string \| null | Human-readable error reason; null on success |

**Critically: no sensitive data is logged.**
- No request bodies (so user prompts and PII don't end up in logs)
- No keys (only the resolved app name)
- No response bodies (so generated content doesn't end up in logs either)

If you ever need prompt-level audit (e.g. for compliance / a "what did our LLM say" investigation), that's a separate feature with separate security controls — not the same as operational logging.

### 5.2 What it answers — operator queries

Five questions you'll ask all the time, with the jq one-liner that answers each:

**"Who used the platform today?"**
```bash
sudo jq -r '.app' /var/log/gatekeeper/requests.jsonl | sort | uniq -c | sort -rn
```

**"How many tokens has each app consumed?"**
```bash
sudo jq -r 'select(.tokens_out != null) | "\(.app) \(.tokens_in + .tokens_out)"' \
  /var/log/gatekeeper/requests.jsonl \
  | awk '{s[$1]+=$2} END {for (a in s) print s[a], a}' | sort -rn
```

**"Which apps are hitting rate limits?"**
```bash
sudo jq -r 'select(.status==429) | .app' /var/log/gatekeeper/requests.jsonl | sort | uniq -c
```

**"What's failing?"**
```bash
sudo jq -r 'select(.status>=500) | "\(.status) \(.backend) \(.error)"' \
  /var/log/gatekeeper/requests.jsonl | sort | uniq -c
```

**"How slow is the slowest 1% of requests?"**
```bash
sudo jq -s 'map(select(.status==200 and .latency_ms!=null)) | sort_by(.latency_ms) | .[-(length/100|floor):] | .[].latency_ms' \
  /var/log/gatekeeper/requests.jsonl
```

### 5.3 Why JSONL specifically

Not JSON-array, not CSV, not a database. JSONL because:

- **Append-only.** Each line is a complete object. No file-level structure to maintain. The writer just opens with `a` mode and writes one line. Crash mid-write, you lose at most one line; the rest of the file is intact.
- **Streamable.** Every line tool (`grep`, `awk`, `jq`, `tail -f`) works line by line. You can grep without loading the file. Loki, Vector, Fluent Bit, Promtail — every modern log shipper handles JSONL natively.
- **Self-describing.** Each line carries its field names. Schema evolution is trivial: add a field tomorrow and old tools still work (they just ignore it).
- **No locking complications.** Concurrent writes use a single mutex in process; the OS guarantees atomic small writes for line-sized records.

### 5.4 Future — shipping to Loki or Prometheus

Today the log lives on the EC2's disk. Two real concerns:

1. **If the instance dies, the log dies with it.** Recovery: rotate the file to S3 nightly (cron in Part 1 section 9.2). Easy fix.
2. **Querying via jq across days/weeks gets clunky.** Real fix: ship to a log aggregator.

**When you're ready, the path is straightforward:**

- Install Promtail or Vector on the EC2
- Point it at `/var/log/gatekeeper/requests.jsonl`
- It tails the file and pushes each line to Loki / Elasticsearch / etc.
- Grafana queries become Loki LogQL queries
- The audit log becomes time-series-queryable, with retention policies and dashboards

The application code doesn't change — that's the beauty of structured logs to a file. Logging stays a one-line write; the *shipping* is an external concern handled by purpose-built tools.

For **metrics** (request counts per app, latency histograms, etc.), the cleaner addition is a Prometheus `/metrics` endpoint that increments counters from the same data the log captures. That's a small addition (~50 lines using the `prometheus-client` library) and is part of the planned v0.3 monitoring story.

---

## 6. The admin CLI

`/usr/local/bin/gatekeeper-keys` — a small Python script that's the only sanctioned way to touch `/etc/gatekeeper/keys.yaml`.

### 6.1 Anatomy

It's ~150 lines of Python. No external dependencies beyond `yaml` (already installed for the gateway). It does five things:
- Generates random keys with `secrets.token_urlsafe(36)`
- Hashes plaintext with SHA-256
- Reads / writes `keys.yaml` using atomic write-tmp-then-rename
- Performs the actual key-management operations
- Pretty-prints output

It deliberately does NOT:
- Talk to the gateway (no API calls; works even if the gateway is down)
- Talk to AWS / any cloud (works offline; works on-prem)
- Store anything outside `keys.yaml`

### 6.2 Commands one by one

The user-facing alias is `gatekeeper-keys`, which expands to `sudo -u gatekeeper gatekeeper-keys` so file ownership stays correct.

#### `list`

```bash
gatekeeper-keys list
```

Output:
```
app                    active       rate/min  created_at             notes
------------------------------------------------------------------------------------------
mobile-backend         yes            30/min  2026-06-29T07:03:24+00:00 mobile team
prod-dashboard         yes           100/min  2026-06-29T19:21:06+00:00 first integration test
```

Shows every app, active/inactive state, rate limit, when issued, notes. Useful for periodic review ("who has access?").

#### `issue`

```bash
gatekeeper-keys issue <app_name> --rate <per_min> --notes "owner: team-x"
```

Generates a new key for a new app. If `app_name` already exists, fails — use `rotate` for existing apps. Prints the plaintext exactly once.

```
issued key for 'prod-dashboard':
  sk-gatekeeper-Khix8nQ9Ow3...

store this key now — it cannot be recovered later.
```

#### `rotate`

```bash
gatekeeper-keys rotate <app_name> [--rate <per_min>]
```

Replaces the key for an existing app. The old hash is overwritten; old key is immediately invalid. Optionally bumps the rate limit at the same time. Prints the new plaintext once.

**Use when:**
- Routine 90-day rotation
- Suspected compromise
- Rate limit needs to change (one command, do both)

#### `disable` / `enable`

```bash
gatekeeper-keys disable <app_name>
gatekeeper-keys enable <app_name>
```

Soft toggle. Disable makes the gateway return 401 on the next request from that app (hot-reload picks up the change within ~1 second). Enable reverses it. The key itself isn't touched.

**Use disable for:**
- Suspected compromise where you want to investigate before deciding to rotate
- Decommissioning an app temporarily
- Emergency: stop an app that's misbehaving while you discuss with their team

#### `remove`

```bash
gatekeeper-keys remove <app_name>
```

Permanently deletes the app entry. There is no undo. The key, the rate-limit config, the notes — all gone. Audit log entries with this app's name stay (that history is preserved on disk).

**Use remove when:**
- An app is permanently decommissioned and you're confident the team has rotated all references
- The app name was issued in error

In doubt, prefer `disable` — costs nothing to keep an inactive entry, and reversible.

### 6.3 The cryptographic decisions, summarised

Three choices, three reasons:

1. **`secrets.token_urlsafe(36)` for generation.** From Python's `secrets` module, which is the standards-compliant cryptographic RNG. Uses the OS's CSPRNG (`/dev/urandom`). 36 bytes of entropy = 288 bits — more than enough.
2. **SHA-256 for storage.** Fast, collision-resistant, no known practical attacks at the protected level. We don't need a key-derivation function like bcrypt/scrypt/argon2 because keys are *high-entropy random strings*, not low-entropy user passwords; the entropy itself defeats brute force without needing a slow hash.
3. **Atomic write-tmp-rename for `keys.yaml`.** The CLI writes to `keys.yaml.tmp`, then `os.replace()` swaps it into place. Either the old file is intact or the new one is — never a half-written file. Matters because the gateway is reading this file concurrently.

---

## 7. The configuration system

### 7.1 `gatekeeper.env` line by line

```bash
GATEKEEPER_HOST=0.0.0.0
GATEKEEPER_PORT=8080
OLLAMA_BASE_URL=http://127.0.0.1:11434
CLASSIFIER_BASE_URL=http://127.0.0.1:9000
GATEKEEPER_KEYS_FILE=/etc/gatekeeper/keys.yaml
GATEKEEPER_LOG_FILE=/var/log/gatekeeper/requests.jsonl
GATEKEEPER_DEFAULT_RATE_LIMIT_PER_MIN=60
GATEKEEPER_REQUEST_TIMEOUT_SECONDS=120
GATEKEEPER_LOG_LEVEL=INFO
```

| Variable | What it controls |
|---|---|
| `GATEKEEPER_HOST` | uvicorn bind address. `0.0.0.0` = all interfaces; `127.0.0.1` = localhost only |
| `GATEKEEPER_PORT` | uvicorn port. Match the target group |
| `OLLAMA_BASE_URL` | Where to find Ollama. Always localhost in this deployment |
| `CLASSIFIER_BASE_URL` | Where to find the classifier (will be localhost when built) |
| `GATEKEEPER_KEYS_FILE` | The keys file path. Used by both gateway and CLI |
| `GATEKEEPER_LOG_FILE` | Audit log path |
| `GATEKEEPER_DEFAULT_RATE_LIMIT_PER_MIN` | Default rate when an app's record omits one |
| `GATEKEEPER_REQUEST_TIMEOUT_SECONDS` | Max time to wait for the backend. LLM calls can be slow, so 120 |
| `GATEKEEPER_LOG_LEVEL` | Python logging level for the gateway's own logs (not the audit log) |

### 7.2 Why environment variables and not a config file

The gateway uses environment variables (via `pydantic-settings`) for configuration, not a YAML/TOML/JSON config file. Why:

- **12-factor app principle.** Configuration that varies between environments (dev/staging/prod) belongs in the environment, not in the code or a baked-in file.
- **Container-friendly.** When you eventually run this in Kubernetes, env vars are how every secret-management and config-management system delivers values.
- **Trivial to override.** `GATEKEEPER_PORT=18080 systemctl edit gatekeeper` and you've changed the port. No file edit dance.
- **No precedence confusion.** Just env vars. There's no "config file says X but env var says Y, which wins" debate.

`keys.yaml` is a different beast — it's *data*, not configuration. Per-app records that change frequently. Files are the right substrate for data; env vars are wrong for it.

---

## 8. Cloud independence

This is the part you specifically asked about. Why doesn't any of this lock you to AWS?

### 8.1 What the gateway does NOT depend on

Look at what the gateway code (`app/*.py`) calls. The full list:

- Python standard library (file I/O, hashing, time, secrets, logging)
- FastAPI / uvicorn / httpx / pydantic / PyYAML (pure Python libraries, no cloud SDKs)
- Linux filesystem semantics (read/write files, atomic rename, mtime)
- HTTP

That's it. No `boto3`, no `aws-sdk`, no `azure-identity`, no `google-cloud-*`. Zero cloud SDK imports. The gateway has no idea what cloud it's on.

**What's stored on the local filesystem (no managed services):**
- The keys file
- The audit log
- The systemd unit
- The venv and code

This is the choice that makes the design portable. We could have used AWS Secrets Manager for keys (with automatic rotation), CloudWatch Logs for the audit, IAM for service auth — each integration is real and useful but each one is AWS-specific. Adding them would have made the code "good AWS code" but tied to AWS.

### 8.2 What would change to move to Azure

| Layer | AWS today | Azure equivalent |
|---|---|---|
| VPC + subnets + routing | VPC, Subnets, IGW, route tables | VNet, Subnets, NAT Gateway / Public IPs, UDRs |
| Load balancer | Application Load Balancer (ALB) | Application Gateway (with WAF if you want) |
| Compute | EC2 g5.2xlarge | NC-series VM with A10 / A100 |
| Network security | Security Groups | Network Security Groups |
| Identity (for ops) | IAM users / SSO | Azure AD |
| Storage volume | EBS | Managed Disks |
| Backups bucket | S3 | Blob Storage |
| Domain / DNS / cert | Route 53 + ACM | Azure DNS + Key Vault certs (or Let's Encrypt) |
| **Gateway code** | **same** | **same** |
| **Keys file format** | **same** | **same** |
| **systemd unit** | **same** | **same** |
| **Ollama setup** | **same** | **same** |
| **Audit log shape** | **same** | **same** |

The whole "below the gateway" stack changes — every networking name, every console UI. The whole "gateway and above" stack is identical. Same Python files, same systemd unit, same CLI, same disaster-recovery script.

**Realistic Azure migration time:** maybe 2–3 days for the infrastructure (re-implementing the VNet, NSGs, AppGw, VM), 1 hour for the application (scp the tarball, run the rebuild script). The application *itself* would migrate in an afternoon.

### 8.3 What would change to move to GCP

| Layer | AWS today | GCP equivalent |
|---|---|---|
| Network | VPC + Subnets + Routes | VPC + Subnets + Routes (very similar concepts) |
| Load balancer | ALB | Global HTTPS LB or Regional Network LB |
| Compute | EC2 g5.2xlarge | GCE A2 / G2 instance with A100 / L4 |
| Network security | Security Groups | VPC firewall rules + tag-based targets |
| Storage | EBS | Persistent Disk |
| Backups | S3 | Cloud Storage |
| Domain / cert | Route 53 + ACM | Cloud DNS + managed SSL |
| **Everything from gateway up** | **same** | **same** |

GCP is slightly different in shape (firewall rules are global, not SG-attached-to-resources), but the patterns translate cleanly.

### 8.4 What would change to move on-prem / bare metal

This is the strongest portability test. No cloud at all.

| Layer | AWS today | On-prem equivalent |
|---|---|---|
| Network | VPC | physical VLAN / your corporate network |
| Load balancer | ALB | nginx, HAProxy, F5, Citrix ADC, etc. |
| Compute | EC2 g5 | bare metal server with NVIDIA GPU |
| Network security | SGs | iptables / firewall appliances |
| Storage | EBS | local disks, NAS, SAN |
| Backups | S3 | NFS / your backup target |
| Domain / cert | Route 53 + ACM | corporate DNS + Let's Encrypt or internal CA |
| **Gateway** | **same** | **same** |

On-prem deploys would actually be *easier* in some ways — no security group dance, no IAM, no managed services to learn. The gateway runs as systemd on Linux; that works literally anywhere.

### 8.5 The trade-offs we made for portability

Honest accounting of what we gave up by choosing file-based storage and no cloud SDKs:

**What we gave up:**
- **Automatic rotation via Secrets Manager.** AWS Secrets Manager can rotate secrets on a schedule and notify subscribers. We rotate manually via CLI. For a 5-app POC, manual is fine; at 50+ apps you'd want automation.
- **Centralized audit / compliance.** CloudTrail captures every API call to AWS Secrets Manager. With file-based keys, your only audit is the operator's command history. For SOC2 / regulated environments, this matters.
- **HA out of the box.** Managed services replicate automatically. Our single file on a single instance is a single point of failure (mitigated by backups, but not zero-downtime).
- **Hardware security module (HSM).** Real secret management uses HSMs for key wrap. We don't.

**What we got:**
- Full portability across clouds and on-prem.
- Zero dependence on a specific vendor's pricing, availability, or product decisions.
- Read code; understand the whole system.
- Test offline. No "AWS down" failure mode.
- Trivial to migrate.

**The right framing:** what we built is a **POC-grade primitive**. It's *exactly* the right level of complexity for a POC and for environments with simple compliance needs. When you cross into "regulated production with audit requirements," some of these decisions need revisiting — and at that point, you'd swap the storage layer for Secrets Manager (an ~80-line change in `storage.py`), keep everything else identical.

That swap-the-layer flexibility is the *real* portability benefit. The gateway is structured so a different storage backend, a different log sink, or a different metric system is a localized change — not a rewrite.

---

## 9. What this design is NOT (yet)

Honest gaps. Things production-grade systems have that this doesn't.

| Gap | What's missing | When it matters |
|---|---|---|
| Per-user identity | The gateway authenticates apps, not users. Apps must implement their own user auth. | Always; this is the right design. Not a gap, an architectural choice. |
| OAuth / OIDC | No support for OAuth 2.0 flows, JWTs, or refresh tokens. Static API keys only. | When you need short-lived credentials per user. |
| Token-level budgets | Rate limit is requests/min only. No tokens/day cap. | When LLM costs become a real budget concern. |
| Streaming responses | `stream: true` not implemented. Clients wait for the full response. | When you want progressive UI rendering. |
| Multi-replica gateway | One process on one host. Restart = brief downtime. | Once you have > 1 critical app depending on uptime. |
| Redis-backed rate limit | In-memory bucket; doesn't survive restarts (you get one free burst). | When you scale to multiple replicas. |
| Prometheus metrics endpoint | Counters/histograms live only in the log file. | When you set up Grafana dashboards. |
| Loki log shipping | Logs are on local disk. | Within a few weeks of the dev team integrating. |
| mTLS between ALB and gateway | Plaintext HTTP from ALB to EC2 (inside VPC). | High-security workloads where intra-VPC traffic isn't trusted. |
| Per-app content policies | All apps can request anything. No content filtering, no PII detection. | When you serve user-facing applications and have policy obligations. |
| Model-level metering | Gateway logs `model` but doesn't differentiate cost. | When you serve multiple models with different cost profiles. |
| Request body / response logging | Not logged for privacy. | When you have a "what did the LLM say" audit requirement. |

**Don't read this list as "the design is incomplete."** Read it as "here's the v0.3 / v0.4 / v1.0 roadmap, organized by priority." A POC that has 80% of what production needs and is *honest about the 20% gap* is far better than one that tries to do everything and ships none of it well.

---

## 10. What's next

Part 3 will walk every file in the gateway:

- `app/main.py` — routes, middleware, lifespan, exception handling
- `app/auth.py` — header parsing, key validation
- `app/rate_limit.py` — token-bucket implementation
- `app/proxy.py` — backend forwarding (Ollama generate, Ollama chat, classifier)
- `app/models.py` — Pydantic schemas
- `app/storage.py` — keys file loading, mtime-based hot reload, audit log appender
- `app/config.py` — environment variable loading
- `scripts/gatekeeper-keys` — admin CLI

For each, we'll cover what it does, why the structure is what it is, the interesting decisions, and what you'd change if you needed to swap something out.

Say "Part 3" when you're ready.
