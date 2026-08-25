#!/usr/bin/env bash
set -euo pipefail

GATEWAY="${GATEWAY:-http://127.0.0.1:8080}"
KEY="${GATEKEEPER_KEY:-}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
hdr() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

hdr "health check (no auth)"
curl -fsS "$GATEWAY/healthz" | python3 -m json.tool

if [[ -z "$KEY" ]]; then
  red "set GATEKEEPER_KEY=<the key you issued> to run authenticated tests"
  exit 0
fi

hdr "who am I"
curl -fsS -H "Authorization: Bearer $KEY" "$GATEWAY/v1/me" | python3 -m json.tool

hdr "rejected without key"
code=$(curl -s -o /dev/null -w '%{http_code}' "$GATEWAY/v1/me")
if [[ "$code" == "401" ]]; then
  grn "ok: got 401 as expected"
else
  red "expected 401, got $code"
  exit 1
fi

hdr "rejected with bad key"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer sk-bogus" "$GATEWAY/v1/me")
if [[ "$code" == "401" ]]; then
  grn "ok: got 401 as expected"
else
  red "expected 401, got $code"
  exit 1
fi

hdr "generate (tiny prompt)"
curl -fsS -X POST "$GATEWAY/v1/generate" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5:9b","prompt":"In one sentence, what is solar soiling?","max_tokens":80}' \
  | python3 -m json.tool

grn "smoke test passed"
