#!/bin/bash
# T-062: 116서버 AADS Remote Agent 데몬 배포 스크립트
# 68서버(AADS)에서 실행 — 116서버로 aads_remote_agent.py 배포
#
# 사용법:
#   export NT116_IP=<116서버_실제_IP>
#   export NT116_SSH_KEY=~/.ssh/id_ed25519_newtalk
#   export AADS_MONITOR_KEY=<모니터_키>
#   bash /root/aads/aads-server/scripts/deploy_remote_to_116.sh
#
# 환경변수:
#   NT116_IP          116서버 IP (필수)
#   NT116_PORT        SSH 포트 (기본: 22)
#   NT116_USER        SSH 사용자 (기본: root)
#   NT116_SSH_KEY     SSH 개인키 경로 (기본: ~/.ssh/id_ed25519_newtalk)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AADS_ENV_FILE="${SCRIPT_DIR}/../.env"

NT116_IP="${1:-${NT116_IP:-}}"
NT116_PORT="${2:-${NT116_PORT:-22}}"
NT116_USER="${NT116_USER:-root}"
NT116_SSH_KEY="${NT116_SSH_KEY:-${HOME}/.ssh/id_ed25519_newtalk}"

if [ -z "$NT116_IP" ]; then
    echo "ERROR: 116서버 IP 필요" >&2
    echo "사용: export NT116_IP=<IP> && bash $0" >&2
    exit 1
fi

# AADS_MONITOR_KEY 로드
if [ -z "${AADS_MONITOR_KEY:-}" ]; then
    if [ -f "$AADS_ENV_FILE" ]; then
        AADS_MONITOR_KEY=$(grep -E "^AADS_MONITOR_KEY=" "$AADS_ENV_FILE" | cut -d= -f2- | tr -d '"' || echo "")
    fi
fi

REMOTE_DIR="/root/aads-remote"
AGENT_SCRIPT="${SCRIPT_DIR}/aads_remote_agent.py"
SERVICE_FILE="${SCRIPT_DIR}/aads-remote-agent-116.service"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=15 -p "$NT116_PORT")
[ -f "$NT116_SSH_KEY" ] && SSH_OPTS+=(-i "$NT116_SSH_KEY")

_ssh() { ssh "${SSH_OPTS[@]}" "${NT116_USER}@${NT116_IP}" "$@"; }
_scp() {
    scp -P "$NT116_PORT" \
        $([ -f "$NT116_SSH_KEY" ] && echo "-i $NT116_SSH_KEY") \
        -o StrictHostKeyChecking=no \
        "$@"
}

echo "=================================================="
echo " AADS Remote Agent 배포 → 116서버 (${NT116_IP})"
echo " T-062: newtalk_v2 / NT_MGR / 포트 9900"
echo "=================================================="

# ── 1. SSH 연결 + 사전 확인 ──────────────────────────────────────────────────
echo ""
echo "[1/6] SSH 연결 + 사전 확인..."
_ssh bash <<'REMOTE'
echo "=== hostname ==="
hostname

echo "=== which claude ==="
which claude 2>/dev/null && claude --version 2>/dev/null || echo "claude: not found"

echo "=== newtalk 디렉토리 ==="
ls /root/newtalk* 2>/dev/null || ls /srv/newtalk* 2>/dev/null || echo "newtalk 디렉토리 없음"

echo "=== python3 ==="
python3 --version 2>/dev/null || echo "python3 없음"

echo "=== pip aiohttp ==="
python3 -c "import aiohttp; print('aiohttp OK:', aiohttp.__version__)" 2>/dev/null || echo "aiohttp 없음 (설치 필요)"
REMOTE
echo "  ✅ 사전 확인 완료"

# ── 2. aiohttp 설치 (없을 경우) ──────────────────────────────────────────────
echo ""
echo "[2/6] 의존성 설치 (aiohttp)..."
_ssh bash <<'REMOTE'
python3 -c "import aiohttp" 2>/dev/null || {
    echo "  aiohttp 설치 중..."
    pip3 install aiohttp --quiet 2>/dev/null || \
    pip install aiohttp --quiet 2>/dev/null || \
    echo "  WARNING: pip 설치 실패 — 수동 설치 필요"
}
python3 -c "import aiohttp; print('  aiohttp:', aiohttp.__version__)" 2>/dev/null || echo "  aiohttp 미설치"
REMOTE
echo "  ✅ 의존성 확인 완료"

# ── 3. 원격 디렉토리 준비 + 파일 전송 ────────────────────────────────────────
echo ""
echo "[3/6] 원격 디렉토리 준비 + 파일 전송..."
_ssh "mkdir -p ${REMOTE_DIR}"

# aads_remote_agent.py 전송
_scp "$AGENT_SCRIPT" "${NT116_USER}@${NT116_IP}:${REMOTE_DIR}/aads_remote_agent.py"
_ssh "chmod +x ${REMOTE_DIR}/aads_remote_agent.py"
echo "  ✅ aads_remote_agent.py 전송됨"

# 서비스 파일 전송
_scp "$SERVICE_FILE" "${NT116_USER}@${NT116_IP}:/tmp/aads-remote-agent.service"
echo "  ✅ aads-remote-agent.service 전송됨"

# ── 4. .env 파일 생성 (PROJECTS: newtalk_v2) ─────────────────────────────────
echo ""
echo "[4/6] .env 파일 생성..."
_ssh bash <<REMOTE
cat > ${REMOTE_DIR}/.env <<'EOF'
AADS_SERVER=https://aads.newtalk.kr/api/v1
AADS_REMOTE_KEY=changeme
AADS_REMOTE_PORT=9900
AADS_AGENT_ID=REMOTE_116
AADS_LOG_FILE=/var/log/aads_remote_agent.log
AADS_REPORT_INTERVAL=300
EOF
chmod 600 ${REMOTE_DIR}/.env
echo "  ✅ .env 생성됨"
cat ${REMOTE_DIR}/.env
REMOTE

# aads_remote_agent.py의 PROJECTS를 newtalk_v2로 패치
echo "  PROJECTS 패치 (newtalk_v2)..."
_ssh python3 <<'PYEOF'
import re, os
path = "/root/aads-remote/aads_remote_agent.py"
with open(path) as f:
    content = f.read()

# PROJECTS 섹션 교체
new_projects = '''PROJECTS = {
    "newtalk_v2": {
        "path": "/root/newtalk-v2",
        "manager": "NT_MGR",
        "log_dirs": ["/root/newtalk-v2/storage/logs", "/var/log/newtalk-v2"],
    },
}'''

# 기존 PROJECTS 블록 교체
content_new = re.sub(
    r'PROJECTS\s*=\s*\{.*?\}(?=\s*\n\n)',
    new_projects,
    content,
    flags=re.DOTALL
)

if content_new == content:
    print("WARNING: PROJECTS 패치 실패 (패턴 불일치) — 수동 확인 필요")
else:
    with open(path, "w") as f:
        f.write(content_new)
    print("  ✅ PROJECTS 패치 완료: newtalk_v2 / NT_MGR")
PYEOF

# ── 5. systemd 등록 + 시작 ───────────────────────────────────────────────────
echo ""
echo "[5/6] systemd 등록 + 시작..."
_ssh bash <<REMOTE
set -e
cp /tmp/aads-remote-agent.service /etc/systemd/system/aads-remote-agent.service
chmod 644 /etc/systemd/system/aads-remote-agent.service
systemctl daemon-reload
systemctl enable aads-remote-agent.service
systemctl restart aads-remote-agent.service
echo "  서비스 시작 대기 (3초)..."
sleep 3
systemctl status aads-remote-agent.service --no-pager | head -20
REMOTE
echo "  ✅ systemd 등록 + 시작 완료"

# ── 6. 검증 ──────────────────────────────────────────────────────────────────
echo ""
echo "[6/6] 검증..."
_ssh bash <<'REMOTE'
echo "=== systemd 상태 ==="
systemctl is-active aads-remote-agent.service

echo ""
echo "=== /health 엔드포인트 ==="
sleep 2
HTTP_CODE=$(curl -s -o /tmp/health_resp.json -w "%{http_code}" \
    http://localhost:9900/health --max-time 10 2>/dev/null || echo "000")
echo "HTTP: ${HTTP_CODE}"
[ -f /tmp/health_resp.json ] && cat /tmp/health_resp.json | python3 -m json.tool 2>/dev/null || true
REMOTE

echo ""
echo "=== 68서버에서 116서버 외부 health 체크 ==="
HTTP_EXT=$(curl -s -o /dev/null -w "%{http_code}" \
    "http://${NT116_IP}:9900/health" --max-time 15 2>/dev/null || echo "000")
echo "116서버 :9900/health HTTP: ${HTTP_EXT}"

if [ "${HTTP_EXT}" = "200" ]; then
    echo "  ✅ health 200 OK"
else
    echo "  ❌ health 응답 이상 (HTTP ${HTTP_EXT})"
fi

echo ""
echo "=== 68서버 memory REMOTE_116 확인 ==="
sleep 5
curl -s \
    -H "X-Monitor-Key: ${AADS_MONITOR_KEY:-}" \
    -H "User-Agent: curl/7.64.0" \
    "https://aads.newtalk.kr/api/v1/context/system/remote_agents/REMOTE_116" \
    --max-time 15 | python3 -m json.tool 2>/dev/null || echo "(응답 없음)"

echo ""
echo "=================================================="
echo " T-062 배포 완료"
echo " 116서버 :9900/health → HTTP ${HTTP_EXT}"
echo "=================================================="
