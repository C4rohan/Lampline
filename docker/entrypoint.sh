#!/bin/sh
# Start the engine in the container: make its folders, give a first run an agent to talk to.
set -eu
umask 077
for d in /data /agents; do
  if ! [ -w "$d" ]; then
    echo "error: $d isn't writable by user $(id -u); run scripts/docker-up.sh, or set VPE_UID/VPE_GID in .env to the owner of ./.vpe-data and ./agents" >&2
    exit 2
  fi
done
mkdir -p /data/config /data/library /data/cache
if [ -e /data/config/settings.json ] && ! [ -r /data/config/settings.json ]; then
  echo "error: /data/config/settings.json belongs to another user; set VPE_UID/VPE_GID in .env to its owner" >&2
  exit 2
fi
if [ -z "$(ls -A /agents 2>/dev/null)" ]; then
  echo "no agents in /agents yet: installing the front-desk template"
  vk templates install front-desk --dir /agents/front-desk >/dev/null
fi
if [ -z "${VPE_API_KEY:-}" ]; then
  echo "error: set VPE_DOCKER_API_KEY in .env (scripts/docker-up.sh makes one); the engine needs a key in a container" >&2
  exit 2
fi
exec vpengine "$@"
