#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-python3}"
PIPTOOLS_CMD=()

if "$PYTHON_BIN" -m piptools compile --help >/dev/null 2>&1; then
    PIPTOOLS_CMD=("$PYTHON_BIN" -m piptools compile)
elif command -v pip-compile >/dev/null 2>&1; then
    PIPTOOLS_CMD=(pip-compile)
else
    echo "pip-tools is required. Use a temporary venv if the host Python is externally managed." >&2
    echo "Example: python3 -m venv /tmp/aads-piptools && /tmp/aads-piptools/bin/pip install pip-tools" >&2
    exit 1
fi

COMMON_ARGS=(
    --resolver=backtracking
    --strip-extras
    --no-emit-index-url
    --quiet
)

"${PIPTOOLS_CMD[@]}" "${COMMON_ARGS[@]}" pyproject.toml -o requirements.runtime.lock
"${PIPTOOLS_CMD[@]}" "${COMMON_ARGS[@]}" --extra dev pyproject.toml -o requirements.dev.lock
"${PIPTOOLS_CMD[@]}" "${COMMON_ARGS[@]}" requirements.visual.in -o requirements.visual.lock

echo "Updated requirements.runtime.lock, requirements.dev.lock, requirements.visual.lock"
