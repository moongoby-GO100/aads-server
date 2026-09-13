#!/usr/bin/env bash
# 교육자료 8건 출처 검수 패치를 원본(baseline)부터 다시 적용한다 (sources_p1, 2026-09-13).
#
#   BASELINE : origin/main 시점의 원본 8건 (바이트 동일 사본)
#   OUT      : 패치 결과를 쌓을 작업 디렉터리
#
# 패치 스크립트는 멱등하지 않으므로(치환 대상을 한 번 소비하면 두 번째엔 못 찾음)
# 항상 baseline 에서 새로 시작한다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASELINE="${BASELINE:-/tmp/edu-p1-baseline}"
OUT="${OUT:-/root/edusrc-p1-scratch/reports}"

FILES=(
  20260912_rag_vectordb_embedding_education.html
  20260912_mcp_tool_calling_education.html
  20260912_finetuning_lora_practice_education.html
  20260912_llmops_observability_education.html
  20260912_multimodal_ai_education.html
  20260912_ai_coding_agent_practice_education.html
  20260912_ai_product_prd_ax_education.html
  20260912_ai_cost_token_economics_education.html
)

mkdir -p "$OUT"
for f in "${FILES[@]}"; do cp "$BASELINE/$f" "$OUT/$f"; done
echo "· baseline 8건 복사 완료"

# 1) 인용 링크·검증 태그 CSS 주입
python3 "$HERE/inject_css.py" "$OUT"

# 2) RAG 본문 교정 (공식 단가·세대·라이선스)
AADS_REPORTS_DIR="$OUT" python3 "$HERE/apply_rag_edits.py"

# 3) 나머지 본문 교정 + 부록(검수 이력 + 1차 출처 표) + 목차 항목
AADS_REPORTS_DIR="$OUT" python3 "$HERE/apply_sources_p1.py"

# 4) 구조 검증
python3 "$HERE/../20260913_education_sources_p0/validate_education_html.py" "$OUT"/*.html
