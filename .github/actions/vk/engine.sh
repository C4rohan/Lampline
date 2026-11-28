#!/usr/bin/env bash
# The vk action's "start an engine" step: the engine from this action's own copy of the repo,
# on 127.0.0.1 only, with a fresh API key, and the model from INPUT_MODEL (provider:model).
set -euo pipefail

root="$(cd "$(dirname "$0")/../../.." && pwd)"
model="${INPUT_MODEL:-}"
if [ -z "$model" ]; then
  echo "::error::start-engine needs a model, like anthropic:claude-haiku-5-5 (with ANTHROPIC_API_KEY in env) or ollama:qwen3:4b (free, but slow on a standard runner)"
  exit 2
fi
provider="${model%%:*}"
name="${model#*:}"
[ "$provider" != "$model" ] && [ -n "$name" ] || { echo "::error::model is provider:model, like ollama:qwen3:4b; got '$model'"; exit 2; }
tmp="${RUNNER_TEMP:-$(mktemp -d)}"
port=7860

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
  anthropic) [ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "::error::model $model needs ANTHROPIC_API_KEY in env"; exit 2; } ;;
  openai)    [ -n "${OPENAI_API_KEY:-}" ]    || { echo "::error::model $model needs OPENAI_API_KEY in env"; exit 2; } ;;
  google)    [ -n "${GEMINI_API_KEY:-}" ]    || { echo "::error::model $model needs GEMINI_API_KEY in env"; exit 2; } ;;
  *) echo "::error::model provider is ollama, anthropic, openai or google; got '$provider'"; exit 2 ;;
esac

key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
echo "::add-mask::$key"
mkdir -p "$tmp/lampline-agents" "$tmp/lampline-data"
VPE_API_KEY="$key" VPE_LLM="$provider" VPE_LLM_MODEL="$name" VPE_DATA_DIR="$tmp/lampline-data" \
  nohup vpengine --agents "$tmp/lampline-agents" --host 127.0.0.1 --port "$port" > "$tmp/lampline-engine.log" 2>&1 &

health=""
for _ in $(seq 1 90); do
  health="$(curl -fsS -H "X-API-Key: $key" "http://127.0.0.1:$port/v1/health" 2>/dev/null)" && break
  sleep 1
done
if [ -z "$health" ]; then
  echo "::error::the engine didn't start; its log:"; cat "$tmp/lampline-engine.log"; exit 1
fi
if ! printf '%s' "$health" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["config"]["llm_ready"] else 1)'; then
  echo "::error::the engine started but its model ($model) isn't ready; check the key and model name"; exit 1
fi
echo "Engine ready on 127.0.0.1:$port with $model"
{ echo "VK_ENGINE_URL=http://127.0.0.1:$port"; echo "VK_ENGINE_KEY=$key"; } >> "${GITHUB_ENV:-/dev/null}"
