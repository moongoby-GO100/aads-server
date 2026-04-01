# 인증 파일 수정 방지 대책 (AUTH_GUARD)
_작성: 2026-04-01 | AADS-CTO_

## 1. 인증 핵심 파일 목록 (절대 주의)

| 파일 | 역할 | 수정 위험도 |
|------|------|------------|
| `app/core/anthropic_client.py` | LLM 호출 중앙 클라이언트 | 🔴 최고 |
| `app/core/auth_provider.py` | OAuth 토큰 제공자 | 🔴 최고 |
| `app/auth.py` | JWT 미들웨어 | 🔴 최고 |
| `app/api/hot_reload.py` | 내부 재로드 API (X-Monitor-Key) | 🟡 중간 |
| `docker-compose.prod.yml` | 환경변수 주입 | 🔴 최고 |
| `scripts/claude_exec.sh` | OAUTH 토큰 CLI 주입 | 🔴 최고 |
| `scripts/pipeline-runner.sh` | 배포 토큰 주입 | 🟡 중간 |

## 2. 수정 전 필수 체크리스트

```
□ CEO 명시적 승인 확인 (R-AUTH)
□ 현재 토큰 상태 확인: docker exec aads-server env | grep ANTHROPIC
□ 수정 전 파일 백업: cp 파일명 파일명.bak_$(date +%Y%m%d)
□ 변경 최소화: 인증 로직 아닌 부분만 수정
□ 커밋 명령: ALLOW_AUTH_COMMIT=1 git commit -m "fix(auth): ..."
```

## 3. 수정 후 필수 검증

```bash
# 1) 문법 검증
python3 -c "import ast; ast.parse(open('수정파일.py').read()); print('OK')"

# 2) 인증 체계 검증
docker exec aads-server python3 -c "
from app.core.anthropic_client import call_llm_with_fallback
print('import OK')
"

# 3) API 헬스 체크
curl -s http://localhost:8100/api/v1/ops/health-check | python3 -m json.tool | grep -E "status|auth"

# 4) 토큰 형식 확인
docker exec aads-server env | grep ANTHROPIC | sed 's/=.*/=***/'
```

## 4. 인증 오류 발생 시 즉시 조치

```
증상: "AuthenticationError" / "401 Unauthorized" / 채팅 응답 없음
    ↓
Step 1: docker exec aads-server env | grep ANTHROPIC
    ↓
Step 2: 토큰 없거나 형식 이상 → .env 파일 확인
    ↓
Step 3: docker exec aads-server supervisorctl restart aads-api
    ↓
Step 4: curl -s http://localhost:8100/api/v1/ops/health-check
    ↓
Step 5: 복구 안 되면 CEO 즉시 보고
```

## 5. 금지 사항 (R-AUTH)

- `ANTHROPIC_API_KEY` 신규 사용 금지 (OAuth 전용)
- `os.getenv("ANTHROPIC_API_KEY")` 코드 추가 금지
- `anthropic.Anthropic()` 직접 초기화 금지 → `call_llm_with_fallback()` 사용
- `.env` 파일 git 커밋 절대 금지
- `--no-verify` 플래그 절대 금지

## 6. 일일 자동 체크

매일 09:05 KST 자동 실행:
- `_auth_daily_check()` (APScheduler, main.py)
- 텔레그램으로 TOKEN_1/TOKEN_2/LiteLLM 상태 보고
- 이상 시 CEO에게 즉시 알림

## 7. 변경 이력

| 날짜 | 내용 | 작성자 |
|------|------|--------|
| 2026-04-01 | 최초 작성 — 인증 파일 수정 방지 대책 | AADS-CTO |
