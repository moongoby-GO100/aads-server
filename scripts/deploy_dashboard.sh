#!/bin/bash
# Deprecated compatibility wrapper.
#
# Dashboard deployment must go through the canonical blue-green script so UI
# changes are reflected without restarting the active slot directly.

set -euo pipefail

exec /root/aads/aads-dashboard/deploy.sh
