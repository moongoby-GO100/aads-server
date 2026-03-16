#!/bin/bash
# AADS 배포 전 검수 스크립트
# 사용법: bash scripts/pre_deploy_check.sh [file1.py file2.py ...]
# 인자 없으면 git diff로 변경된 .py 파일 자동 감지

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ERRORS=0
WARNINGS=0

# 검사 대상 파일 결정
if [ $# -gt 0 ]; then
    FILES="$@"
else
    FILES=$(git diff --name-only HEAD~1 HEAD -- '*.py' 2>/dev/null || git diff --name-only --cached -- '*.py' 2>/dev/null || echo "")
    if [ -z "$FILES" ]; then
        FILES=$(git diff --name-only -- '*.py' 2>/dev/null || echo "")
    fi
fi

if [ -z "$FILES" ]; then
    echo -e "${YELLOW}검사할 파일 없음${NC}"
    exit 0
fi

echo "=== AADS 배포 전 검수 ==="
echo "대상: $FILES"
echo ""

# Step 1: Python 구문 검사
echo "--- Step 1: 구문 검사 (ast.parse) ---"
for f in $FILES; do
    if [ -f "$f" ]; then
        if python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
            echo -e "  ${GREEN}✅${NC} $f"
        else
            echo -e "  ${RED}❌ SYNTAX ERROR${NC} $f"
            python3 -c "import ast; ast.parse(open('$f').read())" 2>&1 | head -3
            ERRORS=$((ERRORS + 1))
        fi
    fi
done
echo ""

# Step 2: ruff 정적 분석 (undefined name, unused var, redefined)
echo "--- Step 2: 정적 분석 (ruff) ---"
if command -v ruff &>/dev/null; then
    for f in $FILES; do
        if [ -f "$f" ]; then
            RUFF_OUT=$(ruff check --select F821,F841,F811,F401 "$f" 2>&1 || true)
            if [ -z "$RUFF_OUT" ] || echo "$RUFF_OUT" | grep -q "All checks passed"; then
                echo -e "  ${GREEN}✅${NC} $f"
            else
                echo -e "  ${YELLOW}⚠️${NC} $f"
                echo "$RUFF_OUT" | head -5
                WARNINGS=$((WARNINGS + 1))
            fi
        fi
    done
else
    echo -e "  ${YELLOW}⚠️ ruff 미설치 — skip${NC}"
fi
echo ""

# Step 3: Docker 컨테이너 내부 import 테스트 (실제 런타임 검증)
echo "--- Step 3: import 검증 (Docker aads-server) ---"
for f in $FILES; do
    if [ -f "$f" ] && [[ "$f" == app/* ]]; then
        # app/services/model_selector.py → app.services.model_selector
        MODULE=$(echo "$f" | sed 's|/|.|g' | sed 's|\.py$||')
        RESULT=$(docker exec aads-server python3 -c "import $MODULE; print('OK')" 2>&1 || true)
        if echo "$RESULT" | grep -q "OK"; then
            echo -e "  ${GREEN}✅${NC} import $MODULE"
        else
            # import 실패 원인 출력
            ERR_LINE=$(echo "$RESULT" | grep -E "Error|error" | tail -1)
            echo -e "  ${RED}❌${NC} import $MODULE"
            echo "     $ERR_LINE"
            ERRORS=$((ERRORS + 1))
        fi
    fi
done
echo ""

# Step 4: 핵심 함수 호출 테스트 (선택적)
echo "--- Step 4: 핵심 함수 존재 확인 ---"
CRITICAL_CHECKS=(
    "app.services.model_selector:call_stream"
    "app.services.chat_service:send_message_stream"
    "app.api.ceo_chat_tools:tool_read_remote_file"
    "app.api.ceo_chat_tools:tool_run_remote_command"
    "app.services.output_validator:validate_response"
)
for check in "${CRITICAL_CHECKS[@]}"; do
    MOD="${check%%:*}"
    FUNC="${check##*:}"
    RESULT=$(docker exec aads-server python3 -c "from $MOD import $FUNC; print('OK')" 2>&1 || true)
    if echo "$RESULT" | grep -q "OK"; then
        echo -e "  ${GREEN}✅${NC} $MOD.$FUNC"
    else
        ERR_LINE=$(echo "$RESULT" | grep -E "Error|error" | tail -1)
        echo -e "  ${RED}❌${NC} $MOD.$FUNC — $ERR_LINE"
        ERRORS=$((ERRORS + 1))
    fi
done
echo ""

# 결과 요약
echo "=== 검수 결과 ==="
if [ $ERRORS -gt 0 ]; then
    echo -e "${RED}❌ 실패: $ERRORS건 에러, $WARNINGS건 경고 — 배포 중단 권장${NC}"
    exit 1
elif [ $WARNINGS -gt 0 ]; then
    echo -e "${YELLOW}⚠️ 경고: $WARNINGS건 — 확인 후 배포${NC}"
    exit 0
else
    echo -e "${GREEN}✅ 전체 통과 — 배포 가능${NC}"
    exit 0
fi
