#!/usr/bin/env bash
# Comprueba que el PID del archivo corresponde al bot de este directorio.
responder_pid() {
  local pid arg executable
  pid=$(jq -r '.pid // empty' responder.lock 2>/dev/null) || return 1
  [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] || return 1
  [[ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" == "$PWD" ]] || return 1
  {
    IFS= read -r -d '' executable || return 1
    [[ "${executable##*/}" == node || "${executable##*/}" == nodejs ]] || return 1
    while IFS= read -r -d '' arg; do
      if [[ "$arg" == responder.js || "$arg" == ./responder.js || "$arg" == "$PWD/responder.js" ]]; then
        printf '%s\n' "$pid"
        return 0
      fi
    done
  } < "/proc/$pid/cmdline"
  return 1
}
