# Gatekeeper — Operator Runbook

This is your day-to-day reference. Dev team uses `DEV_INTEGRATION.md`.

## What's running where

```
            +-------------------+
            |       ALB         |  443 → 8080
            +---------+---------+
                      |
                      v
   g5.2xlarge  +------+---------+      127.0.0.1:11434  +----------+
   (one EC2)   | gatekeeper|----------------------->|  Ollama  |
               |   :8080        |      127.0.0.1:9000   +----------+
               +----------------+----------------------->| classifier (soon) |
                                                        +-------------------+

Files on the host:
  /etc/gatekeeper/keys.yaml        keys + per-app config
  /etc/gatekeeper/gatekeeper.env      gateway env vars (systemd) or via docker-compose .env
  /var/log/gatekeeper/requests.jsonl   per-request audit log
```

Backends bind to localhost. Only the gateway is reachable from the ALB.

## First-time install

### Option A: Docker (recommended)

```bash
# host paths
sudo mkdir -p /etc/gatekeeper /var/log/gatekeeper
sudo chown $USER:$USER /etc/gatekeeper /var/log/gatekeeper

# config
cp .env.example .env
$EDITOR .env

# keys file (empty to start)
cat > /etc/gatekeeper/keys.yaml <<EOF
apps: {}
EOF
sudo chmod 600 /etc/gatekeeper/keys.yaml

# build + run
docker compose up -d --build
docker compose logs -f gatekeeper
```

### Option B: systemd

```bash
sudo useradd --system --home /opt/gatekeeper gatekeeper
sudo mkdir -p /opt/gatekeeper /etc/gatekeeper /var/log/gatekeeper
sudo cp -r app/ /opt/gatekeeper/
sudo cp -r scripts/ /opt/gatekeeper/
sudo python3 -m venv /opt/gatekeeper/venv
sudo /opt/gatekeeper/venv/bin/pip install -r requirements.txt
sudo cp .env.example /etc/gatekeeper/gatekeeper.env
sudo $EDITOR /etc/gatekeeper/gatekeeper.env
sudo chown -R gatekeeper:gatekeeper /opt/gatekeeper /etc/gatekeeper /var/log/gatekeeper
sudo chmod 600 /etc/gatekeeper/gatekeeper.env

sudo cp gatekeeper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gatekeeper
sudo systemctl status gatekeeper
```

## Managing keys

The CLI is the only sanctioned way to touch the keys file. Don't hand-edit `keys.yaml`.

```bash
# install the CLI on the host (one-time)
sudo install -m 0755 scripts/gatekeeper-keys /usr/local/bin/gatekeeper-keys
export GATEKEEPER_KEYS_FILE=/etc/gatekeeper/keys.yaml

# day-to-day
gatekeeper-keys list
gatekeeper-keys issue   prod-dashboard --rate 100 --notes "owner: solar-platform"
gatekeeper-keys rotate  prod-dashboard
gatekeeper-keys disable prod-dashboard
gatekeeper-keys enable  prod-dashboard
gatekeeper-keys remove  prod-dashboard
```

The plaintext key is printed **once** by `issue` and `rotate`. Capture it, deliver it to the requesting team out-of-band (1Password, sealed secret, signal), and don't keep a copy yourself.

Changes take effect within ~1 second — the gateway re-reads the file when its mtime advances. No restart needed.

## Onboarding a new application

1. Have the dev team tell you: app name (lowercase-with-hyphens), expected requests-per-minute, owner contact.
2. `gatekeeper-keys issue <app_name> --rate <n> --notes "owner: <team>; contact: <email>"`
3. Deliver the plaintext key to them via your secret-sharing channel.
4. Share `docs/DEV_INTEGRATION.md` with them.
5. Confirm they can hit `/v1/me` — if that works, they're integrated.

## Offboarding / suspicion of compromise

Immediate, no questions:
```bash
gatekeeper-keys disable <app_name>
```
This invalidates the key on the next request (within ~1 second) without removing audit history. To restore: `gatekeeper-keys enable <app_name>`.

To rotate (issue a new key, invalidate the old, keep the app):
```bash
gatekeeper-keys rotate <app_name>
```

To permanently delete:
```bash
gatekeeper-keys remove <app_name>
```

## Looking at usage

Everything goes to `/var/log/gatekeeper/requests.jsonl`. One JSON object per line.

Useful one-liners until proper monitoring is in place:

```bash
# requests per app today
jq -r '.app // "anonymous"' /var/log/gatekeeper/requests.jsonl | sort | uniq -c | sort -rn

# tokens used per app
jq -r 'select(.tokens_in!=null) | "\(.app) \((.tokens_in + .tokens_out))"' \
  /var/log/gatekeeper/requests.jsonl \
  | awk '{sums[$1]+=$2} END {for (a in sums) print sums[a], a}' | sort -rn

# 429s by app (rate-limit hits)
jq -r 'select(.status==429) | .app' /var/log/gatekeeper/requests.jsonl | sort | uniq -c

# slow requests (>3s)
jq -r 'select(.latency_ms>3000) | "\(.latency_ms)ms \(.app) \(.path)"' \
  /var/log/gatekeeper/requests.jsonl

# failed backends
jq -r 'select(.status>=500) | "\(.status) \(.backend) \(.error)"' \
  /var/log/gatekeeper/requests.jsonl | sort | uniq -c
```

When we plug in monitoring later, this same file gets shipped to Loki/Promtail and the queries become Grafana panels.

## Health checks

The ALB target group health-checks `GET /healthz`. The response tells you which backend is up:

```json
{"status":"ok","backends":{"ollama":"ok","classifier":"unreachable: ConnectError"}}
```

`status: ok` means at least one backend is reachable. `degraded` means both are down. The ALB only cares that 200 is returned. If you want it to fail the target when backends are down, change the check path to `/v1/me` (with no auth this returns 401, still a 4xx so target is healthy).

## Backups

Only the keys file matters. The audit log is append-only; back it up if you want history, but losing it doesn't break anything.

```bash
# nightly cron
0 2 * * * cp /etc/gatekeeper/keys.yaml /etc/gatekeeper/keys.yaml.$(date +%Y%m%d) && \
          find /etc/gatekeeper/ -name "keys.yaml.*" -mtime +30 -delete
```

For real production: sync `/etc/gatekeeper/keys.yaml` to S3 with versioning enabled.

## Rotating all keys

Every 90 days, ideally:

```bash
for app in $(gatekeeper-keys list | awk 'NR>2 && $1!~/^$/ {print $1}'); do
  gatekeeper-keys rotate "$app" | tee -a /tmp/rotation-$(date +%F).log
done
```

Distribute the new keys to each app's owner, then delete the log file once confirmed.

## Common issues

| Symptom                                | Cause                                        | Fix                                                              |
|----------------------------------------|----------------------------------------------|------------------------------------------------------------------|
| 502 on `/v1/generate`                  | Ollama down or model not pulled              | `systemctl status ollama; ollama list`                           |
| 401 with a freshly-issued key          | Key not yet picked up; file mtime didn't change | `touch /etc/gatekeeper/keys.yaml`                                |
| 429 spam from one app                  | They're calling too fast                     | bump their `rate_limit_per_min` with `gatekeeper-keys rotate --rate` (or have them back off) |
| Logs not being written                 | Directory permissions                        | `chown gatekeeper /var/log/gatekeeper`                                 |
| Gateway won't start in Docker          | Port 8080 already in use                     | `ss -tln \| grep 8080`; change `GATEKEEPER_PORT`                    |
| Ollama hit by an external IP somehow   | It's bound to all interfaces                 | `OLLAMA_HOST=127.0.0.1 systemctl restart ollama`                 |

## Things to harden later

These are deliberately not in v0.1.0:
- Move keys from YAML to AWS Secrets Manager (one secret per app)
- Replace JSONL log with Postgres or ship to Loki for queryable history
- Prometheus metrics endpoint (`/metrics`) for per-app counters and histograms
- Per-app daily token budgets (not just RPM)
- Streaming responses for `/v1/generate`
- Multiple gateway replicas behind the ALB (rate limit moves to Redis)
- mTLS between ALB and gateway (currently only ALB→gateway is plain HTTP)
