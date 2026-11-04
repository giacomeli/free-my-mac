#!/bin/sh
# Launcher. Uses the system Python (/usr/bin/python3), which no step removes.
set -eu
if [ "$(id -u)" -eq 0 ]; then
  echo "Run without sudo. Steps ask for the password only in the commands that need it." >&2
  exit 2
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 "$SCRIPT_DIR/free_my_mac.py" "$@"
