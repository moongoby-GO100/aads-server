#!/bin/bash
nohup bash /root/aads/aads-server/scripts/sync-to-contabo.sh </dev/null >/dev/null 2>&1 &
echo "PID=$!"
