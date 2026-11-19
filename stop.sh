#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ./lifecycle.sh
exec 9>.nox-lifecycle.lock
flock -x 9

if pid=$(responder_pid); then
  kill -TERM "$pid" 2>/dev/null || true
  for ((attempt=0; attempt<50; attempt++)); do
    if ! responder_pid >/dev/null; then
      printf 'Nox se detuvo.\n'
      exit 0
    fi
    sleep 0.2
  done
  printf 'Nox sigue activo tras solicitar la detención.\n' >&2
  exit 1
fi

printf 'Nox ya está detenido.\n'
