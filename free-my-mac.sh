#!/bin/sh
# Inicializador da auditoria/manutenção; alterações exigem seleção e confirmação.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/free_my_mac.py" "$@"
