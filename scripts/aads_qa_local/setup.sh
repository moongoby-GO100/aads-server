#!/bin/bash
# AADS QA Local 설치 스크립트 — T-028
# 211서버(ShortFlow) 배포 후 실행

set -e
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== AADS QA Local Setup ==="
echo "설치 경로: $INSTALL_DIR"

# ── 1. 시스템 의존성 확인 ─────────────────────────────────────────────────────
echo "[1/4] 시스템 의존성 확인..."

# ffmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo "  ffmpeg 없음 — 설치 시도..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y ffmpeg 2>/dev/null || echo "  WARNING: ffmpeg 설치 실패 (수동 설치 필요)"
    elif command -v yum &>/dev/null; then
        yum install -y ffmpeg 2>/dev/null || echo "  WARNING: ffmpeg 설치 실패 (수동 설치 필요)"
    else
        echo "  WARNING: 패키지 매니저 없음 — ffmpeg 수동 설치 필요"
    fi
else
    echo "  ffmpeg: $(ffmpeg -version 2>&1 | head -1)"
fi

# ffprobe
if ! command -v ffprobe &>/dev/null; then
    echo "  WARNING: ffprobe 없음 (ffmpeg-full 패키지 설치 권장)"
else
    echo "  ffprobe: OK"
fi

# python3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 필수" >&2
    exit 1
fi
echo "  python3: $(python3 --version)"

# curl
if ! command -v curl &>/dev/null; then
    echo "ERROR: curl 필수" >&2
    exit 1
fi
echo "  curl: $(curl --version | head -1)"

# ── 2. Python 패키지 설치 ────────────────────────────────────────────────────
echo "[2/4] Python 패키지 설치..."

pip3 install --quiet requests 2>/dev/null || python3 -m pip install --quiet requests
echo "  requests: OK"

# google-generativeai (Gemini Vision)
pip3 install --quiet google-generativeai 2>/dev/null || \
    python3 -m pip install --quiet google-generativeai 2>/dev/null || \
    echo "  WARNING: google-generativeai 설치 실패 — Gemini 분석 비활성, AADS API fallback 사용"

# ── 3. 파일 권한 설정 ────────────────────────────────────────────────────────
echo "[3/4] 파일 권한 설정..."
chmod +x "$INSTALL_DIR/auditor.py"
chmod +x "$INSTALL_DIR/quality_gate.sh"
chmod +x "$INSTALL_DIR/setup.sh"
echo "  chmod +x: OK"

# ── 4. qa_env.sh 생성 (없을 경우) ────────────────────────────────────────────
echo "[4/4] 환경 파일 확인..."
ENV_FILE="${INSTALL_DIR}/.env.aads"
QA_ENV="${INSTALL_DIR}/qa_env.sh"

if [ ! -f "$QA_ENV" ]; then
    cat > "$QA_ENV" <<'ENVEOF'
#!/bin/bash
# AADS QA 환경변수 로드
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.aads"
if [ -f "$ENV_FILE" ]; then
    set -o allexport
    source "$ENV_FILE"
    set +o allexport
else
    echo "WARNING: .env.aads 파일 없음 — $ENV_FILE" >&2
fi
ENVEOF
    chmod +x "$QA_ENV"
    echo "  qa_env.sh 생성됨"
fi

if [ ! -f "$ENV_FILE" ]; then
    echo ""
    echo "  ⚠️  .env.aads 파일을 생성하세요:"
    echo "  cat > ${ENV_FILE} << EOF"
    echo "  GOOGLE_API_KEY=<ShortFlow 기존 Gemini 키>"
    echo "  AADS_API_URL=https://aads.newtalk.kr/api/v1"
    echo "  AADS_MONITOR_KEY=mon_2e950b076dff3c2503dd0991e82674ffa248b8229c04e476e9ee98ffbce79bca"
    echo "  EOF"
fi

echo ""
echo "=== 설치 완료 ==="
echo "다음 단계:"
echo "  1. ${ENV_FILE} 에 GOOGLE_API_KEY 설정"
echo "  2. source ${QA_ENV}"
echo "  3. python3 ${INSTALL_DIR}/auditor.py --help"
echo "  4. ${INSTALL_DIR}/quality_gate.sh --help"
