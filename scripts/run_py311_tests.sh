#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON311_BIN:-/usr/local/bin/python3.11}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3.11 || true)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "python3.11 executable not found. Set PYTHON311_BIN to the interpreter path." >&2
  exit 127
fi

cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m pytest "$@"
