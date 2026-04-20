# AADS 인증 체계 명세서 v3.0
작성: 2026-04-20 | AADS-204 P0 401 인증 장애 조치 후 갱신 + AADS-LLM-KEY-DB 반영

---

## 1. 인증 원칙 (R-AUTH)

- **AADS는 Anthropic OAuth만 사용** — `sk-ant-oat01-...` 형식
- API Key(`sk-ant-api03-...`) 방식 **금지**
- API 키는 DB `llm_api_keys` 테이블에 암호화(Fernet) 저장, `.env`는 DB 장애 시 폴백으로만 사용
- 모든 LLM 호출은 `app/core/anthropic_client.py`의 `call_llm_with_fallback()` 경유
- Gemini/Qwen 등 외부 LLM은 **LiteLLM 프록시** 경유 (`http://aads-litellm:4000`)

---

## 2. OAuth 토큰 구조

| 변수명 | 용도 | 우선순위 | label |
|--------|------|:--------:|-------|
| `ANTHROPIC_AUTH_TOKEN` | Claude OAuth 1순위 | 1 | 기본 |
| `ANTHROPIC_AUTH_TOKEN_2` | Claude OAuth 2순위 (폴백) | 2 | 폴백 |
| `GEMINI_API_KEY_2` | Gemini 2차 키 (LiteLLM 경유) | 2 | `aads 계정` |
| `GEMINI_API_KEY` | Gemini 1차 키 (LiteLLM 경유) | 3 | `newtalk 계정` |
| `ALIBABA_API_KEY` | Qwen 폴백 (LiteLLM 경유) | 4 | 기본 |

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

## 5. LLM 키 DB 관리 (AADS-LLM-KEY-DB)

LLM 자격 증명은 중앙 DB `llm_api_keys` 테이블에서 관리하며, 저장 시 `app/core/credential_vault.py`가 Fernet으로 암호화한다.
런타임 조회는 `app/core/llm_key_provider.py`가 담당하며, 기본 흐름은 DB 조회 후 캐시(300초)를 거쳐 DB 장애 시에만 `.env` 폴백을 사용한다.

### `llm_api_keys` 테이블 구조 요약

| 컬럼 | 설명 |
|------|------|
| `provider` | `anthropic`, `gemini`, `alibaba` 등 프로바이더 식별자 |
| `key_name` | 환경변수형 키 이름 (`ANTHROPIC_AUTH_TOKEN`, `GEMINI_API_KEY_2`) |
| `encrypted_value` | Fernet 암호화 저장값 |
| `priority` | 조회/폴백 순서 |
| `label` | 계정 식별 라벨 (`newtalk 계정`, `aads 계정`) |
| `is_active` | 활성화 여부 |
| `updated_at` | 최종 갱신 시각 |

### 조회 우선순위

1. DB `llm_api_keys` 조회
2. 애플리케이션 캐시 재사용 (TTL 300초)
3. `.env` 폴백 사용 (DB 장애 또는 미등록 시)

### 관련 모듈

| 파일 | 역할 |
|------|------|
| `app/core/llm_key_provider.py` | provider별 키 조회, 우선순위 정렬, 캐시/폴백 처리 |
| `app/core/credential_vault.py` | Fernet 암복호화, DB 저장 전 보호 계층 |

### 키 등록 방법

- `scripts/seed_llm_keys.py` 실행으로 표준 시드 등록
- 운영 필요 시 `llm_api_keys` 직접 `INSERT` 가능

## 6. 3서버 인증 파일 구조

| 서버 | 역할 | 인증 설정 |
|------|------|----------|
| **68** (68.183.183.11) | 메인 서버 | `/root/aads/aads-server/current.env` |
| **211** | 러너/터미널 | 동일 토큰, CLAUDE_CODE_OAUTH_TOKEN export |
| **114** | 백업 | 동일 토큰, CLAUDE_CODE_OAUTH_TOKEN export |

공통 금지 사항:
- ~/.claude/credentials.json 존재 금지 (Bearer 헤더 강제 → 401)
- ANTHROPIC_API_KEY 환경변수 신규 설정 금지

---

## 7. Docker 컨테이너 환경변수 주입

docker-compose.prod.yml:
  aads-server:
    env_file: current.env   # ANTHROPIC_AUTH_TOKEN, ANTHROPIC_AUTH_TOKEN_2 포함
    environment:
      - ALIBABA_API_KEY (Qwen 연동용)

주의: .env 변경 후 반드시 docker compose up -d --no-build aads-server 로 재생성

---

## 8. Claude Max OAuth 사용량 한도

| 한도 | 값 | 비고 |
|------|-----|------|
| 5시간 윈도우 | ~10M tokens | 초과 시 429 |
| 주간 한도 | ~50M tokens | 초과 시 429 |
| 리셋 | 매 5시간/주 | anthropic-ratelimit-*-reset 헤더로 확인 |

429 발생 시 흐름:
Token1 429 → Token2 시도 → Token2 429 → Gemini 폴백 → Qwen 폴백

---

## 9. 장애 이력

| 날짜 | 장애 | 원인 | 조치 |
|------|------|------|------|
| 2026-04-20 | 키 관리 분산 | `.env` 중심 운영 한계 | `llm_api_keys` + Fernet + 300초 캐시 구조로 전환 |
| 04-02~04-03 | 401 반복 | credentials.json 존재 → Bearer 헤더 | 3서버 credentials.json 삭제 |
| 04-03 | Gemini 폴백 무응답 | gemini-3.1-flash-lite-preview content=null | gemini-2.5-flash로 교체 (a713a55) |

---

## 10. 관련 파일

| 파일 | 역할 |
|------|------|
| app/core/anthropic_client.py | 중앙 LLM 클라이언트 (수정 시 CEO 승인 필요) |
| app/core/llm_key_provider.py | LLM 키 조회/캐시/폴백 중앙화 |
| app/core/credential_vault.py | LLM 키 Fernet 암복호화 |
| app/services/oauth_usage_tracker.py | OAuth 사용량 추적 |
| docs/AUTH_GUARD.md | 인증 핵심 파일 수정 체크리스트 |
| current.env | 실제 토큰 저장 (git 커밋 금지) |
| litellm-config.yaml | LiteLLM 모델 라우팅 (qwen-turbo, qwen-plus, gemini) |
