#!/bin/bash
# OAuth Direct Switch — 롤백 스크립트
# 사용법: bash /root/aads/aads-server/scripts/oauth_switch_rollback.sh
set -e

BACKUP_DIR="/root/aads/aads-server/backup/direct_oauth_20260326"
RELAY_SCRIPT="/root/aads/aads-server/scripts/claude_relay_server.py"
SERVICE="claude-relay.service"

echo "=== OAuth Direct Switch ROLLBACK ==="
echo "[1/4] 백업 파일 존재 확인..."
if [ ! -f "$BACKUP_DIR/claude_relay_server.py" ]; then
    echo "ERROR: 백업 파일 없음: $BACKUP_DIR/claude_relay_server.py"
    exit 1
fi
echo "  OK: 백업 파일 확인됨"

echo "[2/4] relay 스크립트 복원..."
cp "$BACKUP_DIR/claude_relay_server.py" "$RELAY_SCRIPT"
echo "  OK: claude_relay_server.py 복원됨"

echo "[3/4] systemd env에서 AADS_CLAUDE_DIRECT_OAUTH 제거 (drop-in)..."
DROP_IN="/etc/systemd/system/claude-relay.service.d/oauth-direct.conf"
if [ -f "$DROP_IN" ]; then
    rm -f "$DROP_IN"
    echo "  OK: drop-in 제거됨"
else
    echo "  SKIP: drop-in 없음 (이미 제거됨)"
fi

echo "[4/4] relay 재시작..."
systemctl daemon-reload
systemctl restart "$SERVICE"
sleep 2

# 헬스체크
HEALTH=$(curl -s http://localhost:8199/health 2>/dev/null || echo '{"status":"FAIL"}')
echo "  Health: $HEALTH"

if echo "$HEALTH" | grep -q '"status": "ok"'; then
    echo ""
    echo "=== ROLLBACK 완료 — LiteLLM 프록시 모드 복원됨 ==="
else
    echo ""
    echo "=== WARNING: relay health check 실패! 수동 확인 필요 ==="
    echo "  systemctl status $SERVICE"
    echo "  journalctl -u $SERVICE --no-pager -n 20"
    exit 1
fi
