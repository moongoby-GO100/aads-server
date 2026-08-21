#!/usr/bin/env bash
set -euo pipefail

# Compatibility entrypoint for the former cron watchdog. All API recovery is
# centralized in the host-level, blue/green-aware watchdog and its audit log.
exec /root/aads/aads-server/scripts/aads_api_watchdog.sh "$@"
