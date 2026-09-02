#!/usr/bin/env bash
# Refresh the public LLM model report for CEO/admin browser access.
set -euo pipefail

SERVER_ROOT="/root/aads/aads-server"
EXPORT_PATH="/var/www/certbot/exports/llm-models-current.html"
DASHBOARD_PUBLIC="/root/aads/aads-dashboard/public/reports/llm-models-current.html"

docker exec -i aads-server python - <<'PY'
import asyncio
from app.api.llm_report import refresh_static_report

async def main():
    print(await refresh_static_report(refresh=True))

asyncio.run(main())
PY

mkdir -p "$(dirname "$EXPORT_PATH")" "$(dirname "$DASHBOARD_PUBLIC")"
cp "$SERVER_ROOT/app/static/reports/llm-models-current.html" "$EXPORT_PATH"
cp "$SERVER_ROOT/app/static/reports/llm-models-current.html" "$DASHBOARD_PUBLIC"
