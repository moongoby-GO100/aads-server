# AADS LLM 모델 레지스트리

**마지막 업데이트:** 2026-04-23 18:25 KST | **버전:** v4.0 | **작성:** AADS-189B

---

## 목적

AADS 채팅창에 노출되는 모델 집합과 실제 실행 경로를 문서화합니다.
- 모델 목록의 기준은 `llm_models` 레지스트리와 `/api/v1/llm-models/providers/summary` 응답입니다.
- `llm_api_keys` 상태와 레지스트리 metadata를 함께 보고 실행 가능 모델과 실제 routing backend를 판단합니다.
- 본문의 과거 수기 모델 표는 역사적 참고 자료이며, 현재 authoritative source는 레지스트리 row입니다.

## 2026-04-23 레지스트리 기반 실행 경로 메타데이터

- 레지스트리 저장 방식
  - `llm_models.metadata.execution_backend`
  - `llm_models.metadata.execution_model_id`
  - `llm_models.metadata.execution_base_url`
  - 위 3개를 저장해 selector가 DB row를 보고 실제 실행 provider와 base URL을 결정한다.
- 기본 backend 분류
  - `openai_compatible_direct`: OpenAI, Groq, DeepSeek, OpenRouter, Qwen, Kimi, MiniMax
  - `claude_cli_relay`: Anthropic
  - `codex_cli`: Codex
  - `litellm_proxy`: Gemini
- 채팅 실행 경로
  - `app/services/model_selector.py`는 레지스트리 metadata를 보고 `openai_compatible_direct` 모델을 provider 직통 OpenAI-compatible endpoint로 우선 호출한다.
  - 정적 allowlist에 없는 모델도 `llm_models`에 active row와 direct metadata가 있으면 실행할 수 있다.
  - direct provider API 키는 DB의 provider 활성 키를 우선 사용하고, 없으면 환경변수로 폴백한다.
- 현재 범위
  - 이 단계는 “레지스트리 row가 있으면 실행 가능”까지다.
  - 공급사 catalog를 자동 수집해 `llm_models` row를 생성하는 단계는 아직 별도 작업이다.
- 2026-04-24 안정화
  - `llm_models.metadata`는 asyncpg row/JSONB 상태에 따라 dict가 아니라 JSON 문자열로 읽힐 수 있다.
  - `app/services/model_selector.py`와 `app/services/model_registry.py`는 metadata를 사용하기 전에 JSON object로 정규화해야 한다.
  - 이 정규화가 빠지면 `ValueError: dictionary update sequence element #0 has length 1; 2 is required`가 발생하며, 특정 모델이 아니라 채팅 공통 경로 전체가 무응답 상태가 될 수 있다.

## 2026-04-23 백엔드 레지스트리 1단계

- 모델 메타데이터 저장소: `llm_models`
- 키 변경 감사 로그: `llm_key_audit_logs`
- 계산 규칙:
  - `llm_api_keys`의 활성/우선순위/rate-limit 상태를 읽어 provider별 실행 가능 모델 집합 계산
  - 템플릿이 있는 provider만 자동 활성화
  - 템플릿이 없는 provider는 provider summary에서 `requires_admin_review=true`로 남기고 모델 자동 노출 금지
- 런타임 연결:
  - `app/services/model_selector.py`가 레지스트리 기반 실행 가능 모델을 우선 적용
  - 활성 모델 집합이 비어 있거나 레지스트리 조회에 실패하면 기존 하드코딩 세트로 폴백
- 운영 API:
  - `GET /api/v1/llm-models`
  - `GET /api/v1/llm-models/providers/summary`
  - `POST /api/v1/llm-models/sync`

---

## 아키텍처

AADS LLM 라우팅: 채팅AI `call_llm_with_fallback()`
- [A] DB `llm_api_keys` 조회 -> Fernet 복호화 -> provider 키 반환
- [B] Anthropic OAuth (채팅 메인): AUTH_TOKEN 1->2 폴백
- [C] LiteLLM Proxy (localhost:4000): Gemini 12 + DeepSeek 2 + Groq 8 + Alibaba 30 + Claude 10

## 현재 운영 기준

- 채팅창에 어떤 모델이 보이는지는 `llm_models`와 `/api/v1/llm-models/providers/summary` 응답이 기준이다.
- Settings의 `LLM 키 및 모델 레지스트리`는 위 레지스트리를 그대로 소비한다.
- 아래의 상세 모델 표와 과거 실측 수치는 역사적 참고 자료이며, 현재 authoritative source는 아니다.

---

## 전체 모델 목록 (62개, 실측 2026-04-05)

### [A] Anthropic OAuth 직접 (채팅 AI 메인)

| 모델명 | 폴백 | 상태 | 용도 |
|--------|------|:---:|------|
| claude-opus-4-7 | Token 1->2 | 429빈번 | 초고난도 |
| claude-sonnet-4-6 | Token 1->2 | 정상 | 중상급 |
| claude-haiku-4-5 | Token 1->2 | 정상 | 범용 기본 |

### [B] Gemini (12개)

2개 키 로드밸런싱: `newtalk 계정` + `aads 계정`

gemini-2.5-flash, gemini-2.5-flash-lite, gemini-2.5-pro, gemini-2.5-flash-image,
gemini-3-pro-preview, gemini-3-flash-preview, gemini-3.1-pro-preview, gemini-3.1-flash-lite-preview,
gemma-3-27b-it, gemini-flash-lite(alias), gemini-flash(alias), gemini-pro(alias)

### [B] DeepSeek (2개)

deepseek-chat, deepseek-reasoner

### [B] Groq (8개, 무료)

groq-llama-70b, groq-llama-8b, groq-llama4-maverick, groq-llama4-scout,
groq-qwen3-32b, groq-kimi-k2, groq-gpt-oss-120b, groq-compound

### [B] Alibaba DashScope (30개) - NEW v2.0

**범용 텍스트 (7):** qwen-turbo, qwen-turbo-latest, qwen-plus, qwen-plus-latest, qwen-max, qwen-max-latest, qwen-flash

**코딩 전용 (4):** qwen-coder-plus, qwen3-coder-plus(A/B 7.38점), qwen3-coder-flash, qwen3-coder-480b

**Qwen3 시리즈 (9):** qwen3-8b, qwen3-14b, qwen3-32b, qwen3-30b-a3b, qwen3-max, qwen3-235b, qwen3-235b-instruct, qwen3-235b-thinking, qwen3-next-80b

**Qwen3.5 (2):** qwen3.5-plus, qwen3.5-flash

**Qwen2.5 (1):** qwen2.5-72b-instruct

**추론 (1):** qwq-plus (reasoning_content 파싱 필요)

**Vision/멀티모달 (5):** qwen-vl-max, qwen-vl-plus, qwen3-vl-plus, qwen3-vl-235b, qwen-omni-turbo

**DashScope 호스팅 (1):** dashscope-deepseek-v3.2

### [B] Claude via LiteLLM (10개, 2키 폴백)

claude-sonnet, claude-opus, claude-haiku, claude-opus-4-7, claude-sonnet-4-6,
claude-haiku-4-5, claude-sonnet-4-5-20250514, claude-haiku-4-5-20251001,
claude-opus-4-7-20250610, claude-sonnet-4-6-20250610

---

## 키 관리

| 프로바이더 | 키 수 | 관리 방식 | 폴백 체인 |
|-----------|:----:|----------|----------|
| Anthropic | 2 | `llm_api_keys` 저장, Fernet 복호화 후 OAuth 토큰 반환 | `AUTH_TOKEN` -> `API_KEY_FALLBACK` -> Gemini LiteLLM |
| Gemini | 2 | `llm_api_keys` 저장, `newtalk`/`aads` 계정 로드밸런싱 | DB -> 캐시(300초) -> `.env` 폴백 |
| Alibaba | 1 | `llm_api_keys` 저장, LiteLLM 프록시 전용 | DB -> 캐시(300초) -> `.env` 폴백 |
| DeepSeek/Groq | 운영 구성 기준 단일 키 | LiteLLM 경유, 직접 REST API 호출 금지 | DB -> 캐시(300초) -> `.env` 폴백 |

관련 모듈: `app/core/llm_key_provider.py`, `app/core/credential_vault.py`, `app/core/anthropic_client.py`

---

## 폴백 체인

- Claude: sonnet -> opus -> haiku (양방향)
- Qwen: max -> plus -> turbo
- Coder: qwen3-coder-plus -> qwen3-coder-flash -> qwen-coder-plus

---

## DashScope 주의사항

1. Qwen3 계열: enable_thinking: false 필요 (비스트리밍)
2. qwq-plus: reasoning_content 필드에만 결과 반환
3. qwen-omni-turbo: max_tokens 최소 10
4. qwen3-235b-thinking: thinking 모드 기본, 토큰 소비 높음

---

## A/B 테스트 결과 (125회)

| 모델 | 평균 | 편차 | 추천 |
|------|:---:|:---:|:---:|
| claude-haiku | 7.74 | 0.89 | 우수 |
| qwen3-coder-plus | 7.38 | 1.01 | 양호 |
| deepseek-v3.2 | 6.79 | 1.79 | 저비용 |

---

## 비용 최적화

현재 00/월 -> 개선안 50/월 (-37.5%)
- Claude Max 1계정 00 + Alibaba 0 + Groq bash + DeepSeek bash

---

## 설정

- Config: /root/aads/aads-server/litellm-config.yaml -> /app/config.yaml
- 환경변수 폴백: ANTHROPIC_AUTH_TOKEN, ANTHROPIC_AUTH_TOKEN_2, GEMINI_API_KEY, GEMINI_API_KEY_2, DEEPSEEK_API_KEY, GROQ_API_KEY, ALIBABA_API_KEY

## 변경 이력

| 날짜 | 버전 | 변경 |
|------|------|------|
| 2026-04-20 | v3.0 | `llm_api_keys` DB 관리, Gemini 2키 로드밸런싱, Claude Opus 4.7 반영 |
| 2026-04-05 | v2.0 | Alibaba DashScope 30개 모델 추가 (총 62개) |
| 2026-04-05 | v1.0 | 초기 문서 작성 (32개) |

---

## 전체 모델 응답 테스트 결과 (v3.0, 2026-04-20 기준)

| 프로바이더 | 등록 | 정상 | 실패 | 비고 |
|-----------|:----:|:----:|:----:|------|
| **Alibaba DashScope** | 30 | 25 | 5 (thinking) | thinking 모델은 streaming 또는 enable_thinking=false로 사용 |
| **Gemini** | 12 | 12 | 0 | Pro/2.5-pro는 thinking 응답 (정상) |
| **Groq** | 8 | 7 | 1 | groq-llama4-maverick 404 (Groq 미제공) |
| **DeepSeek** | 2 | 0 | 2 | 402 잔액 부족 — 충전 필요 |
| **Claude** | 10 | — | — | OAuth 직접 경로 사용 |
| **합계** | **62** | **44+** | **8** | |

### Thinking 모델 사용법 (qwen3-235b, qwen3-32b, qwen3-14b, qwen3-8b, qwen3-30b-a3b)
- 비스트리밍: 요청에  추가
- 스트리밍: 로 호출하면 thinking 토큰 포함 정상 응답

### 변경 이력

| 날짜 | 버전 | 변경 |
|------|------|------|
| 2026-04-20 | v3.0 | DB 기반 키 관리와 Opus 4.7 모델명으로 현행화 |
| 2026-04-05 | v2.1 | 전체 62개 모델 응답 테스트 완료, thinking 모델 가이드 추가 |
| 2026-04-05 | v2.0 | Alibaba DashScope 30개 모델 LiteLLM 등록, 총 62개 |
| 2026-04-04 | v1.0 | 초기 등록 현황 문서 생성 (32개 모델) |
