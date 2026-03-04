#!/bin/bash
# AADS QA 환경변수 로드 — 211서버 배포용 T-028
# 사용: source /root/aads_qa/qa_env.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.aads"

if [ -f "$ENV_FILE" ]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
    echo "[AADS QA] 환경변수 로드 완료: $ENV_FILE" >&2
else
    echo "WARNING: .env.aads 파일 없음 — $ENV_FILE" >&2
    echo "다음 명령으로 생성하세요:" >&2
    echo "  cat > ${ENV_FILE} << EOF" >&2
    echo "  GOOGLE_API_KEY=<your_key>" >&2
    echo "  AADS_API_URL=https://aads.newtalk.kr/api/v1" >&2
    echo "  AADS_MONITOR_KEY=mon_2e950b076dff3c2503dd0991e82674ffa248b8229c04e476e9ee98ffbce79bca" >&2
    echo "  EOF" >&2
fi
