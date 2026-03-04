#!/bin/bash
# AADS QA 클라이언트 — 211서버(ShortFlow), 116서버(뉴톡 V2) 공통 사용
# T-027 + T-028: 영상 품질 게이트 + 이미지 검수
#
# 사용법:
#   aads_qa_client.sh quality-gate <video_path> <project_id> <channel> <video_id>
#   aads_qa_client.sh image-qa <image_path> <project_id> <image_id> [category]
#   aads_qa_client.sh image-gate <image_path> <project_id> <image_id>
#   aads_qa_client.sh --help
#
# 반환 코드 (quality-gate):
#   0: AUTO_PUBLISH  — 업로드 진행
#   2: CONDITIONAL   — CEO 리뷰 대기
#   3: AUTO_REJECT   — 재렌더링 필요
#   1: ERROR         — 오류
#
# 반환 코드 (image-gate):
#   0: APPROVE — 이미지 통과
#   1: REJECT  — 이미지 미달 or 오류

AADS_URL="${AADS_QA_URL:-https://aads.newtalk.kr/api/v1/visual-qa}"
UA="curl/7.64.0"

usage() {
    cat <<EOF
AADS QA Client v1.1 (T-027+T-028)

명령:
  quality-gate <video_path> <project_id> <channel> <video_id>
      영상 품질 게이트 (ShortFlow 업로드 직전 호출)
      반환: 0=AUTO_PUBLISH, 2=CONDITIONAL, 3=AUTO_REJECT, 1=ERROR

  image-qa <image_path> <project_id> <image_id> [category]
      이미지 스코어카드 (6항목 60점)
      반환: JSON 스코어카드 출력

  image-gate <image_path> <project_id> <image_id>
      이미지 품질 게이트 (뉴톡 저장 직전 호출)
      반환: 0=APPROVE, 1=REJECT

환경변수:
  AADS_QA_URL   AADS 서버 base URL (기본: https://aads.newtalk.kr/api/v1/visual-qa)

예시:
  /root/aads_qa_client.sh quality-gate /data/sf/economy/latest.mp4 shortflow economy eco_$(date +%Y%m%d)
  /root/aads_qa_client.sh image-gate /tmp/product.jpg newtalk_v2 product_12345
EOF
    exit 0
}

CMD=$1
case $CMD in
    --help|-h|help)
        usage
        ;;

    quality-gate)
        VIDEO_PATH=$2; PROJECT=$3; CHANNEL=$4; VIDEO_ID=$5
        if [ -z "$VIDEO_PATH" ] || [ -z "$PROJECT" ] || [ -z "$CHANNEL" ] || [ -z "$VIDEO_ID" ]; then
            echo "Usage: $0 quality-gate <video_path> <project_id> <channel> <video_id>" >&2
            exit 1
        fi
        RESULT=$(curl -s -X POST "$AADS_URL/quality-gate" \
            -H "Content-Type: application/json" -H "User-Agent: $UA" \
            -d "{\"project_id\":\"$PROJECT\",\"video_path\":\"$VIDEO_PATH\",\"video_id\":\"$VIDEO_ID\",\"channel_name\":\"$CHANNEL\",\"auto_correct\":true}")
        if [ -z "$RESULT" ]; then
            echo "ERROR: AADS 서버 응답 없음" >&2
            exit 1
        fi
        ACTION=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action','error'))" 2>/dev/null)
        MATCH_PERCENT=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('match_percent',0))" 2>/dev/null)
        VERDICT=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','UNKNOWN'))" 2>/dev/null)
        SUMMARY=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary',''))" 2>/dev/null)
        echo "=== AADS Quality Gate ==="
        echo "Project : $PROJECT / $CHANNEL / $VIDEO_ID"
        echo "Verdict : $VERDICT ($MATCH_PERCENT%)"
        echo "Summary : $SUMMARY"
        echo "Action  : $ACTION"
        echo "========================="
        case $ACTION in
            publish)   exit 0 ;;
            ceo_review) exit 2 ;;
            re-render) exit 3 ;;
            reject)    exit 3 ;;
            *)         echo "ERROR: $RESULT" >&2; exit 1 ;;
        esac
        ;;

    image-qa)
        IMAGE_PATH=$2; PROJECT=$3; IMAGE_ID=$4; CATEGORY=${5:-"상품"}
        if [ -z "$IMAGE_PATH" ] || [ -z "$PROJECT" ] || [ -z "$IMAGE_ID" ]; then
            echo "Usage: $0 image-qa <image_path> <project_id> <image_id> [category]" >&2
            exit 1
        fi
        if [ ! -f "$IMAGE_PATH" ]; then
            echo "ERROR: 파일 없음: $IMAGE_PATH" >&2
            exit 1
        fi
        B64=$(base64 -w0 "$IMAGE_PATH")
        curl -s -X POST "$AADS_URL/image-qa" \
            -H "Content-Type: application/json" -H "User-Agent: $UA" \
            -d "{\"project_id\":\"$PROJECT\",\"images\":[{\"image_base64\":\"$B64\",\"image_id\":\"$IMAGE_ID\",\"category\":\"$CATEGORY\"}]}"
        ;;

    image-gate)
        IMAGE_PATH=$2; PROJECT=$3; IMAGE_ID=$4
        if [ -z "$IMAGE_PATH" ] || [ -z "$PROJECT" ] || [ -z "$IMAGE_ID" ]; then
            echo "Usage: $0 image-gate <image_path> <project_id> <image_id>" >&2
            exit 1
        fi
        if [ ! -f "$IMAGE_PATH" ]; then
            echo "ERROR: 파일 없음: $IMAGE_PATH" >&2
            exit 1
        fi
        B64=$(base64 -w0 "$IMAGE_PATH")
        RESULT=$(curl -s -X POST "$AADS_URL/image-quality-gate" \
            -H "Content-Type: application/json" -H "User-Agent: $UA" \
            -d "{\"project_id\":\"$PROJECT\",\"image_base64\":\"$B64\",\"image_id\":\"$IMAGE_ID\",\"min_score\":48}")
        ACTION=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action','error'))" 2>/dev/null)
        SCORE=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_score',0))" 2>/dev/null)
        echo "[$PROJECT/$IMAGE_ID] Score: $SCORE | Action: $ACTION"
        case $ACTION in
            approve) exit 0 ;;
            reject)  exit 1 ;;
            *)       echo "ERROR: $RESULT" >&2; exit 1 ;;
        esac
        ;;

    *)
        echo "알 수 없는 명령: $CMD" >&2
        usage
        ;;
esac
