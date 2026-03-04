#!/bin/bash
# ShortFlow cron에서 업로드 직전에 호출
# 사용: ./shortflow_quality_gate.sh /path/to/video.mp4 economy economy_20260304
#
# 반환 코드:
#   0: AUTO_PUBLISH — 업로드 진행
#   2: CONDITIONAL  — CEO 리뷰 대기
#   3: AUTO_REJECT  — 재렌더링 필요
#   1: ERROR        — 오류

VIDEO_PATH=$1
CHANNEL=$2
VIDEO_ID=$3
AADS_URL="https://aads.newtalk.kr/api/v1/visual-qa/quality-gate"

if [ -z "$VIDEO_PATH" ] || [ -z "$CHANNEL" ] || [ -z "$VIDEO_ID" ]; then
    echo "Usage: $0 <video_path> <channel> <video_id>" >&2
    exit 1
fi

RESULT=$(curl -s -X POST "$AADS_URL" \
  -H "Content-Type: application/json" \
  -H "User-Agent: curl/7.64.0" \
  -d "{\"project_id\":\"shortflow\",\"video_path\":\"$VIDEO_PATH\",\"video_id\":\"$VIDEO_ID\",\"channel_name\":\"$CHANNEL\",\"auto_correct\":true}")

if [ -z "$RESULT" ]; then
    echo "ERROR: AADS 서버 응답 없음 ($AADS_URL)" >&2
    exit 1
fi

ACTION=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('action','error'))" 2>/dev/null)
MATCH_PERCENT=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('match_percent',0))" 2>/dev/null)
VERDICT=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','UNKNOWN'))" 2>/dev/null)
SUMMARY=$(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('summary',''))" 2>/dev/null)

echo "=== ShortFlow Quality Gate 결과 ==="
echo "Video : $VIDEO_PATH"
echo "Channel: $CHANNEL"
echo "Video ID: $VIDEO_ID"
echo "Verdict: $VERDICT"
echo "Match %: $MATCH_PERCENT%"
echo "Summary: $SUMMARY"
echo "Action : $ACTION"
echo "==================================="

case $ACTION in
  publish)
    echo "AUTO_PUBLISH: 품질 통과 — 업로드 진행"
    exit 0
    ;;
  ceo_review)
    echo "CONDITIONAL: CEO 리뷰 대기 ($MATCH_PERCENT%)"
    echo "CORRECTIONS: $(echo $RESULT | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get("corrections","none"), ensure_ascii=False))' 2>/dev/null)"
    exit 2
    ;;
  re-render)
    echo "AUTO_REJECT: 품질 미달 — 재렌더링 필요 ($MATCH_PERCENT%)"
    echo "CORRECTIONS: $(echo $RESULT | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get("corrections",{}), ensure_ascii=False))' 2>/dev/null)"
    exit 3
    ;;
  reject)
    echo "AUTO_REJECT: auto_correct=false — 수동 검토 필요"
    echo "REASON: $(echo $RESULT | python3 -c 'import sys,json; print(json.load(sys.stdin).get("error",""))' 2>/dev/null)"
    exit 3
    ;;
  *)
    echo "ERROR: 예상치 못한 응답 — $RESULT" >&2
    exit 1
    ;;
esac
