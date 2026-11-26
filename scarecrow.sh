#!/usr/bin/env bash
# scarecrow.sh — ONE script to set up and run the scarecrow-balancer.
# Owner goal (2026-11-26): "it runs and sets up via one script."
#
# Usage:
#   ./scarecrow.sh              # setup (if needed) + start + wait healthy
#   ./scarecrow.sh setup        # first-time setup only
#   ./scarecrow.sh run          # start (docker compose if available, else bare python)
#   ./scarecrow.sh stop         # stop
#   ./scarecrow.sh restart      # stop + run
#   ./scarecrow.sh status       # health + ladder + GUI URL
#   ./scarecrow.sh logs         # tail logs
#
# Everything lives beside this script: source/, config/lanes.json, static/
# (the control GUI), deploy/docker-compose.yml, .env (keys; gitignored).

set -u
cd "$(dirname "$0")"

PORT="${FORK_BALANCER_PORT:-8486}"
GUI_URL="http://127.0.0.1:${PORT}/admin"
HEALTH_URL="http://127.0.0.1:${PORT}/health"
COMPOSE="deploy/docker-compose.yml"

have() { command -v "$1" >/dev/null 2>&1; }

need_setup() {
  [ ! -f .env ] && return 0
  return 1
}

do_setup() {
  echo "[scarecrow] setup"
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "[scarecrow] created .env — EDIT IT: set FORK_BALANCER_TOKEN and the API keys."
    echo "[scarecrow] (the live values exist in the sunny-scarecrow container env on Lappy;"
    echo "[scarecrow]   sudo docker inspect sunny-scarecrow --format '{{range .Config.Env}}{{println .}}{{end}}')"
  else
    echo "[scarecrow] .env present"
  fi
  [ -f config/lanes.json ] && echo "[scarecrow] lanes registry present ($(grep -c '"name"' config/lanes.json) lanes)" \
    || echo "[scarecrow] WARNING: config/lanes.json missing"
  [ -f source/balancer.py ] && echo "[scarecrow] balancer source present" \
    || { echo "[scarecrow] FATAL: source/balancer.py missing"; exit 1; }
  [ -f static/admin.html ] && echo "[scarecrow] control GUI present" \
    || echo "[scarecrow] control GUI missing (GUI will 404 until static/admin.html exists)"
}

start_docker() {
  have docker && have docker-compose || have "docker compose" || return 1
  docker compose -f "$COMPOSE" --env-file .env up -d
}

start_python() {
  echo "[scarecrow] starting bare python (no docker) on port ${PORT}"
  set -a; . ./.env; set +a
  export FORK_LANES_PATH="$(pwd)/config/lanes.json"
  nohup python3 source/balancer.py >> scarecrow_run.log 2>&1 &
  echo $! > scarecrow.pid
}

wait_healthy() {
  for _ in $(seq 1 30); do
    if curl -sf -m 3 "$HEALTH_URL" >/dev/null 2>&1; then
      echo "[scarecrow] healthy"
      echo "[scarecrow] GUI:  ${GUI_URL}"
      echo "[scarecrow] API:  http://127.0.0.1:${PORT}/v1/chat/completions  (model = lane name to pin)"
      return 0
    fi
    sleep 1
  done
  echo "[scarecrow] not healthy after 30s — check: ./scarecrow.sh logs"
  return 1
}

case "${1:-run}" in
  setup)
    do_setup
    ;;
  run)
    need_setup && do_setup
    if have docker && docker compose version >/dev/null 2>&1; then
      echo "[scarecrow] starting via docker compose"
      start_docker || { echo "[scarecrow] compose failed — falling back to bare python"; start_python; }
    else
      start_python
    fi
    wait_healthy
    ;;
  stop)
    if [ -f scarecrow.pid ]; then kill "$(cat scarecrow.pid)" 2>/dev/null; rm -f scarecrow.pid; fi
    have docker && docker compose -f "$COMPOSE" down 2>/dev/null
    echo "[scarecrow] stopped"
    ;;
  restart)
    "$0" stop; sleep 2; "$0" run
    ;;
  status)
    curl -sf -m 3 "$HEALTH_URL" || echo "[scarecrow] not running (or unreachable)"
    ;;
  logs)
    if [ -f scarecrow_run.log ]; then tail -n 50 -f scarecrow_run.log
    else have docker && docker compose -f "$COMPOSE" logs --tail 50 -f; fi
    ;;
  *)
    echo "usage: $0 [setup|run|stop|restart|status|logs]"
    exit 1
    ;;
esac
