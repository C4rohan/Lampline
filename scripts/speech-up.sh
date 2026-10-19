#!/usr/bin/env bash
# Start free, local speech for the engine: Whisper (listening) and Kokoro (speaking).
#
#   scripts/speech-up.sh          # start (first run downloads the images and the Whisper model)
#   scripts/speech-up.sh --down   # stop
#
# Then in the engine's Integrations page pick "Whisper on this computer" and
# "Kokoro on this computer". Audio never leaves this machine. Needs Docker
# (OrbStack or Docker Desktop on a Mac).
set -euo pipefail
cd "$(dirname "$0")/.."

FILE=local/docker-compose.speech.yml
if ! command -v docker >/dev/null 2>&1; then
  echo "error: needs Docker (macOS: install OrbStack and open it once)" >&2
  exit 1
fi
if [[ "${1:-}" == "--down" ]]; then
  docker compose -f "$FILE" down
  exit 0
fi

MODEL="${VPE_WHISPER_MODEL:-Systran/faster-whisper-small}"
docker compose -f "$FILE" up -d

echo "waiting for the speech servers..."
for _ in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:8001/v1/models >/dev/null 2>&1 && curl -fsS http://127.0.0.1:8880/v1/audio/voices >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
echo "downloading the Whisper model $MODEL (once)..."
curl -fsS -X POST "http://127.0.0.1:8001/v1/models/$MODEL" >/dev/null || echo "  (download request failed; it will download on first use)"

echo
echo "listening: http://localhost:8001/v1  (Whisper, $MODEL)"
echo "speaking:  http://localhost:8880/v1  (Kokoro)"
echo "In the engine: Integrations > Listening: Whisper on this computer, Speaking: Kokoro on this computer"
