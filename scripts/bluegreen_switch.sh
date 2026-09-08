#!/bin/bash
# AADS Blue-Green manual switch — safe wrapper
# Canonical deploy: deploy.sh bluegreen (includes standby sync + marker update)
# This script only switches nginx upstream for emergency use when a full
# deploy.sh run is not desired. It now updates both marker files and uses
# max_fails=0 consistent with deploy.sh.
set -euo pipefail

UPSTREAM_CONF="/etc/nginx/conf.d/aads-upstream.conf"
COMPOSE_DIR="/root/aads/aads-server"
NGINX_LOCK="/tmp/aads-nginx-upstream.lock"
STATE_WRITER="${COMPOSE_DIR}/scripts/aads_active_slot_state.sh"

exec 9>"$NGINX_LOCK"
if ! flock -w 10 9; then
    echo "ERROR: nginx cutover lock is busy"
    exit 1
fi

CURRENT_PORT=$(grep "server 127.0.0.1:" "$UPSTREAM_CONF" | grep -v backup | head -1 | grep -oP '127\.0\.0\.1:\K[0-9]+')
BACKUP_PORT=$(grep "server 127.0.0.1:.*backup" "$UPSTREAM_CONF" | head -1 | grep -oP '127\.0\.0\.1:\K[0-9]+')

if [[ -z "$CURRENT_PORT" || -z "$BACKUP_PORT" ]]; then
    echo "ERROR: cannot determine active/backup ports"; exit 1
fi

case "$BACKUP_PORT" in
    8100) NEW_CONTAINER="aads-server" ;;
    8102) NEW_CONTAINER="aads-server-green" ;;
    *) echo "ERROR: unknown port $BACKUP_PORT"; exit 1 ;;
esac
case "$CURRENT_PORT" in
    8100) OLD_CONTAINER="aads-server" ;;
    8102) OLD_CONTAINER="aads-server-green" ;;
esac

echo "Current: :$CURRENT_PORT -> switching to :$BACKUP_PORT ($NEW_CONTAINER)"

if ! curl -sf "http://127.0.0.1:${BACKUP_PORT}/api/v1/health" >/dev/null 2>&1; then
    echo "ERROR: backup slot :$BACKUP_PORT health failed"; exit 1
fi

BACKUP_CONF="${UPSTREAM_CONF}.pre_manual_switch_$(date +%Y%m%d_%H%M%S)"
cp "$UPSTREAM_CONF" "$BACKUP_CONF"
sed -i -E     -e "s/server 127\.0\.0\.1:${BACKUP_PORT} [^;]*;/server 127.0.0.1:${BACKUP_PORT} max_fails=0;/g"     -e "s/server 127\.0\.0\.1:${CURRENT_PORT} [^;]*;/server 127.0.0.1:${CURRENT_PORT} max_fails=3 fail_timeout=30s backup;/g"     "$UPSTREAM_CONF"

if ! nginx -t 2>/dev/null; then
    echo "ERROR: nginx config invalid — rollback"
    cp "$BACKUP_CONF" "$UPSTREAM_CONF"
    exit 1
fi

systemctl reload nginx

if ! curl -fsS --max-time 5 -H 'Host: aads.newtalk.kr' \
    'http://127.0.0.1/api/v1/health' >/dev/null 2>&1; then
    echo "ERROR: routed health failed — rollback"
    cp "$BACKUP_CONF" "$UPSTREAM_CONF"
    systemctl reload nginx
    exit 1
fi

if [[ ! -x "$STATE_WRITER" ]] || ! AADS_SLOT_STATE_LOCK_HELD=true "$STATE_WRITER" write \
    "$BACKUP_PORT" "$NEW_CONTAINER" "manual-bluegreen-switch" "routed health passed"; then
    echo "ERROR: active-slot marker authorization failed — rollback"
    cp "$BACKUP_CONF" "$UPSTREAM_CONF"
    systemctl reload nginx
    exit 1
fi
docker exec "$NEW_CONTAINER" sh -c "printf true > /tmp/aads_execution_resume_owner" 2>/dev/null || true
docker exec "$OLD_CONTAINER" sh -c "printf false > /tmp/aads_execution_resume_owner" 2>/dev/null || true

echo "Switch complete: :$BACKUP_PORT ($NEW_CONTAINER) active"
echo "WARNING: standby sync NOT included. Run deploy.sh bluegreen for full sync."
