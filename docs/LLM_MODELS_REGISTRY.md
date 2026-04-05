# AADS LLM 모델 레지스트리

**마지막 업데이트:** 2026-04-05 22:30 KST | **버전:** v2.1 | **작성:** AADS-012

---

## 목적

AADS 채팅창에 등록된 모든 LLM 모델을 문서화하고 버전관리합니다.
- 총 **62개 모델** 등록 (실측 확인)
- 5개 제공자: Anthropic, Google, DeepSeek, Groq, Alibaba DashScope

---

## 아키텍처

AADS LLM 라우팅: 채팅AI call_llm_with_fallback()
- [A] Anthropic OAuth (채팅 메인): AUTH_TOKEN 1->2 폴백
- [B] LiteLLM Proxy (localhost:4000): Gemini 12 + DeepSeek 2 + Groq 8 + Alibaba 30 + Claude 10

---

## 전체 모델 목록 (62개, 실측 2026-04-05)

### [A] Anthropic OAuth 직접 (채팅 AI 메인)

| 모델명 | 폴백 | 상태 | 용도 |
|--------|------|:---:|------|
| claude-opus-4-6 | Token 1->2 | 429빈번 | 초고난도 |
| claude-sonnet-4-6 | Token 1->2 | 정상 | 중상급 |
| claude-haiku-4-5 | Token 1->2 | 정상 | 범용 기본 |

### [B] Gemini (12개)

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

claude-sonnet, claude-opus, claude-haiku, claude-opus-4-6, claude-sonnet-4-6,
claude-haiku-4-5, claude-sonnet-4-5-20250514, claude-haiku-4-5-20251001,
claude-opus-4-6-20250610, claude-sonnet-4-6-20250610

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
- 환경변수: ANTHROPIC_API_KEY_1/2, GEMINI_API_KEY, DEEPSEEK_API_KEY, GROQ_API_KEY, ALIBABA_API_KEY

## 변경 이력

| 날짜 | 버전 | 변경 |
|------|------|------|
| 2026-04-05 | v2.0 | Alibaba DashScope 30개 모델 추가 (총 62개) |
| 2026-04-05 | v1.0 | 초기 문서 작성 (32개) |

---

## 전체 모델 응답 테스트 결과 (v2.1, 2026-04-05 실측)

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
| 2026-04-05 | v2.1 | 전체 62개 모델 응답 테스트 완료, thinking 모델 가이드 추가 |
| 2026-04-05 | v2.0 | Alibaba DashScope 30개 모델 LiteLLM 등록, 총 62개 |
| 2026-04-04 | v1.0 | 초기 등록 현황 문서 생성 (32개 모델) |
