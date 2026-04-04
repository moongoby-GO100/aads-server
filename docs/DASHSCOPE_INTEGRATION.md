# Alibaba Cloud DashScope — AADS 통합 기술 명세
작성: 2026-04-04 | AADS-204 Background LLM 10종 qwen-turbo 전환

---

## 1. 개요

**목적**: Claude OAuth 한도 압박 완화 (Background 서비스 10종 → qwen-turbo 이전)
**효과**: OAuth 월 소비 48% 절감, CEO 채팅 응답 끊김 방지
**비용**: 월 ~$1.50 (90일 무료 쿼터 + 유료 $0.05~$0.40/1M tokens)

---

## 2. DashScope API 정보

| 항목 | 값 |
|------|-----|
| **엔드포인트** | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| **프로토콜** | OpenAI 호환 |
| **인증** | API Key (Authorization: Bearer) |
| **사용 가능 모델** | 139개 (Qwen, CodeQwen, QwQ 등) |

---

## 3. AADS 적용 모델

### Phase 1: Background 10종 (완료 ✅)

| 서비스 | 모델 | 호출/월 | 비용/월 |
|--------|------|:-------:|:-------:|
| compaction_service | qwen-turbo | 120 | $0.02 |
| memory_manager | qwen-turbo | 60 | $0.01 |
| fact_extractor | qwen-turbo | 40 | $0.01 |
| quality_feedback_loop | qwen-turbo | 30 | $0.01 |
| experience_learner | qwen-turbo | 30 | $0.01 |
| smart_search_service | qwen-turbo | 300 | $0.05 |
| kakaobot_ai (2곳) | qwen-turbo | 200 | $0.03 |
| code_reviewer | qwen-turbo | 50 | $0.01 |
| self_evaluator | qwen-turbo | 50 | $0.01 |
| response_critic | qwen-turbo | 40 | $0.01 |
| **소계** | | **920** | **$0.16** |

### Phase 2: 검수 폴백 (예정)

| 서비스 | 모델 | 용도 | 비용 |
|--------|------|------|------|
| pipeline_c.py | qwen-plus | Runner QA 검수 5순위 | $0.40/1M |
| intent_router | qwen-turbo | 복분류 폴백 | $0.05/1M |

### Phase 3: CEO 채팅 (미정)

- CEO 채팅은 **Claude Opus/Sonnet 100% 유지** (품질 우선)
- Qwen 사용 없음

---

## 4. 환경설정

### 4.1 .env 설정

```bash
# /root/aads/aads-server/current.env
ALIBABA_API_KEY={ALIBABA_API_KEY_PLACEHOLDER}
```

### 4.2 docker-compose.prod.yml

```yaml
aads-server:
  env_file: current.env
  environment:
    - ALIBABA_API_KEY=${ALIBABA_API_KEY:-}

aads-litellm:
  environment:
    - ALIBABA_API_KEY=${ALIBABA_API_KEY:-}
```

### 4.3 LiteLLM 설정 (litellm-config.yaml)

```yaml
model_list:
  - model_name: qwen-turbo
    litellm_params:
      model: openai/qwen-turbo
      api_base: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
      api_key: os.environ/ALIBABA_API_KEY
      
  - model_name: qwen-plus
    litellm_params:
      model: openai/qwen-plus
      api_base: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
      api_key: os.environ/ALIBABA_API_KEY
```

---

## 5. LLM 호출 경로

### Background 서비스 (직접 qwen-turbo)

```python
# app/services/smart_search_service.py (예)
from app.core.anthropic_client import call_llm_with_fallback

result = await call_llm_with_fallback(
    prompt=prompt,
    model="qwen-turbo",  # ← DashScope로 자동 라우팅
    max_tokens=200,
)
```

**anthropic_client.py 라우팅 로직:**
```python
def call_llm_with_fallback(model, ...):
    if model.startswith("qwen"):
        return await _call_dashscope(model, ...)  # DashScope 직접 호출
    elif not model.startswith("claude"):
        return await _call_litellm(model, ...)    # LiteLLM 경유 (Gemini 등)
    else:
        return await _call_anthropic(model, ...)  # Claude OAuth
```

### Runner 검수 폴백 (5순위)

```
Sonnet 4.6 → Gemini 2.5 Flash → qwen-plus (LiteLLM)
```

---

## 6. 무료 쿼터

| 항목 | 값 |
|------|-----|
| **기간** | Model Studio 활성화 후 90일 |
| **입력** | 모델당 100만 tokens/월 |
| **출력** | 모델당 100만 tokens/월 |
| **총액** | qwen 5종 기준 약 10M tokens 무료 |

**90일 후 월정액**: Savings Plan (12개월 40% 할인) 권장

---

## 7. 가격 정책

### 90일 이후 유료 가격

| 모델 | Input $/1M | Output $/1M | 추정 월비용 |
|------|:----------:|:-----------:|:----------:|
| qwen-turbo | $0.05 | $0.20 | ~$0.20 |
| qwen-plus | $0.40 | $1.20 | ~$2.40 |
| qwen3.5-plus | $0.40 | $2.40 | ~$3.00 |
| qwen3.6-plus | $0.50 | $3.00 | ~$4.50 |

**Savings Plan 적용 (12개월 40% 할인):**
- qwen-turbo: $0.12/월
- qwen-plus: $1.44/월
- **합계: ~$1.50/월**

---

## 8. 검증 체크리스트

### 배포 전

- [ ] ALIBABA_API_KEY `.env`에 설정
- [ ] docker compose up -d --no-build aads-server (컨테이너 재생성)
- [ ] litellm-config.yaml에 qwen-turbo/plus 등록
- [ ] aads-litellm 컨테이너도 ALIBABA_API_KEY 주입 확인

### 배포 후

- [ ] `docker exec aads-server python3 -c "import anthropic_client; print('OK')"` 문법 확인
- [ ] `curl -X POST http://localhost:8100/api/v1/chat -H "Content-Type: application/json" -d '{"model":"qwen-turbo","messages":[{"role":"user","content":"1+1"}]}'` qwen-turbo 직호출 테스트
- [ ] Background 서비스 로그 확인 (`docker logs aads-server | grep qwen`)
- [ ] 헬스체크 통과 확인

---

## 9. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| qwen-turbo 호출 실패 | ALIBABA_API_KEY 미주입 | docker-compose.prod.yml 확인 후 `docker compose up -d` |
| 401 Unauthorized | API 키 만료/비활성화 | Alibaba 계정 활성화 상태 확인 |
| 504 Timeout | API Base URL 오류 | dashscope-intl.aliyuncs.com 접근성 확인 |
| output_tokens=0 | max_tokens 지나 작음 | 서비스별 max_tokens 증가 |

---

## 10. 관련 파일

| 파일 | 수정 내용 |
|------|----------|
| app/services/smart_search_service.py | L74: claude-haiku → qwen-turbo |
| app/services/kakaobot_ai.py | L62, L112: claude-haiku → qwen-turbo |
| app/services/code_reviewer.py | L17: claude-haiku → qwen-turbo |
| app/services/self_evaluator.py | L23: 기본값 qwen-turbo |
| app/services/response_critic.py | L19: 기본값 qwen-turbo |
| litellm-config.yaml | qwen-turbo/plus 등록 |
| docker-compose.prod.yml | ALIBABA_API_KEY 환경변수 |

---

## 11. 참고 자료

- Alibaba Cloud Dashboard: https://home.console.aliyun.com
- DashScope API 문서: https://dashscope.console.aliyun.com
- LiteLLM 프록시: http://aads-litellm:4000
