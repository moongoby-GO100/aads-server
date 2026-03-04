#!/bin/bash
# T-030: 116서버(뉴톡 V2) AADS QA 클라이언트 배포 스크립트
# 68서버(AADS)에서 실행 — 116서버로 클라이언트 패키지 배포
#
# 사용법:
#   export NT116_IP=<116서버_실제_IP>
#   export NT116_PORT=<SSH포트>
#   export NT116_SSH_KEY=~/.ssh/id_ed25519_newtalk
#   bash /root/aads/aads-server/scripts/deploy_to_116.sh
#
# 또는:
#   bash /root/aads/aads-server/scripts/deploy_to_116.sh <116서버_IP> [SSH포트]
#
# 환경변수:
#   NT116_IP       116서버 IP (필수)
#   NT116_PORT     SSH 포트 (기본: 22)
#   NT116_USER     SSH 사용자 (기본: root)
#   NT116_SSH_KEY  SSH 개인키 경로 (기본: ~/.ssh/id_ed25519_newtalk)
#   AADS_MONITOR_KEY  AADS 모니터 키 (68서버 .env에서 자동 로드)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AADS_ENV_FILE="${SCRIPT_DIR}/../.env"

# ── 인자/환경변수에서 IP, 포트 읽기 ─────────────────────────────────────
NT116_IP="${1:-${NT116_IP:-}}"
NT116_PORT="${2:-${NT116_PORT:-22}}"
NT116_USER="${NT116_USER:-root}"
NT116_SSH_KEY="${NT116_SSH_KEY:-${HOME}/.ssh/id_ed25519_newtalk}"

if [ -z "$NT116_IP" ]; then
    echo "ERROR: 116서버 IP가 필요합니다." >&2
    echo "사용법:" >&2
    echo "  export NT116_IP=<IP> NT116_PORT=<PORT> && bash $0" >&2
    echo "  또는: bash $0 <IP> [PORT]" >&2
    exit 1
fi

# ── AADS_MONITOR_KEY 로드 ────────────────────────────────────────────────
if [ -z "${AADS_MONITOR_KEY:-}" ]; then
    if [ -f "$AADS_ENV_FILE" ]; then
        AADS_MONITOR_KEY=$(grep -E "^AADS_MONITOR_KEY=" "$AADS_ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
    fi
fi

if [ -z "${AADS_MONITOR_KEY:-}" ]; then
    echo "WARNING: AADS_MONITOR_KEY 없음 — .env.aads에 수동 입력 필요" >&2
    AADS_MONITOR_KEY="<AADS_MONITOR_KEY_여기에_입력>"
fi

CLIENT_SH="${SCRIPT_DIR}/aads_qa_client.sh"
AADS_QA_URL="https://aads.newtalk.kr/api/v1/visual-qa"
AADS_API_URL="https://aads.newtalk.kr/api/v1"
REMOTE_INSTALL_DIR="/root/aads_qa"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -p "$NT116_PORT")
if [ -f "$NT116_SSH_KEY" ]; then
    SSH_OPTS+=(-i "$NT116_SSH_KEY")
else
    echo "WARNING: SSH 키 없음 (${NT116_SSH_KEY}) — 패스워드 인증으로 시도" >&2
fi

_ssh() { ssh "${SSH_OPTS[@]}" "${NT116_USER}@${NT116_IP}" "$@"; }
_scp() { scp -P "$NT116_PORT" "${SSH_OPTS[@]/#-p*/}" "$@"; }

echo "================================================"
echo " AADS QA Client 배포 → 116서버 뉴톡 V2 (${NT116_IP}:${NT116_PORT})"
echo "================================================"

# ── 1. 연결 테스트 ─────────────────────────────────────────────────────────
echo "[1/6] 연결 테스트..."
if ! _ssh "echo 'SSH OK'"; then
    echo "ERROR: 116서버 SSH 접속 실패 (${NT116_USER}@${NT116_IP}:${NT116_PORT})" >&2
    exit 1
fi
echo "  ✅ SSH 접속 성공"

# ── 2. 원격 디렉토리 준비 ─────────────────────────────────────────────────
echo "[2/6] 원격 디렉토리 준비..."
_ssh "mkdir -p ${REMOTE_INSTALL_DIR}"
echo "  ✅ ${REMOTE_INSTALL_DIR} 생성됨"

# ── 3. 파일 전송 ──────────────────────────────────────────────────────────
echo "[3/6] 파일 전송..."

# aads_qa_client.sh 전송
if [ -f "$CLIENT_SH" ]; then
    scp -P "$NT116_PORT" $([ -f "$NT116_SSH_KEY" ] && echo "-i $NT116_SSH_KEY") \
        -o StrictHostKeyChecking=no \
        "$CLIENT_SH" "${NT116_USER}@${NT116_IP}:${REMOTE_INSTALL_DIR}/aads_qa_client.sh"
    _ssh "chmod +x ${REMOTE_INSTALL_DIR}/aads_qa_client.sh"
    echo "  ✅ ${REMOTE_INSTALL_DIR}/aads_qa_client.sh"
else
    echo "  ERROR: aads_qa_client.sh 없음 (${CLIENT_SH})" >&2
    exit 1
fi

# ── 4. .env.aads 생성 ─────────────────────────────────────────────────────
echo "[4/6] .env.aads 환경 파일 생성..."
_ssh bash <<REMOTE
cat > ${REMOTE_INSTALL_DIR}/.env.aads << 'EOF'
AADS_API_URL=${AADS_API_URL}
AADS_QA_URL=${AADS_QA_URL}
AADS_MONITOR_KEY=${AADS_MONITOR_KEY}
EOF
chmod 600 ${REMOTE_INSTALL_DIR}/.env.aads
echo "  ✅ .env.aads 생성됨 (chmod 600)"
REMOTE

# ── 5. 원격 환경 설정 ─────────────────────────────────────────────────────
echo "[5/6] 원격 환경 설정..."
_ssh bash <<REMOTE
set -e

# .bashrc에 AADS_QA_URL 등록 (중복 방지)
if ! grep -q "AADS_QA_URL" /root/.bashrc 2>/dev/null; then
    echo "export AADS_QA_URL=${AADS_QA_URL}" >> /root/.bashrc
    echo "  AADS_QA_URL 등록됨"
else
    echo "  AADS_QA_URL 이미 등록됨"
fi

# .bashrc에 PATH 등록
if ! grep -q "${REMOTE_INSTALL_DIR}" /root/.bashrc 2>/dev/null; then
    echo "export PATH=\\\$PATH:${REMOTE_INSTALL_DIR}" >> /root/.bashrc
    echo "  PATH 등록됨"
fi

# python3 존재 확인
python3 --version 2>/dev/null || echo "  WARNING: python3 없음 — base64 파싱 불가"

echo "환경 설정 완료"
REMOTE
echo "  ✅ 환경 설정 완료"

# ── 6. 동작 확인 ──────────────────────────────────────────────────────────
echo "[6/6] 동작 확인..."
_ssh bash <<REMOTE
echo "=== aads_qa_client.sh --help ==="
${REMOTE_INSTALL_DIR}/aads_qa_client.sh --help 2>&1 | head -20 || echo "실행 실패"

echo ""
echo "=== .env.aads 내용 ==="
ls -la ${REMOTE_INSTALL_DIR}/ || true

echo ""
echo "=== AADS 서버 연결 테스트 ==="
HTTP_CODE=\$(curl -s -o /dev/null -w "%{http_code}" \
    https://aads.newtalk.kr/api/v1/health \
    -H "User-Agent: curl/7.64.0" --max-time 10 2>/dev/null || echo "000")
echo "AADS health: HTTP \${HTTP_CODE}"
REMOTE
echo "  ✅ 동작 확인 완료"

echo ""
echo "================================================"
echo " 배포 완료 — 116서버 뉴톡 V2 (${NT116_IP}:${NT116_PORT})"
echo "================================================"
echo ""
echo "다음 단계 (116서버에서 실행):"
echo "  1. 단일 이미지 게이트 테스트:"
echo "     SAMPLE=\$(find /srv/newtalk-v2/storage -name '*.jpg' -o -name '*.png' 2>/dev/null | head -1)"
echo "     ${REMOTE_INSTALL_DIR}/aads_qa_client.sh image-gate \"\$SAMPLE\" newtalk_v2 test_\$(date +%Y%m%d)"
echo ""
echo "  2. 배치 이미지 검수:"
echo "     ${REMOTE_INSTALL_DIR}/aads_qa_client.sh image-qa /path/img1.jpg newtalk_v2 prod_001"
echo ""
echo "  3. 68서버 Context API에서 결과 확인:"
echo "     curl -s -H 'User-Agent: curl/7.64.0' \\"
echo "       -H 'X-Monitor-Key:${AADS_MONITOR_KEY}' \\"
echo "       https://aads.newtalk.kr/api/v1/visual-qa/qa-results/newtalk"
echo ""
echo "  4. Laravel 연동: /root/aads/aads-server/docs/ProductController_AADS.php 참조"
echo "     (CEO 승인 후 적용)"
