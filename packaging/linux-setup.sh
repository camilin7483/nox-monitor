#!/bin/sh
set -eu

case "$(uname -m)" in
    x86_64) ;;
    *) printf '%s\n' 'Esta edición requiere Linux x86_64.' >&2; exit 1 ;;
esac
umask 077
workdir=$(mktemp -d)
cleanup() { find "$workdir" -depth -delete; }
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
offset=$(awk '/^__NOX_PAYLOAD__$/ {print NR + 1; exit}' "$0")
tail -n "+$offset" "$0" | tar -xz -C "$workdir"
"$workdir/runtime/python/bin/python3" "$workdir/packaging/linux_install.py" "$@"
exit 0
__NOX_PAYLOAD__
