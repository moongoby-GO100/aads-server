#!/bin/bash
# Deprecated compatibility wrapper.
#
# The canonical AADS API deployment path is deploy.sh bluegreen. It rebuilds the
# inactive slot, switches nginx after health checks, and then rebuilds the old
# slot as warm standby after stream drain. Do not keep a second implementation
# here; older versions stopped the previous slot and could leave blue/green out
# of sync.

set -euo pipefail

exec /root/aads/aads-server/deploy.sh bluegreen
