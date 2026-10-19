#!/usr/bin/env bash
# Run the voice engine in Docker, like `docker compose up`, with the setup done for you:
#   scripts/docker-up.sh            build if needed, start, print the address to open
#   scripts/docker-up.sh --tunnel   also start the Cloudflare named tunnel (phone numbers)
#   scripts/docker-up.sh --down     stop it (your agents and data stay)
#   scripts/docker-up.sh --logs     follow the engine's log
# Agents live in ./agents and everything else in ./.vpe-data, both on this computer.
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE=()
case "${1:-}" in
  --down) docker compose --profile tunnel down; exit 0 ;;
  --logs) exec docker compose logs -f engine ;;
  --tunnel) PROFILE=(--profile tunnel) ;;
  -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown option $1 (try --help)" >&2; exit 2 ;;
esac

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
if [[ ${#PROFILE[@]} -gt 0 ]] && ! grep -qE '^CLOUDFLARE_TUNNEL_TOKEN=.+' .env; then
  echo "error: --tunnel needs CLOUDFLARE_TUNNEL_TOKEN and VPE_DOCKER_PUBLIC_URL in .env" >&2
  exit 1
fi

# Your user owns what the engine writes, so you can edit agents and back up data without sudo.
# Saved in .env too, so a plain `docker compose up` later runs as the same user.
mkdir -p agents .vpe-data
if ! grep -qE '^VPE_UID=' .env; then
  printf 'VPE_UID=%s\nVPE_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
fi

docker compose "${PROFILE[@]}" up -d --build

printf 'waiting for the engine'
for _ in $(seq 1 60); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/v1/health" >/dev/null 2>&1; then
    echo
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
