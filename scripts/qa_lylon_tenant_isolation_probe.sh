#!/usr/bin/env bash
# 라일론 신규 가입자 테넌트 격리 재현 프로브 (읽기 검증용, 2026-09-19)
# 신규 계정 1개를 만들고 그 토큰으로 오비서 데이터 API 를 호출해
# 타 사업자(business_id) 데이터가 보이는지 실측한다.
set -u

BASE="https://fb.newtalk.kr"
STAMP=$(date +%s)
EMAIL="lylon-iso-${STAMP}@lylon.test"
PASS="LylonProbe!${STAMP}"

echo "=== 1. 신규 가입 (라일론) ==="
echo "email=${EMAIL}"
REG=$(curl -s -X POST "${BASE}/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASS}\",\"name\":\"라일론격리테스트\",\"organization_name\":\"라일론테스트상사\",\"consents\":[{\"consent_key\":\"terms\",\"version\":\"1.0\",\"agreed\":true},{\"consent_key\":\"privacy\",\"version\":\"1.0\",\"agreed\":true},{\"consent_key\":\"age14\",\"version\":\"1.0\",\"agreed\":true}]}")
echo "${REG}" | head -c 400
echo

TOKEN=$(echo "${REG}" | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
if [ -z "${TOKEN}" ]; then
  echo "!! 토큰 없음 — 가입 실패. 중단."
  exit 1
fi
echo "token_len=${#TOKEN}"

AUTH="Authorization: Bearer ${TOKEN}"

echo
echo "=== 2. 내 신원 (/auth/me) ==="
curl -s -H "${AUTH}" "${BASE}/api/v1/auth/me" | head -c 400
echo

echo
echo "=== 3. 오비서 세션 권한 (/session) ==="
curl -s -H "${AUTH}" "${BASE}/api/v1/yeoljeong-finance/session" | head -c 500
echo

echo
echo "=== 4. 플랫폼 계정 목록 (/accounts) — 타 사업자 노출 여부 ==="
ACC=$(curl -s -H "${AUTH}" "${BASE}/api/v1/yeoljeong-finance/accounts")
echo "응답길이=${#ACC}"
echo "business_id 출현:"
echo "${ACC}" | grep -o '"business_id":"[^"]*"' | sort | uniq -c

echo
echo "=== 5. 배달 매출 (/sales) — 타 사업자 노출 여부 ==="
SAL=$(curl -s -H "${AUTH}" "${BASE}/api/v1/yeoljeong-finance/sales")
echo "응답길이=${#SAL}"
echo "business_id 출현:"
echo "${SAL}" | grep -o '"business_id":"[^"]*"' | sort | uniq -c

echo
echo "=== 6. 직원 가입요청 (/employees/join-requests) ==="
JR=$(curl -s -H "${AUTH}" "${BASE}/api/v1/yeoljeong-finance/employees/join-requests")
echo "응답길이=${#JR}"
echo "${JR}" | grep -o '"employee_email":"[^"]*"' | sort | uniq -c | head -10

echo
echo "=== 7. 계약서 (/contracts) — 타인 개인정보 노출 여부 ==="
CT=$(curl -s -H "${AUTH}" "${BASE}/api/v1/yeoljeong-finance/contracts")
echo "응답길이=${#CT}"
echo "${CT}" | grep -o '"employee_name":"[^"]*"' | sort | uniq -c | head -10

echo
echo "=== 프로브 종료 (계정 ${EMAIL} 은 검증 흔적으로 남김) ==="
