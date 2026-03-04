#!/bin/bash
# T-029: 211서버(ShortFlow) AADS QA 클라이언트 배포 스크립트
# 68서버(AADS)에서 실행 — 211서버로 클라이언트 패키지 배포
#
# 사용법:
#   export SF211_IP=<211서버_실제_IP>
#   bash /root/aads/aads-server/scripts/deploy_to_211.sh
#
# 또는:
#   bash /root/aads/aads-server/scripts/deploy_to_211.sh <211서버_IP>

set -euo pipefail

# ── 인자/환경변수에서 IP 읽기 ─────────────────────────────────────────────
SF211_IP="${1:-${SF211_IP:-}}"

if [ -z "$SF211_IP" ]; then
    echo "ERROR: 211서버 IP가 필요합니다." >&2
    echo "사용법:" >&2
    echo "  export SF211_IP=<IP> && bash $0" >&2
    echo "  또는: bash $0 <IP>" >&2
    exit 1
fi

SF211_USER="${SF211_USER:-root}"
SF211_SSH_KEY="${SF211_SSH_KEY:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_SRC="${SCRIPT_DIR}/aads_qa_local"
CLIENT_SH="${SCRIPT_DIR}/aads_qa_client.sh"
AADS_URL="https://aads.newtalk.kr/api/v1/visual-qa"
REMOTE_INSTALL_DIR="/root/aads_qa"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)
if [ -n "$SF211_SSH_KEY" ]; then
    SSH_OPTS+=(-i "$SF211_SSH_KEY")
fi

_ssh() { ssh "${SSH_OPTS[@]}" "${SF211_USER}@${SF211_IP}" "$@"; }
_scp() { scp "${SSH_OPTS[@]}" "$@"; }

echo "=============================================="
echo " AADS QA Client 배포 → 211서버 (${SF211_IP})"
echo "=============================================="

# ── 1. 연결 테스트 ─────────────────────────────────────────────────────────
echo "[1/6] 연결 테스트..."
if ! _ssh "echo 'SSH OK'"; then
    echo "ERROR: 211서버 SSH 접속 실패 (${SF211_USER}@${SF211_IP})" >&2
    exit 1
fi
echo "  ✅ SSH 접속 성공"

# ── 2. 원격 디렉토리 준비 ─────────────────────────────────────────────────
echo "[2/6] 원격 디렉토리 준비..."
_ssh "mkdir -p ${REMOTE_INSTALL_DIR}"
echo "  ✅ ${REMOTE_INSTALL_DIR} 생성됨"

# ── 3. 파일 전송 ──────────────────────────────────────────────────────────
echo "[3/6] 파일 전송..."

# aads_qa_client.sh (심플 클라이언트)
if [ -f "$CLIENT_SH" ]; then
    _scp "$CLIENT_SH" "${SF211_USER}@${SF211_IP}:/root/aads_qa_client.sh"
    _ssh "chmod +x /root/aads_qa_client.sh"
    echo "  ✅ /root/aads_qa_client.sh"
else
    echo "  WARNING: aads_qa_client.sh 없음 — 건너뜀"
fi

# aads_qa_local/ 패키지 전송
if [ -d "$PKG_SRC" ]; then
    # tar로 묶어 전송
    TAR_FILE="/tmp/aads_qa_local_$(date +%Y%m%d%H%M%S).tar.gz"
    tar -czf "$TAR_FILE" -C "${SCRIPT_DIR}" aads_qa_local/
    _scp "$TAR_FILE" "${SF211_USER}@${SF211_IP}:/tmp/aads_qa_local.tar.gz"
    _ssh "tar -xzf /tmp/aads_qa_local.tar.gz -C ${REMOTE_INSTALL_DIR} --strip-components=1 && rm -f /tmp/aads_qa_local.tar.gz"
    rm -f "$TAR_FILE"
    echo "  ✅ ${REMOTE_INSTALL_DIR}/ (aads_qa_local 패키지)"
else
    echo "  WARNING: ${PKG_SRC} 없음 — 건너뜀"
fi

# run_v4_pipeline_qa_patch.py 전송 (T-029: run_v4 연동 모듈)
RUN_V4_PATCH="${SCRIPT_DIR}/run_v4_pipeline_qa_patch.py"
if [ -f "$RUN_V4_PATCH" ]; then
    _scp "$RUN_V4_PATCH" "${SF211_USER}@${SF211_IP}:${REMOTE_INSTALL_DIR}/run_v4_pipeline_qa_patch.py"
    _ssh "chmod +x ${REMOTE_INSTALL_DIR}/run_v4_pipeline_qa_patch.py"
    echo "  ✅ ${REMOTE_INSTALL_DIR}/run_v4_pipeline_qa_patch.py"
else
    echo "  WARNING: run_v4_pipeline_qa_patch.py 없음 — 건너뜀"
fi

# ── 4. 권한 설정 + 환경변수 등록 ─────────────────────────────────────────
echo "[4/6] 원격 환경 설정..."
_ssh bash <<REMOTE
set -e

# 실행 권한
chmod +x ${REMOTE_INSTALL_DIR}/quality_gate.sh   2>/dev/null || true
chmod +x ${REMOTE_INSTALL_DIR}/auditor.py         2>/dev/null || true
chmod +x ${REMOTE_INSTALL_DIR}/setup.sh           2>/dev/null || true

# symlink: /root/aads_qa → REMOTE_INSTALL_DIR (이미 같은 경로면 스킵)
if [ "${REMOTE_INSTALL_DIR}" != "/root/aads_qa" ]; then
    ln -sfn ${REMOTE_INSTALL_DIR} /root/aads_qa 2>/dev/null || true
fi

# .bashrc에 AADS_QA_URL 등록 (중복 방지)
if ! grep -q "AADS_QA_URL" /root/.bashrc 2>/dev/null; then
    echo "export AADS_QA_URL=${AADS_URL}" >> /root/.bashrc
    echo "AADS_QA_URL 등록됨"
else
    echo "AADS_QA_URL 이미 등록됨"
fi

# .bashrc에 PATH 등록
if ! grep -q "aads_qa" /root/.bashrc 2>/dev/null; then
    echo "export PATH=\\\$PATH:${REMOTE_INSTALL_DIR}" >> /root/.bashrc
    echo "PATH 등록됨"
fi

echo "환경 설정 완료"
REMOTE
echo "  ✅ 권한 설정 + 환경변수 등록 완료"

# ── 5. Python 의존성 설치 ────────────────────────────────────────────────
echo "[5/6] Python 의존성 설치..."
_ssh bash <<'REMOTE'
pip3 install --quiet requests 2>/dev/null || python3 -m pip install --quiet requests 2>/dev/null || echo "WARNING: requests 설치 실패"
pip3 install --quiet google-generativeai 2>/dev/null || \
    python3 -m pip install --quiet google-generativeai 2>/dev/null || \
    echo "WARNING: google-generativeai 설치 실패 (AADS API fallback 사용)"
echo "의존성 설치 완료"
REMOTE
echo "  ✅ Python 의존성 설치 완료"

# ── 6. 동작 확인 ──────────────────────────────────────────────────────────
echo "[6/6] 동작 확인..."
_ssh bash <<REMOTE
echo "=== /root/aads_qa_client.sh --help ==="
/root/aads_qa_client.sh --help 2>&1 | head -20 || echo "aads_qa_client.sh 실행 실패"

echo ""
echo "=== auditor.py --help ==="
python3 ${REMOTE_INSTALL_DIR}/auditor.py --help 2>&1 | head -10 || echo "auditor.py 실행 실패"

echo ""
echo "=== AADS 서버 연결 테스트 ==="
HTTP_CODE=\$(curl -s -o /dev/null -w "%{http_code}" https://aads.newtalk.kr/api/v1/health -H "User-Agent: curl/7.64.0" --max-time 10)
echo "AADS health: HTTP \${HTTP_CODE}"
REMOTE
echo "  ✅ 동작 확인 완료"

echo ""
echo "=============================================="
echo " 배포 완료 — 211서버 (${SF211_IP})"
echo "=============================================="
echo ""
echo "다음 단계:"
echo "  1. 211서버에서 .env.aads 파일 생성:"
echo "     cat > ${REMOTE_INSTALL_DIR}/.env.aads << EOF"
echo "     GOOGLE_API_KEY=<ShortFlow Gemini 키>"
echo "     AADS_API_URL=https://aads.newtalk.kr/api/v1"
echo "     AADS_MONITOR_KEY=<모니터링 키>"
echo "     EOF"
echo ""
echo "  2. run_v4_pipeline.py에 통합 (run_v4_integration.py 참조):"
echo "     cp ${REMOTE_INSTALL_DIR}/run_v4_integration.py /data/shortflow/"
echo "     # 또는 sys.path에 ${REMOTE_INSTALL_DIR} 추가 후 import"
echo ""
echo "  3. 벤치마크 등록 (채널별 최초 1회):"
echo "     /root/aads_qa_client.sh quality-gate /data/shortflow/outputs/economy/best.mp4 shortflow economy eco_benchmark"
echo ""
echo "  4. 검수 테스트:"
echo "     /root/aads_qa_client.sh quality-gate /data/shortflow/outputs/economy/latest.mp4 shortflow economy eco_\$(date +%Y%m%d)"
