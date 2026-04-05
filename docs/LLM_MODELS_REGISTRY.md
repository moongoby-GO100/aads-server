# AADS LLM 모델 레지스트리

**마지막 업데이트:** 2026-04-05 KST | **작성자:** CEO 지시 실행 (AADS-012)

---

## 🎯 목적

AADS 채팅창에 등록된 모든 LLM 모델을 문서화하고 버전관리합니다.
- 현재 활성 모델 목록
- 인증 경로 및 설정
- 성능 특성 (비용/속도/품질)
- 사용 가이드라인

---

## 📊 모델 아키텍처

```
AADS LLM 라우팅 구조

┌─────────────────────────────────────────────────────────────┐
│  [채팅 AI / Pipeline Runner]                               │
│  call_llm_with_fallback()                                   │
└────────────┬────────────────────────────────────────────────┘
             │
      ┌──────┴──────────────────────────────────────┐
      │                                             │
   ┌──▼──────────────────┐           ┌─────────────▼────────┐
   │ [A] Anthropic OAuth │           │ [B] LiteLLM Proxy    │
   │ (채팅 AI 메인)      │           │ (http://localhost:4000)
   └─┬────────────────────┘           └──┬──────────────────┘
     │ ANTHROPIC_AUTH_TOKEN            │ /chat/completions
     │ (1순위)                         │
     │ ANTHROPIC_AUTH_TOKEN_2          ├─ Gemini (Google)
     │ (2순위 폴백)                    ├─ DeepSeek
     │                                 ├─ Groq (무료)
     ├─ claude-opus-4-6                ├─ Alibaba DashScope
     ├─ claude-sonnet-4-6              │   (Qwen, 추론 모델)
     └─ claude-haiku-4-5               └─ Claude (2개 키 폴백)
```

---

## 🔐 인증 경로별 모델 목록

### [A] Anthropic OAuth 직접 (채팅 AI 메인)

**설정:** `anthropic_client.py` → `call_llm_with_fallback()`

| 모델명 | 토큰 | 상태 | 용도 |
|--------|------|:---:|------|
| **claude-opus-4-6** | Token 1→2 폴백 | ⚠️ 429 빈번 | 초고난도 분석 |
| **claude-sonnet-4-6** | Token 1→2 폴백 | ✅ 정상 | 중상급 작업 |
| **claude-haiku-4-5** | Token 1→2 폴백 | ✅ 정상 | 범용 (기본) |

**현재 상태:**
- Token 1: 월 쿼터 초과 → 429 에러 발생
- Token 2: 대체 토큰으로 폴백 작동

---

### [B] LiteLLM Proxy (다중 제공자)

**위치:** `http://aads-litellm:4000` (Docker 컨테이너)

#### ✅ Gemini (Google)
- gemini-2.5-flash, gemini-2.5-pro, gemini-2.5-flash-lite
- gemini-3-pro-preview, gemini-3.1-flash-lite-preview
- 비용: 저~중, 속도: 우수

#### ✅ DeepSeek
- deepseek-chat, deepseek-reasoner
- 비용: 초저, 크레딧: 402 유닛 보유

#### ✅ Groq (무료)
- llama-70b, llama-8b, qwen3-32b, kimi-k2
- 비용: 무료, 속도: 초고속 (100ms~500ms)

#### ✅ Alibaba DashScope (Qwen) — 139개 모델

**범용 모델:**
- qwen3-max, qwen3.5-plus, qwen3.6-plus
- qwen-turbo, qwen-plus, qwen-max

**코딩 전문:**
- qwen3-coder-plus ⭐ (A/B: 7.38점, 안정적)
- qwen3-coder-next, qwen3-coder-flash

**추론:**
- qwq-plus, deepseek-v3.2

**특징:** 무료(DashScope) 또는 월 $50 Coding Plan Pro

---

## 💰 비용 최적화

### 현재
- Claude Max 2계정: $400/월 (429 에러 + 과다 비용)

### 개선안 (총 $270/월, -32.5%)
| 항목 | 비용 | 역할 |
|------|------|------|
| Claude Max 1계정 | $200 | 고품질 (메인) |
| Alibaba Coding Plan | $50 | 코딩 특화 |
| Groq 무료 | $0 | 백업 |
| DeepSeek 크레딧 | ~$20 | 초저가 백업 |
| **합계** | **$270** | **절감: -$130** |

---

## 🎯 모델 선택 가이드

### A/B 테스트 결과 (125회, Blind Judge 채점)

| 모델 | 평균 | 편차 | 추천 |
|------|:---:|:---:|:---:|
| **claude-haiku** | **7.74** | 0.89 | ⭐⭐⭐ 우수 |
| qwen3-coder-plus | 7.38 | 1.01 | ⭐⭐ 양호 |
| deepseek-v3.2 | 6.79 | 1.79 | ⭐ 저비용 |

**결론:** Claude-Haiku가 종합 1위. 코딩은 Haiku 또는 Qwen3-Coder-Plus.

---

## 🔧 설정 정보

**파일:** `/app/litellm-config.yaml` (331줄, 60+ 모델)
**커맨드:** `litellm --config /app/config.yaml --port 4000`

**환경 변수:** .env.litellm
```
ANTHROPIC_API_KEY_1, ANTHROPIC_API_KEY_2
GEMINI_API_KEY, DEEPSEEK_API_KEY
GROQ_API_KEY, ALIBABA_API_KEY
LITELLM_MASTER_KEY=sk-litellm
```

---

## ✅ 실행 체크리스트

- [x] Alibaba DashScope 139개 모델 등록 확인
- [x] LiteLLM 컨테이너 재시작 (설정 적용)
- [x] LLM 문서 작성 (버전관리)
- [ ] Alibaba Coding Plan Pro 가입 ($50/월)
- [ ] Claude Max 2계정 → 1계정 축소
- [ ] 월말 비용 보고

---

**버전:** v1.0 | **관리자:** AADS PM | **라이선스:** Internal Use Only
