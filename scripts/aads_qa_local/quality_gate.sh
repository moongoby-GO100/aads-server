#!/bin/bash
# AADS Quality Gate — 211서버(ShortFlow) 배포용 T-028
#
# 사용법:
#   quality_gate.sh video   <video_path> <project> <channel> <video_id>
#   quality_gate.sh benchmark <video_path> <project> <channel>
#
# 반환 코드:
#   0: AUTO_PUBLISH  — 품질 통과, 업로드 진행
#   2: CONDITIONAL   — CEO 리뷰 대기
#   3: AUTO_REJECT   — 품질 미달, 재렌더링 필요
#   1: ERROR         — 오류 발생
#
# 환경변수 (qa_env.sh 에서 자동 로드):
#   AADS_API_URL     — AADS 서버 URL
#   AADS_MONITOR_KEY — 모니터링 키
#   GOOGLE_API_KEY   — Gemini Vision 키

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 환경변수 로드
if [ -f "$SCRIPT_DIR/qa_env.sh" ]; then
    source "$SCRIPT_DIR/qa_env.sh"
elif [ -f "/root/aads_qa/qa_env.sh" ]; then
    source /root/aads_qa/qa_env.sh
fi

AADS_URL="${AADS_API_URL:-https://aads.newtalk.kr/api/v1}"
AUDITOR="${SCRIPT_DIR}/auditor.py"
if [ ! -f "$AUDITOR" ]; then
    AUDITOR="/root/aads_qa/auditor.py"
fi

CMD=$1
shift

usage() {
    cat <<EOF
AADS Quality Gate v1.0 (T-028)

명령:
  video     <video_path> <project> <channel> <video_id>
      영상 품질 검수 후 AADS API에 결과 전송
      반환: 0=AUTO_PUBLISH, 2=CONDITIONAL, 3=AUTO_REJECT, 1=ERROR

  benchmark <video_path> <project> <channel>
      벤치마크 스펙 등록 (최초 1회, 채널별)

예시:
  $0 video /data/shortflow/outputs/economy/latest.mp4 shortflow economy eco_20260304
  $0 benchmark /data/shortflow/outputs/economy/best_video.mp4 shortflow economy
EOF
    exit 0
}

case "$CMD" in
    --help|-h|help)
        usage
        ;;

    video)
        VIDEO_PATH=$1; PROJECT=${2:-shortflow}; CHANNEL=$3; VIDEO_ID=$4
        if [ -z "$VIDEO_PATH" ] || [ -z "$CHANNEL" ] || [ -z "$VIDEO_ID" ]; then
            echo "Usage: $0 video <video_path> <project> <channel> <video_id>" >&2
            exit 1
        fi

        if python3 "$AUDITOR" video "$VIDEO_PATH" \
            --project "$PROJECT" \
            --channel "$CHANNEL" \
            --video-id "$VIDEO_ID"; then
            EXIT_CODE=$?
        else
            EXIT_CODE=$?
        fi

        # auditor.py가 없거나 실패시 AADS API 직접 호출 fallback
        if [ "$EXIT_CODE" -eq 127 ] || [ "$EXIT_CODE" -eq 126 ]; then
            echo "[Quality Gate] auditor.py 실행 실패, AADS API 직접 호출..." >&2
            RESULT=$(curl -s -X POST "$AADS_URL/visual-qa/quality-gate" \
                -H "Content-Type: application/json" \
                -H "X-Monitor-Key: ${AADS_MONITOR_KEY}" \
                -d "{\"project_id\":\"$PROJECT\",\"video_path\":\"$VIDEO_PATH\",\"video_id\":\"$VIDEO_ID\",\"channel_name\":\"$CHANNEL\",\"auto_correct\":true}" \
                --max-time 120)

            if [ -z "$RESULT" ]; then
                echo "ERROR: AADS 서버 응답 없음 ($AADS_URL)" >&2
                exit 1
            fi

            ACTION=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action','error'))" 2>/dev/null)
            MATCH=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('match_percent',0))" 2>/dev/null)
            VERDICT=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','UNKNOWN'))" 2>/dev/null)
            SUMMARY=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary',''))" 2>/dev/null)

            echo "=== AADS Quality Gate (API) ==="
            echo "Project : $PROJECT / $CHANNEL / $VIDEO_ID"
            echo "Verdict : $VERDICT ($MATCH%)"
            echo "Summary : $SUMMARY"
            echo "Action  : $ACTION"
            echo "==============================="

            case "$ACTION" in
                publish)    exit 0 ;;
                ceo_review) exit 2 ;;
                re-render|reject) exit 3 ;;
                *) echo "ERROR: 예상치 못한 응답 — $RESULT" >&2; exit 1 ;;
            esac
        fi
        exit $EXIT_CODE
        ;;

    benchmark)
        VIDEO_PATH=$1; PROJECT=${2:-shortflow}; CHANNEL=$3
        if [ -z "$VIDEO_PATH" ] || [ -z "$CHANNEL" ]; then
            echo "Usage: $0 benchmark <video_path> <project> <channel>" >&2
            exit 1
        fi

        echo "[Quality Gate] 벤치마크 등록: $VIDEO_PATH → $PROJECT/$CHANNEL"

        if python3 "$AUDITOR" benchmark "$VIDEO_PATH" \
            --project "$PROJECT" \
            --channel "$CHANNEL"; then
            echo "벤치마크 등록 완료"
            exit 0
        else
            EXIT_CODE=$?
            # fallback: extract-spec API 직접 호출
            if [ "$EXIT_CODE" -eq 127 ] || [ "$EXIT_CODE" -eq 126 ]; then
                echo "[Quality Gate] auditor.py 없음, extract-spec API 직접 호출..." >&2
                RESULT=$(curl -s -X POST "$AADS_URL/visual-qa/extract-spec" \
                    -H "Content-Type: application/json" \
                    -H "X-Monitor-Key: ${AADS_MONITOR_KEY}" \
                    -d "{\"project_id\":\"$PROJECT\",\"channel_name\":\"$CHANNEL\",\"video_path\":\"$VIDEO_PATH\"}" \
                    --max-time 60)
                if [ -z "$RESULT" ]; then
                    echo "ERROR: extract-spec 응답 없음" >&2
                    exit 1
                fi
                echo "벤치마크 API 응답: $RESULT"
                exit 0
            fi
            exit $EXIT_CODE
        fi
        ;;

    *)
        echo "알 수 없는 명령: $CMD" >&2
        usage
        ;;
esac
