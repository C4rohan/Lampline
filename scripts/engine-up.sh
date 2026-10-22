#!/usr/bin/env bash
# Run our voice engine (vpengine) natively, with the agent folders in ./agents.
#
#   scripts/engine-up.sh                 # http://localhost:7860
#   scripts/engine-up.sh --agents DIR    # another agents directory
#   scripts/engine-up.sh --port 7870
#   scripts/engine-up.sh --free          # no API keys: Ollama, Moonshine and Kokoro on this computer
#   scripts/engine-up.sh --tunnel        # also take phone calls (Twilio, Telnyx or jambonz)
#   scripts/engine-up.sh --no-reload     # don't restart by itself when the engine's code changes
#
# Reads model keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, ...),
# speech and phone keys (DEEPGRAM_API_KEY, FISH_API_KEY, TWILIO_AUTH_TOKEN, TELNYX_*,
# JAMBONZ_WEBHOOK_SECRET), BRAND_NAME and VPE_* from the environment or .env (only
# those names; the full list is below). Most people set these on the Integrations page instead.
# Runs natively rather than in Docker so browser calls (WebRTC) reach it
# directly. Listens on 127.0.0.1 only; --tunnel opens a Cloudflare quick
# tunnel so a phone provider can reach it, and then every API needs VPE_API_KEY.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=.venv-engine
AGENTS=agents
ARGS=()
TUNNEL=0
FREE=0
RELOAD=1
PORT="${VPE_PORT:-7860}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agents) AGENTS="$2"; shift 2 ;;
    --tunnel) TUNNEL=1; shift ;;
    --free) FREE=1; shift ;;
    --no-reload) RELOAD=0; shift ;;
    --port) PORT="$2"; ARGS+=("$1" "$2"); shift 2 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

# Python 3.11+ (Pipecat needs it).
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [[ -z "$PY" ]]; then
  echo "error: needs Python 3.11 or newer (macOS: brew install python@3.12)" >&2
  exit 1
fi

# Install or refresh the engine when its package files change.
STAMP="$VENV/.installed"
WANT=$("$PY" -c 'import hashlib; print(hashlib.sha256(b"".join(open(f, "rb").read() for f in ("engine/pyproject.toml", "cli/pyproject.toml"))).hexdigest()[:16])')
if [[ ! -x "$VENV/bin/vpengine" || "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]]; then
  echo "installing the engine into $VENV (first run takes a minute)..."
  # Start from an empty environment so packages we no longer need don't linger.
  "$PY" -m venv --clear "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -e cli -e engine
  # Moonshine (streaming local speech to text) where this computer can run it; fine without it.
  "$VENV/bin/python" -m pip install --quiet -e "engine[moonshine]" 2>/dev/null \
    || echo "note: Moonshine isn't available on this computer; Whisper and the cloud services still are"
  echo "$WANT" > "$STAMP"
fi

# Pick up keys and settings from .env without executing it (only these names).
KEYS="ANTHROPIC_API_KEY|OPENAI_API_KEY|GEMINI_API_KEY|GOOGLE_CLOUD_PROJECT|AZURE_OPENAI_API_KEY|AZURE_OPENAI_ENDPOINT"
KEYS+="|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_REGION|GROQ_API_KEY|MISTRAL_API_KEY|DEEPSEEK_API_KEY|XAI_API_KEY"
KEYS+="|TOGETHER_API_KEY|FIREWORKS_API_KEY|OPENROUTER_API_KEY|CEREBRAS_API_KEY|NVIDIA_API_KEY|DASHSCOPE_API_KEY"
KEYS+="|SAMBANOVA_API_KEY|NEBIUS_API_KEY|NOVITA_API_KEY|OLLAMA_BASE_URL"
KEYS+="|DEEPGRAM_API_KEY|FISH_API_KEY|TWILIO_AUTH_TOKEN|TELNYX_API_KEY|TELNYX_PUBLIC_KEY|JAMBONZ_WEBHOOK_SECRET|BRAND_NAME|VPE_[A-Z0-9_]+"
if [[ -f .env ]]; then
  while IFS= read -r line; do
    [[ "$line" =~ ^($KEYS)=(.*)$ ]] || continue
    name="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    [[ -n "${!name:-}" ]] || export "$name=$value"
  done < .env
fi

# First run: give the engine something to talk to.
if [[ ! -d "$AGENTS" ]] || [[ -z "$(ls -A "$AGENTS" 2>/dev/null)" ]]; then
  echo "no agents in $AGENTS yet: installing the front-desk template there"
  "$VENV/bin/vk" templates install front-desk --dir "$AGENTS/front-desk" >/dev/null
fi


# --free: everything on this computer, no accounts (values already in the environment or .env win).
if [[ "$FREE" == 1 ]]; then
  export VPE_LLM="${VPE_LLM:-ollama}" VPE_LLM_MODEL="${VPE_LLM_MODEL:-qwen3:4b}" VPE_TTS="${VPE_TTS:-kokoro}"
  export VPE_AUTO_DOWNLOAD="${VPE_AUTO_DOWNLOAD:-on}"
  if "$VENV/bin/python" -c 'import moonshine_voice' 2>/dev/null; then
    export VPE_STT="${VPE_STT:-moonshine}"
  else
    export VPE_STT="${VPE_STT:-whisper}"
  fi
  if ! command -v ollama >/dev/null 2>&1; then
    echo "--free needs Ollama for the model: install it from https://ollama.com (or brew install ollama), then run this again" >&2
    exit 1
  fi
  if ! curl -fsS -m 3 http://localhost:11434/api/version >/dev/null 2>&1; then
    echo "starting Ollama..."
    (ollama serve >"$VENV/ollama.log" 2>&1 &)
    for _ in $(seq 1 20); do curl -fsS -m 2 http://localhost:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
  fi
  echo "getting the model $VPE_LLM_MODEL (about 2.5 GB, first run only)..."
  ollama pull "$VPE_LLM_MODEL"
  echo "speech models (about 500 MB) download when the engine starts; the Integrations page shows progress"
fi

[[ "$RELOAD" == 1 ]] && ARGS+=(--reload)
export VPE_REPO_ROOT="$PWD"   # lets the Integrations page start local speech (local/docker-compose.speech.yml)

if [[ "$TUNNEL" != 1 ]]; then
  exec "$VENV/bin/vpengine" --agents "$AGENTS" ${ARGS[@]+"${ARGS[@]}"}
fi

# --- phone calls: a public https address for Twilio, Telnyx or jambonz --------
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "error: --tunnel needs cloudflared (macOS: brew install cloudflared)" >&2
  exit 1
fi
# The tunnel makes every endpoint reachable from the internet, so the APIs need a key
# while it's open. Kept as VPE_TUNNEL_API_KEY so runs without --tunnel stay keyless.
if [[ -z "${VPE_API_KEY:-}" && -z "${VPE_TUNNEL_API_KEY:-}" ]]; then
  VPE_TUNNEL_API_KEY="vpe_$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  printf '\n# API key while the engine is reachable through a tunnel (added by engine-up.sh --tunnel)\nVPE_TUNNEL_API_KEY=%s\n' \
    "$VPE_TUNNEL_API_KEY" >> .env
  chmod 600 .env
  echo "created VPE_TUNNEL_API_KEY in .env"
fi
export VPE_API_KEY="${VPE_API_KEY:-$VPE_TUNNEL_API_KEY}"
echo "while the tunnel is open, the call page asks for the VPE_TUNNEL_API_KEY from .env,"
echo "and vk test needs it as VK_ENGINE_KEY"

LOG="$VENV/tunnel.log"
cloudflared tunnel --no-autoupdate --url "http://localhost:$PORT" > "$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null || true' EXIT INT TERM
URL=""
for _ in $(seq 1 60); do
  URL=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [[ -n "$URL" ]] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "error: cloudflared stopped; see $LOG" >&2
    exit 1
  fi
  sleep 1
done
if [[ -z "$URL" ]]; then
  echo "error: no tunnel address after 60s; see $LOG" >&2
  exit 1
fi
export VPE_PUBLIC_URL="$URL"
echo
echo "Twilio: Phone Numbers > your number > Voice > \"A call comes in\": Webhook, HTTP POST"
echo "  $URL/v1/twilio/voice?agent=front-desk"
echo "Telnyx: TeXML Application > voice URL, method POST"
echo "  $URL/v1/telnyx/voice?agent=front-desk"
echo "  (these change each time you start the tunnel; the Phone numbers page shows them too;"
echo "   replace front-desk with any agent folder. Set the Twilio token or Telnyx key on that page.)"
echo
"$VENV/bin/vpengine" --agents "$AGENTS" ${ARGS[@]+"${ARGS[@]}"}
