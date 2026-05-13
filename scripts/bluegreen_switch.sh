#!/bin/bash
# AADS Blue-Green manual switch — safe wrapper
# Canonical deploy: deploy.sh bluegreen (includes standby sync + marker update)
# This script only switches nginx upstream for emergency use when a full
# deploy.sh run is not desired. It now updates both marker files and uses
# max_fails=0 consistent with deploy.sh.
set -euo pipefail

UPSTREAM_CONF="/etc/nginx/conf.d/aads-upstream.conf"
COMPOSE_DIR="/root/aads/aads-server"

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

cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.pre_manual_switch_$(date +%Y%m%d_%H%M)"
sed -i -E     -e "s/server 127\.0\.0\.1:${BACKUP_PORT} [^;]*;/server 127.0.0.1:${BACKUP_PORT} max_fails=0;/g"     -e "s/server 127\.0\.0\.1:${CURRENT_PORT} [^;]*;/server 127.0.0.1:${CURRENT_PORT} max_fails=3 fail_timeout=30s backup;/g"     "$UPSTREAM_CONF"

if ! nginx -t 2>/dev/null; then
    echo "ERROR: nginx config invalid — rollback"
    cp "${UPSTREAM_CONF}.pre_manual_switch_"* "$UPSTREAM_CONF" 2>/dev/null
    exit 1
fi

systemctl reload nginx

echo "$BACKUP_PORT" > "${COMPOSE_DIR}/.active_port"
echo "$NEW_CONTAINER" > "${COMPOSE_DIR}/.active_container"
docker exec "$NEW_CONTAINER" sh -c "printf true > /tmp/aads_execution_resume_owner" 2>/dev/null || true
docker exec "$OLD_CONTAINER" sh -c "printf false > /tmp/aads_execution_resume_owner" 2>/dev/null || true

echo "Switch complete: :$BACKUP_PORT ($NEW_CONTAINER) active"
echo "WARNING: standby sync NOT included. Run deploy.sh bluegreen for full sync."
