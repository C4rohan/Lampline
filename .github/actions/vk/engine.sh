#!/usr/bin/env bash
# The vk action's "start an engine" step: the engine from this action's own copy of the repo,
# on 127.0.0.1 only, with a fresh API key, and the model from INPUT_MODEL (provider:model).
# It ends with one real request to the model, so a wrong key or model name fails here, clearly,
# and not later as a test that "didn't reach" a step.
set -euo pipefail

case "$(printf '%s' "${INPUT_START_ENGINE:-false}" | tr '[:upper:]' '[:lower:]')" in
  false) exit 0 ;;
  true) ;;
  *) echo "::error::input start-engine is true or false; got '${INPUT_START_ENGINE}'"; exit 2 ;;
esac

root="$(cd "$(dirname "$0")/../../.." && pwd)"
py="${PY:-python3}"
vpengine="${VPENGINE:-vpengine}"
tmp="${RUNNER_TEMP:-$(mktemp -d)}"
port=7860

model="${INPUT_MODEL:-}"
if [ -z "$model" ]; then
  echo "::error::start-engine needs a model, like anthropic:claude-haiku-5-5 (with ANTHROPIC_API_KEY in env) or ollama:qwen3:4b (free, but slow on a standard runner)"
  exit 2
fi
provider="${model%%:*}"
name="${model#*:}"
[ "$provider" != "$model" ] && [ -n "$name" ] || { echo "::error::model is provider:model, like ollama:qwen3:4b; got '$model'"; exit 2; }

# A pull request from a fork gets none of the repository's secrets: skip the tests with a
# warning (lint still runs) instead of failing every outside contributor's pull request.
fork_pr() {
  case "${GITHUB_EVENT_NAME:-}" in pull_request|pull_request_target) ;; *) return 1 ;; esac
  [ -f "${GITHUB_EVENT_PATH:-}" ] || return 1
  "$py" - "$GITHUB_EVENT_PATH" "${GITHUB_REPOSITORY:-}" <<'PY'
import json, sys
try:
    event = json.load(open(sys.argv[1]))
    head = ((event.get("pull_request") or {}).get("head") or {}).get("repo") or {}
except (OSError, ValueError, AttributeError):
    sys.exit(1)
sys.exit(0 if head.get("full_name") and head["full_name"] != sys.argv[2] else 1)
PY
}
need_key() {  # need_key VARIABLE
  [ -n "${!1:-}" ] && return 0
  if fork_pr; then
    echo "::warning::model $model needs $1, which pull requests from forks don't get; the tests are skipped"
    echo "LAMPLINE_ACTION_SKIP_TESTS=a pull request from a fork has no $1" >> "${GITHUB_ENV:-/dev/null}"
    exit 0
  fi
  echo "::error::model $model needs $1 in env"
  exit 2
}

case "$provider" in
  ollama)
    # The same Ollama image docker-compose.yml runs.
    image="$(grep -m1 -oE 'ollama/ollama:[A-Za-z0-9._-]+' "$root/docker-compose.yml")"
    echo "Starting $image and pulling $name (a few minutes the first time)"
    docker run -d --name lampline-ollama -p 127.0.0.1:11434:11434 "$image" >/dev/null
    for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
    if ! docker exec lampline-ollama ollama pull "$name" > "$tmp/lampline-ollama-pull.log" 2>&1; then
      echo "::error::couldn't pull $name:"; tr '\r' '\n' < "$tmp/lampline-ollama-pull.log" | tail -5; exit 1
    fi
    export OLLAMA_BASE_URL=http://127.0.0.1:11434/v1 ;;
  anthropic) need_key ANTHROPIC_API_KEY ;;
  openai)    need_key OPENAI_API_KEY ;;
  google)    need_key GEMINI_API_KEY ;;
  *) echo "::error::model provider is ollama, anthropic, openai or google; got '$provider'"; exit 2 ;;
esac

key="$("$py" -c 'import secrets; print(secrets.token_urlsafe(24))')"
echo "::add-mask::$key"
mkdir -p "$tmp/lampline-agents" "$tmp/lampline-data"
# Its own settings file and data folder: nothing saved on the machine (a self-hosted runner's
# ~/.config/vpengine) changes which model or keys the tests use.
VPE_API_KEY="$key" VPE_LLM="$provider" VPE_LLM_MODEL="$name" VPE_DATA_DIR="$tmp/lampline-data" \
VPE_SETTINGS_FILE="$tmp/lampline-settings.json" \
  nohup "$vpengine" --agents "$tmp/lampline-agents" --host 127.0.0.1 --port "$port" > "$tmp/lampline-engine.log" 2>&1 &
engine_pid=$!
fail() { echo "::error::$*"; kill "$engine_pid" 2>/dev/null || true; exit 1; }

up=""
for _ in $(seq 1 90); do
  curl -fsS -H "X-API-Key: $key" "http://127.0.0.1:$port/v1/health" >/dev/null 2>&1 && { up=1; break; }
  sleep 1
done
if [ -z "$up" ]; then
  cat "$tmp/lampline-engine.log"; fail "the engine didn't start; its log is above"
fi

# One small real request: the engine's model playing a caller who says hello.
wait_s=120; [ "$provider" = ollama ] && wait_s=600
status="$(curl -sS -o "$tmp/lampline-model-check.json" -w '%{http_code}' --max-time "$wait_s" \
  -H "X-API-Key: $key" -H 'Content-Type: application/json' \
  -d '{"persona": "A caller checking that the line works.", "goal": "say hello", "transcript": []}' \
  "http://127.0.0.1:$port/v1/tests/caller" || echo 000)"
if [ "$status" != 200 ]; then
  detail="$("$py" -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("detail", ""))
except Exception: print("")' "$tmp/lampline-model-check.json" 2>/dev/null || true)"
  fail "the engine started, but its model ($model) didn't answer (HTTP $status): ${detail:-no reply}. Check the key and the model name."
fi
echo "Engine ready on 127.0.0.1:$port with $model"
{ echo "VK_ENGINE_URL=http://127.0.0.1:$port"; echo "VK_ENGINE_KEY=$key"; } >> "${GITHUB_ENV:-/dev/null}"
