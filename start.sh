#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ./lifecycle.sh
exec 9>.nox-lifecycle.lock
flock -x 9

if pid=$(responder_pid); then
  printf 'Nox ya está activo (PID %s).\n' "$pid"
  exit 0
fi

node --check responder.js >/dev/null
printf '===== inicio %s =====\n' "$(date -Is)" >> responder.log
export NO_PROXY=127.0.0.1,localhost
export no_proxy="$NO_PROXY"
setsid node responder.js </dev/null >/dev/null 2>>responder.log 9>&- &
child=$!
for ((attempt=0; attempt<50; attempt++)); do
  if pid=$(responder_pid); then
    printf 'Nox inició (PID %s).\n' "$pid"
    exit 0
  fi
  if ! kill -0 "$child" 2>/dev/null; then
    break
  fi
  sleep 0.2
done
kill "$child" 2>/dev/null || true
printf 'Nox no inició. Revisa responder.log para ver el error.\n' >&2
exit 1
