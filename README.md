# Gatekeeper

A self-hosted, **OpenAI-compatible** API gateway that sits in front of your own LLM/ML model servers (e.g. [Ollama](https://ollama.com), vLLM) and adds the things a raw model server lacks: **API-key authentication, per-key rate limiting, per-request audit logging**, and a stable OpenAI-shaped contract your apps can code against.

Point the OpenAI SDK at it and it just works — integration is a base-URL change.

```
        api-key / Bearer                     localhost
Client ──────────────────▶  Gatekeeper  ──────────────▶  Ollama / vLLM / classifier
        HTTPS (via your LB)   (this app)      HTTP          (your model backends)
```

## Highlights

Two things set Gatekeeper apart from a plain reverse proxy in front of a model server:

### 🔑 Built-in API-key management (no database, no external auth service)
Issue, rotate, disable, and list keys with a single bundled CLI. Keys are stored only as **SHA-256 hashes** (the plaintext is shown once and never persisted), each key has its own **rate limit**, and changes take effect within ~1 second with **no restart** (the keys file hot-reloads). That's a complete auth layer in one file — nothing else to run.

```bash
gatekeeper-keys issue  payments-api --rate 100    # create (prints the key once)
gatekeeper-keys rotate payments-api               # roll the key, old one dies
gatekeeper-keys disable payments-api              # instant 401s
gatekeeper-keys list                              # audit who has access
```

### 🎯 Per-key model access control
Each key can be scoped to exactly the models it's allowed to call — decided **at key-issue time**:

- **One key with access to *all* models** — issue it with no restriction:
  ```bash
  gatekeeper-keys issue platform-team --rate 100
  ```
- **One key restricted to a *single* (or specific) model** — pass `--models`:
  ```bash
  gatekeeper-keys issue mobile-app --rate 30 --models gemma4:e4b
  ```

A restricted key calling any other model gets a **403 before the request ever reaches the backend**, and `GET /v1/models` shows it only the models it's allowed to use. Keys with no restriction (the default) can call anything. This lets you hand your cheap/fast small model to high-volume apps while reserving the big models for keys that should have them — all enforced at the gateway, not on the honor system.

## Why

Model servers like Ollama are built to run on `localhost` with no auth. The moment you want to expose one to other apps or teammates, you need a layer in front that answers: *Who is calling? Are they allowed? Which models can they use? How much? What did they ask?* Gatekeeper is that layer, kept deliberately small (~1,100 lines) so you can read the whole thing.

## Features

- **OpenAI-compatible** `POST /v1/chat/completions` (streaming + non-streaming) and `GET /v1/models` — works with the official `openai` SDK in any language, plus tools, multimodal content, and any OpenAI field passed straight through.
- **Built-in user/key management** — bundled admin CLI to issue/rotate/disable/list keys; **SHA-256-hashed** storage (plaintext shown once), hot-reloaded with no restart.
- **Per-key model access control** — scope any key to specific model(s) at issue time; disallowed models are rejected with `403` and hidden from `GET /v1/models`. No restriction = access to all models.
- **Three auth header styles** accepted: `api-key:` (Azure-style), `Authorization: Bearer` (OpenAI-style), and `X-API-Key:` (curl-style).
- **Per-key token-bucket rate limiting**, returning `429` with `Retry-After`.
- **Append-only JSONL audit log** — one line per request (app, path, status, latency, token counts, request id), never any secret.
- **Backend-agnostic**: zero cloud-SDK imports. Swapping Ollama for vLLM/TensorRT-LLM (all OpenAI-compatible) is a config change, not a rewrite.

## Quick start

```bash
# 1. install
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

# 2. configure (copy the example and edit paths if you like)
cp .env.example .env

# 3. issue an API key (prints the plaintext key once — save it)
export GATEKEEPER_KEYS_FILE=./keys.yaml
python3 scripts/gatekeeper-keys issue my-app --rate 60

# 4. run (expects an OpenAI-compatible backend, e.g. Ollama, at OLLAMA_BASE_URL)
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Call it with the OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="sk-gatekeeper-...")
resp = client.chat.completions.create(
    model="gemma4:12b",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Or curl:

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "api-key: sk-gatekeeper-..." -H "Content-Type: application/json" \
  -d '{"model":"gemma4:12b","messages":[{"role":"user","content":"Hello!"}]}'
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI Chat Completions (streaming + non-streaming) |
| GET | `/v1/models` | List models (OpenAI format) |
| GET | `/v1/me` | Returns the calling key's app record (verify a key) |
| GET | `/healthz` | Liveness + backend reachability (no auth) |

Interactive docs at `/docs` (Swagger UI) — click **Authorize**, paste a key, and try it in the browser.

## Admin CLI

```
gatekeeper-keys list                       list apps (never shows secrets)
gatekeeper-keys issue   <app> [--rate N] [--notes "..."] [--models M ...]   create a key (prints once)
gatekeeper-keys rotate  <app> [--rate N] [--models M ...]   issue a new key, invalidate the old
gatekeeper-keys disable <app>              deactivate (401s immediately)
gatekeeper-keys enable  <app>              re-activate
gatekeeper-keys remove  <app>              delete the app entry
```

### Restricting a key to specific models

By default a key may call any model. Pass `--models` to limit it — requests for
any other model get `403`, and `GET /v1/models` only lists the permitted ones:

```bash
# full access (no restriction)
gatekeeper-keys issue web-app --rate 100

# restricted: this key may ONLY call gemma4:e4b
gatekeeper-keys issue mobile-app --rate 30 --models gemma4:e4b

# allow a couple of models
gatekeeper-keys issue batch-job --models gemma4:e4b gemma4:12b

# change restrictions later (or clear them by passing --models with no values)
gatekeeper-keys rotate mobile-app --models gemma4:12b
```

## Configuration

All via environment variables (or a `.env` file). See `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `GATEKEEPER_HOST` / `GATEKEEPER_PORT` | `0.0.0.0` / `8080` | bind address |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | OpenAI-compatible model backend |
| `CLASSIFIER_BASE_URL` | `http://127.0.0.1:9000` | optional classifier backend |
| `GATEKEEPER_KEYS_FILE` | `/etc/gatekeeper/keys.yaml` | keys file (hashes only) |
| `GATEKEEPER_LOG_FILE` | `/var/log/gatekeeper/requests.jsonl` | audit log |
| `GATEKEEPER_DEFAULT_RATE_LIMIT_PER_MIN` | `60` | default per-key rate |
| `GATEKEEPER_REQUEST_TIMEOUT_SECONDS` | `120` | backend request timeout |

## Deployment

Runs anywhere Python runs. A `systemd` unit (`gatekeeper.service`) and a `Dockerfile` + `docker-compose.yml` are included. The gateway terminates no TLS itself — put it behind a load balancer or reverse proxy that does (the standard pattern).

**Security notes:** never commit your real `keys.yaml` or `.env` (both are git-ignored — only the `.example` files ship). Keys are hashed, so the keys file is low-risk, but treat it as sensitive anyway.

## License

MIT — see [LICENSE](LICENSE).
