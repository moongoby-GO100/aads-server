#!/bin/bash
# Deprecated compatibility wrapper.
#
# Direct dashboard rebuilds bypass nginx blue-green switching and can leave
# the standby slot stale. Keep this entrypoint for callers, but route it to the
# canonical deploy script.

set -euo pipefail

exec /root/aads/aads-dashboard/deploy.sh
