# AADS 인증 체계 명세서 v2.0
작성: 2026-04-04 | AADS-204 P0 401 인증 장애 조치 후 갱신

---

## 1. 인증 원칙 (R-AUTH)

- **AADS는 Anthropic OAuth만 사용** — `sk-ant-oat01-...` 형식
- API Key(`sk-ant-api03-...`) 방식 **금지**
- 모든 LLM 호출은 `app/core/anthropic_client.py`의 `call_llm_with_fallback()` 경유
- Gemini/Qwen 등 외부 LLM은 **LiteLLM 프록시** 경유 (`http://aads-litellm:4000`)

---

## 2. OAuth 토큰 구조

| 변수명 | 용도 | 우선순위 |
|--------|------|:--------:|
| `ANTHROPIC_AUTH_TOKEN` | Claude OAuth 1순위 | 1 |
| `ANTHROPIC_AUTH_TOKEN_2` | Claude OAuth 2순위 (폴백) | 2 |
| `GEMINI_API_KEY` | Gemini 폴백 (LiteLLM 경유) | 3 |
| `ALIBABA_API_KEY` | Qwen 폴백 (LiteLLM 경유) | 4 |

---

## 3. 폴백 체인 (anthropic_client.py)

```
call_llm_with_fallback(model, ...)
  │
  ├─ [1] Claude OAuth Token 1 (ANTHROPIC_AUTH_TOKEN)
  │       x-api-key 헤더로 전송
  │       429/5xx → 재시도 2회 (3s, 12s)
  │
  ├─ [2] Claude OAuth Token 2 (ANTHROPIC_AUTH_TOKEN_2)
  │       동일 방식, Token 1 완전 소진 시
  │
  ├─ [3] Gemini 2.5 Flash (LiteLLM → gemini-2.5-flash)
  │       양쪽 Claude 소진/실패 시
  │
  └─ [4] Qwen-Turbo (LiteLLM → dashscope-intl)
          Background 서비스 전용 (직접 호출)
```

---

## 4. 헤더 방식 (중요)

| 토큰 종류 | 올바른 헤더 | 잘못된 헤더 |
|-----------|------------|------------|
| `sk-ant-oat01-...` (OAuth) | `x-api-key: sk-ant-oat01-...` | `Authorization: Bearer ...` → 401 |
| `sk-ant-api03-...` (API Key) | `x-api-key: sk-ant-api03-...` | 사용 금지 |

P0 장애 교훈 (04-02~04-03): ~/.claude/credentials.json이 존재하면 CLI가 자동으로
Bearer 헤더를 사용 → 401 반복. 반드시 credentials.json 삭제 상태 유지.

---

## 5. 3서버 인증 파일 구조

| 서버 | 역할 | 인증 설정 |
|------|------|----------|
| **68** (68.183.183.11) | 메인 서버 | `/root/aads/aads-server/current.env` |
| **211** | 러너/터미널 | 동일 토큰, CLAUDE_CODE_OAUTH_TOKEN export |
| **114** | 백업 | 동일 토큰, CLAUDE_CODE_OAUTH_TOKEN export |

공통 금지 사항:
- ~/.claude/credentials.json 존재 금지 (Bearer 헤더 강제 → 401)
- ANTHROPIC_API_KEY 환경변수 신규 설정 금지

---

## 6. Docker 컨테이너 환경변수 주입

docker-compose.prod.yml:
  aads-server:
    env_file: current.env   # ANTHROPIC_AUTH_TOKEN, ANTHROPIC_AUTH_TOKEN_2 포함
    environment:
      - ALIBABA_API_KEY (Qwen 연동용)

주의: .env 변경 후 반드시 docker compose up -d --no-build aads-server 로 재생성

---

## 7. Claude Max OAuth 사용량 한도

| 한도 | 값 | 비고 |
|------|-----|------|
| 5시간 윈도우 | ~10M tokens | 초과 시 429 |
| 주간 한도 | ~50M tokens | 초과 시 429 |
| 리셋 | 매 5시간/주 | anthropic-ratelimit-*-reset 헤더로 확인 |

429 발생 시 흐름:
Token1 429 → Token2 시도 → Token2 429 → Gemini 폴백 → Qwen 폴백

---

## 8. 장애 이력

| 날짜 | 장애 | 원인 | 조치 |
|------|------|------|------|
| 04-02~04-03 | 401 반복 | credentials.json 존재 → Bearer 헤더 | 3서버 credentials.json 삭제 |
| 04-03 | Gemini 폴백 무응답 | gemini-3.1-flash-lite-preview content=null | gemini-2.5-flash로 교체 (a713a55) |

---

## 9. 관련 파일

| 파일 | 역할 |
|------|------|
| app/core/anthropic_client.py | 중앙 LLM 클라이언트 (수정 시 CEO 승인 필요) |
| app/services/oauth_usage_tracker.py | OAuth 사용량 추적 |
| docs/AUTH_GUARD.md | 인증 핵심 파일 수정 체크리스트 |
| current.env | 실제 토큰 저장 (git 커밋 금지) |
| litellm-config.yaml | LiteLLM 모델 라우팅 (qwen-turbo, qwen-plus, gemini) |
