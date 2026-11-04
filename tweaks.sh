#!/bin/sh
# Shortcut to the free-my-mac tweaks catalog (debloat and optimization).
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$SCRIPT_DIR/free-my-mac.sh" --tweaks "$@"
