#!/usr/bin/env bash
# Run the voice engine in Docker, like `docker compose up`, with the setup done for you:
#   scripts/docker-up.sh            build if needed, start, print the address to open
#   scripts/docker-up.sh --free     no API keys: a model (Ollama) and speech (Moonshine, Kokoro) on this computer
#   scripts/docker-up.sh --tunnel   also start the Cloudflare named tunnel (phone numbers)
#   scripts/docker-up.sh --down     stop it (your agents and data stay)
#   scripts/docker-up.sh --logs     follow the engine's log
# Agents live in ./agents and everything else in ./.vpe-data, both on this computer.
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE=()
FREE=0
TUNNEL=0
for arg in "$@"; do
  case "$arg" in
    --down) docker compose --profile tunnel --profile free down; exit 0 ;;
    --logs) exec docker compose logs -f engine ;;
    --tunnel) TUNNEL=1; PROFILE+=(--profile tunnel) ;;
    --free) FREE=1; PROFILE+=(--profile free) ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $arg (try --help)" >&2; exit 2 ;;
  esac
done

if ! docker compose version >/dev/null 2>&1; then
  echo "error: needs Docker with Compose v2 (Docker Desktop, or docker-ce with the compose plugin)" >&2
  exit 1
fi

# The engine's API key lives in .env (never in the image). Made once, kept private.
touch .env
chmod 600 .env
if ! grep -qE '^VPE_DOCKER_API_KEY=.+' .env; then
  key="vpe_$(od -An -tx1 -N24 /dev/urandom | tr -d ' \n')"
  printf '\n# API key for the engine in Docker (added by scripts/docker-up.sh)\nVPE_DOCKER_API_KEY=%s\n' "$key" >> .env
  echo "created VPE_DOCKER_API_KEY in .env"
fi
KEY="$(grep -E '^VPE_DOCKER_API_KEY=' .env | tail -1 | cut -d= -f2-)"
PORT="$(grep -E '^VPE_DOCKER_PORT=' .env | tail -1 | cut -d= -f2- || true)"
PORT="${PORT:-7860}"
if [[ "$TUNNEL" == 1 ]] && ! grep -qE '^CLOUDFLARE_TUNNEL_TOKEN=.+' .env; then
  echo "error: --tunnel needs CLOUDFLARE_TUNNEL_TOKEN and VPE_DOCKER_PUBLIC_URL in .env" >&2
  exit 1
fi

# Your user owns what the engine writes, so you can edit agents and back up data without sudo.
# Saved in .env too, so a plain `docker compose up` later runs as the same user.
mkdir -p agents .vpe-data
if ! grep -qE '^VPE_UID=' .env; then
  printf 'VPE_UID=%s\nVPE_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
fi

# --free: everything on this computer, no accounts. Only fills in what .env doesn't already set,
# and the Integrations page can switch to any provider later.
if [[ "$FREE" == 1 ]]; then
  added=0
  for pair in VPE_LLM=ollama VPE_LLM_MODEL=qwen3:4b VPE_STT=moonshine VPE_TTS=kokoro VPE_AUTO_DOWNLOAD=on \
              OLLAMA_BASE_URL=http://ollama:11434/v1; do
    if ! grep -qE "^${pair%%=*}=" .env; then
      [[ "$added" == 0 ]] && printf '\n# No API keys: a model and speech on this computer (added by scripts/docker-up.sh --free)\n' >> .env
      echo "$pair" >> .env
      added=1
    fi
  done
fi

docker compose ${PROFILE[@]+"${PROFILE[@]}"} up -d --build

# --free: wait for the model and the speech models to download (first run only), showing progress.
free_wait() {
  local model; model="$(grep -E '^VPE_LLM_MODEL=' .env | tail -1 | cut -d= -f2-)"
  echo "first run: downloading the model (${model:-qwen3:4b}, ~2.5 GB) and speech models (~500 MB). This takes a few minutes."
  for _ in $(seq 1 720); do
    local llm=no speech=no
    docker compose exec -T ollama ollama list 2>/dev/null | grep -q "^${model:-qwen3:4b}" && llm=yes
    if curl -fsS -m 3 -H "X-API-Key: $KEY" "http://127.0.0.1:$PORT/v1/moonshine" 2>/dev/null | grep -q '"ready": *true' \
       && curl -fsS -m 3 -H "X-API-Key: $KEY" "http://127.0.0.1:$PORT/v1/builtin-speech" 2>/dev/null | grep -q '"kokoro_ready": *true'; then
      speech=yes
    fi
    printf '\r  model: %-3s  speech: %-3s' "$llm" "$speech"
    if [[ "$llm" == yes && "$speech" == yes ]]; then echo; return 0; fi
    sleep 5
  done
  echo
  echo "still downloading; the Integrations page shows progress. Calls work once it's done."
}

printf 'waiting for the engine'
for _ in $(seq 1 60); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/v1/health" >/dev/null 2>&1; then
    echo
    if [[ "$FREE" == 1 ]]; then
      free_wait
    fi
    echo "ready. Open this once in your browser (it signs the page in with your key):"
    echo "  http://localhost:$PORT/#/?key=$KEY"
    echo "after that, http://localhost:$PORT is enough. Stop with scripts/docker-up.sh --down"
    exit 0
  fi
  printf '.'
  sleep 2
done
echo
echo "the engine didn't come up; see: docker compose logs engine" >&2
exit 1
