#!/usr/bin/env bash
# AADS-186C: Langfuse 셀프호스팅 초기 설정 스크립트
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE_FILE="$SERVER_DIR/docker-compose.langfuse.yml"

echo "=== AADS Langfuse 설정 ==="
echo "Compose 파일: $COMPOSE_FILE"

# 1. aads_network 존재 확인
if ! docker network ls --format '{{.Name}}' | grep -q '^aads_network$'; then
    echo "[INFO] aads_network 생성..."
    docker network create aads_network
fi

# 2. Langfuse 컨테이너 시작
echo "[INFO] Langfuse 컨테이너 시작 중..."
docker compose -f "$COMPOSE_FILE" up -d

# 3. 헬스체크 대기 (최대 120초)
echo "[INFO] Langfuse 헬스체크 대기 중..."
for i in $(seq 1 24); do
    if docker compose -f "$COMPOSE_FILE" ps langfuse 2>/dev/null | grep -q "healthy"; then
        echo "[OK] Langfuse 준비 완료!"
        break
    fi
    if [ "$i" -eq 24 ]; then
        echo "[WARN] 헬스체크 타임아웃 — 컨테이너 상태를 직접 확인하세요."
        echo "  docker compose -f $COMPOSE_FILE ps"
        break
    fi
    echo "  대기 중... ($((i * 5))s / 120s)"
    sleep 5
done

# 4. Langfuse 접속 정보 출력
SERVER_IP="${LANGFUSE_SERVER_IP:-localhost}"
echo ""
echo "==================================================================="
echo " Langfuse ready at http://${SERVER_IP}:3001"
echo "==================================================================="
echo ""
echo "초기 관리자 계정 생성 가이드:"
echo "  1. http://${SERVER_IP}:3001 브라우저 접속"
echo "  2. 'Sign Up' 클릭 → 이메일/비밀번호 입력"
echo "  3. 조직(Organization) 생성 → 프로젝트 생성"
echo "  4. Settings > API Keys 에서 Public/Secret Key 발급"
echo "  5. .env 파일에 추가:"
echo "     LANGFUSE_PUBLIC_KEY=pk-lf-..."
echo "     LANGFUSE_SECRET_KEY=sk-lf-..."
echo "     LANGFUSE_HOST=http://${SERVER_IP}:3001"
echo ""
echo "컨테이너 상태:"
docker compose -f "$COMPOSE_FILE" ps
