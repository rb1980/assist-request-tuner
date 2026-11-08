#!/usr/bin/env bash
# A throwaway Home Assistant with a real Supervisor in Docker (OrbStack or Docker Desktop), to test installing the App.
# Uses Home Assistant's own App-development container (privileged, Docker inside Docker) on the STABLE channel.
#
#   scripts/dev-ha.sh up        start it (first run: ~10 min of image pulls), install Assist Request Tuner as a local App
#   scripts/dev-ha.sh rebuild   copy the current code into the local App and rebuild it (~20 s)
#   scripts/dev-ha.sh logs      the App's log
#   scripts/dev-ha.sh assistant <api_url> <model>
#                               an LLM voice assistant to test the whole workflow with: the Local OpenAI LLM integration
#                               on any OpenAI-compatible API (LM Studio, llama.cpp, OpenRouter...), demo lights in rooms,
#                               and a "Dev Assist" pipeline that tries Home Assistant's own intents first
#   scripts/dev-ha.sh say "<text>" ...   send requests through that pipeline (prints who handled each and the reply)
#   scripts/dev-ha.sh stop      stop it (keeps its volumes; `up` starts it again)
#
# UI: http://localhost:8125 (proxied to its port 80; HA 2026.11 redirects 8123 to 80). `up` onboards a "dev"
# owner; its password is in .data/dev-ha-login.txt. Nothing is published beyond 127.0.0.1. Deleting it for good is up to you:
#   docker rm -f ar-tuner-ha ar-tuner-ha-proxy && docker volume rm ar-tuner-ha-docker ar-tuner-ha-supervisor
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=ar-tuner-ha
IMAGE=ghcr.io/home-assistant/devcontainer:3-addons
CLI=(docker exec "$NAME" docker exec hassio_cli ha)
PY=.venv/bin/python   # the dev venv (README, Development): httpx and websockets

stage() {
  # The Supervisor builds a local App from its own folder with the whole Dockerfile (no build target), so stage a
  # copy: the code, the App metadata without `image:` (or it would pull the published image), and the Dockerfile
  # cut after the `app` stage. Local Apps live in apps/local (addons/local before Supervisor 2026.10).
  local dir tmp
  dir=$(docker exec "$NAME" sh -c 'if [ -d /mnt/supervisor/apps ] || [ ! -d /mnt/supervisor/addons ]; then echo apps; else echo addons; fi')
  dir=/mnt/supervisor/$dir/local/ar_tuner
  docker exec "$NAME" sh -c "mkdir -p $dir && find $dir -mindepth 1 -delete"
  tmp=$(mktemp -d)
  cp -R app requirements.txt ha-app/DOCS.md ha-app/README.md ha-app/CHANGELOG.md ha-app/translations "$tmp/"
  find "$tmp" -name __pycache__ -prune -exec rm -rf {} +
  grep -v '^image:' ha-app/config.yaml > "$tmp/config.yaml"
  sed '/^FROM base AS standalone/,$d' Dockerfile > "$tmp/Dockerfile"
  docker cp "$tmp/." "$NAME:$dir/"
  rm -rf "$tmp"
}

supervisor() {
  # Workarounds for the dev container's script (checked with Supervisor 2026.10.5 / Core 2026.11.0):
  #  - it follows the dev channel (nightly builds); use stable, which is what users run;
  #  - newer Supervisors talk to Core over a socket in /run/supervisor, mounted at /run/os, which the script doesn't mount;
  #  - it calls `stty`, which fails without a terminal (hence `exec -t`).
  docker exec "$NAME" bash -c '
    sed -i "s#version.home-assistant.io/dev.json#version.home-assistant.io/stable.json#" /etc/supervisor_scripts/common
    grep -q "/run/supervisor:/run/os" /usr/bin/supervisor_run || \
      sed -i "s#-v /run/udev:/run/udev:ro \\\\#-v /run/udev:/run/udev:ro \\\\\n        -v /run/supervisor:/run/os:rw \\\\#" /usr/bin/supervisor_run
    mkdir -p /run/supervisor/core /run/supervisor/supervisor/core'
  # Its own exec: inside the bash -c above, pkill would match (and kill) that shell, whose text names the script.
  docker exec "$NAME" pkill -f supervisor_run || true
  docker exec "$NAME" sh -c 'docker rm -f hassio_supervisor >/dev/null 2>&1 || true'
  docker exec -dt "$NAME" bash -c 'supervisor_run > /var/log/supervisor_run.log 2>&1'
  echo -n "Waiting for the Supervisor"
  until "${CLI[@]}" supervisor info >/dev/null 2>&1; do echo -n .; sleep 5; done; echo
  "${CLI[@]}" supervisor options --channel stable >/dev/null
  # Docker inside Docker has no protected gateway, so the Supervisor calls the system unhealthy and blocks updates
  # and installs; harmless here.
  "${CLI[@]}" jobs options --ignore-conditions healthy >/dev/null
}


update_core() {
  # First boot can still be installing Core automatically. Retry only that specific job conflict, for at most
  # five minutes, and recheck the version because the automatic installation may already have reached our target.
  local wanted=$1 have output result attempt
  for attempt in $(seq 30); do
    have=$("${CLI[@]}" core info --raw-json | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["version"])')
    [ "$have" = "$wanted" ] && return 0
    if output=$("${CLI[@]}" core update --version "$wanted" 2>&1); then
      printf '%s\n' "$output"
      return 0
    else
      result=$?
    fi
    if [[ "$output" != *"Another job is running for home_assistant_core"* && "$output" != *"Another job is running for job group home_assistant_core"* ]] || [ "$attempt" -eq 30 ]; then
      printf '%s\n' "$output" >&2
      return "$result"
    fi
    echo "Core installation is still running; retrying in 10 seconds ($attempt/30)."
    sleep 10
  done
}

case "${1:-}" in
  up)
    if ! docker inspect "$NAME" >/dev/null 2>&1; then
      docker run -d --name "$NAME" --hostname "$NAME" --privileged \
        -v ar-tuner-ha-docker:/var/lib/docker -v ar-tuner-ha-supervisor:/mnt/supervisor \
        "$IMAGE" sleep infinity >/dev/null
    else
      docker start "$NAME" >/dev/null
    fi
    docker inspect ar-tuner-ha-proxy >/dev/null 2>&1 || docker run -d --name ar-tuner-ha-proxy -p 127.0.0.1:8125:8125 \
      alpine/socat tcp-listen:8125,fork,reuseaddr "tcp:$NAME.orb.local:80" >/dev/null
    docker start ar-tuner-ha-proxy >/dev/null
    stage   # before the Supervisor starts: it reads local Apps only at start-up (`store reload` doesn't rescan them)
    supervisor
    want=$(curl -s https://version.home-assistant.io/stable.json | python3 -c 'import sys,json; print(json.load(sys.stdin)["homeassistant"]["default"])')
    have=$("${CLI[@]}" core info --raw-json | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["version"])')
    [ "$have" = "$want" ] || { echo "Core $have -> $want"; update_core "$want"; }
    if "${CLI[@]}" apps info local_assist_ar_tuner --raw-json | grep -qE '"version": ?null'; then
      "${CLI[@]}" apps install local_assist_ar_tuner
    fi
    # Just after a Supervisor (re)start it refuses App starts for a few seconds while it finishes setting up.
    for try in $(seq 12); do "${CLI[@]}" apps start local_assist_ar_tuner >/dev/null 2>&1 && break; sleep 5; done
    "${CLI[@]}" apps info local_assist_ar_tuner | grep -q '^state: started' || "${CLI[@]}" apps start local_assist_ar_tuner
    "$PY" scripts/dev_ha.py onboard
    echo "Dev Home Assistant: http://localhost:8125 (Settings > Apps > Assist Request Tuner)"
    ;;
  rebuild)
    stage
    "${CLI[@]}" store reload >/dev/null
    "${CLI[@]}" apps rebuild local_assist_ar_tuner
    ;;
  logs) "${CLI[@]}" apps logs local_assist_ar_tuner ;;
  assistant) shift; "$PY" scripts/dev_ha.py assistant "$@" ;;
  say) shift; "$PY" scripts/dev_ha.py say "$@" ;;
  stop) docker stop ar-tuner-ha-proxy "$NAME" >/dev/null && echo stopped ;;
  *) sed -n '2,19p' "$0"; exit 1 ;;
esac
