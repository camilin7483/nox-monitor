#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ./lifecycle.sh

if pid=$(responder_pid); then
  printf 'Nox está activo (PID %s).\n' "$pid"
else
  printf 'Nox está detenido.\n'
fi

if [[ -f responder.log ]]; then
  tail -n 12 responder.log
fi
