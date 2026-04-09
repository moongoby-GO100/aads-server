#!/bin/bash
# AADS Blue-Green 수동 전환 스크립트
# 위치: /root/aads/aads-server/scripts/bluegreen_switch.sh
# 용도: deploy.sh bluegreen 없이 수동으로 Blue↔Green 전환할 때 사용
# [2026-04-09] 생성 — /tmp/ 에서 scripts/ 영구 위치로 이동

set -e

UPSTREAM_CONF="/etc/nginx/conf.d/aads-upstream.conf"

echo "🔍 현재 Blue-Green 상태 확인 중..."

# 현재 활성 포트 확인 (upstream에서 backup이 아닌 서버)
CURRENT_PORT=$(grep "server 127.0.0.1:" "$UPSTREAM_CONF" | grep -v backup | head -1 | grep -oP '127\.0\.0\.1:\K[0-9]+')
BACKUP_PORT=$(grep "server 127.0.0.1:.*backup" "$UPSTREAM_CONF" | head -1 | grep -oP '127\.0\.0\.1:\K[0-9]+')

if [ -z "$CURRENT_PORT" ]; then
    echo "❌ 현재 활성 포트를 찾을 수 없습니다."
    exit 1
fi

if [ -z "$BACKUP_PORT" ]; then
    echo "❌ 백업 포트를 찾을 수 없습니다."
    exit 1
fi

echo "✅ 현재 활성: 포트 $CURRENT_PORT"
echo "✅ 백업 대기: 포트 $BACKUP_PORT"

# 헬스체크 함수
health_check() {
    local port=$1
    echo -n "🩺 포트 $port 헬스체크... "
    if curl -s -f --max-time 5 "http://127.0.0.1:$port/api/v1/health" > /dev/null; then
        echo "✅ 건강"
        return 0
    else
        echo "❌ 실패"
        return 1
    fi
}

# 백업 포트 헬스체크
echo ""
echo "📊 백업 서버($BACKUP_PORT) 상태 확인..."
if ! health_check $BACKUP_PORT; then
    echo "❌ 백업 서버가 건강하지 않습니다. 전환을 중단합니다."
    exit 1
fi

# upstream 설정 백업
echo ""
echo "🔄 트래픽 전환 시작..."
cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.backup.$(date +%s)"

# upstream 전환: backup 키워드 스왑
echo "🔧 Upstream 설정 수정 중..."
# 새 활성(이전 백업)에서 backup 제거
sed -i "s/server 127.0.0.1:${BACKUP_PORT} max_fails=3 fail_timeout=30s backup;/server 127.0.0.1:${BACKUP_PORT} max_fails=3 fail_timeout=30s;/g" "$UPSTREAM_CONF"
# 이전 활성을 backup으로 전환
sed -i "s/server 127.0.0.1:${CURRENT_PORT} max_fails=3 fail_timeout=30s;/server 127.0.0.1:${CURRENT_PORT} max_fails=3 fail_timeout=30s backup;/g" "$UPSTREAM_CONF"

# nginx 설정 검증
if ! nginx -t 2>/dev/null; then
    echo "❌ nginx 설정 오류 — 롤백"
    cp "${UPSTREAM_CONF}.backup."* "$UPSTREAM_CONF" 2>/dev/null
    exit 1
fi

# Nginx reload
echo "🔄 Nginx 재로드..."
systemctl reload nginx

echo "⏳ 2초 대기 (연결 안정화)..."
sleep 2

# 상태 파일 업데이트
echo "$BACKUP_PORT" > /root/aads/aads-server/.active_port

echo ""
echo "✅ 전환 완료!"
echo "📊 새 상태:"
echo "   활성: 포트 $BACKUP_PORT"
echo "   백업: 포트 $CURRENT_PORT"

# 최종 헬스체크
echo ""
echo "📊 최종 헬스체크..."
health_check $BACKUP_PORT

echo ""
echo "🎉 Blue-Green 전환 완료!"
