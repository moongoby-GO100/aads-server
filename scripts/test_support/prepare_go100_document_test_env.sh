#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
python3 -m venv "$root/.venv"
"$root/.venv/bin/python" -m pip install --no-index --find-links "${GO100_WHEEL_CACHE:-/tmp/go100-document-testdeps-20261118}" -r "$root/scripts/test_support/go100_document_test_requirements.txt"
