# Groq 실패 원인 분석 (LiteLLM → Gemini 폴백)

**일시**: 2026-03-30 (KST)  
**증상**: 채팅에서 Groq(Qwen3 32B) 선택 후에도 말풍선 메타가 `[gemini-2.5-flash]`로 표시됨 (이전 브라우저 검증).

## 결론 (요약)

**Groq 측 `429 rate_limit_exceeded`** — 조직/티어의 **TPM(토큰/분) 한도 6000** 대비, 단일 요청이 **약 13k+ 토큰**을 요구해 거절됨.  
LiteLLM이 에러를 반환 → `model_selector.call_stream`의 Groq 분기에서 `_stream_litellm`이 **`type: error`** → **`litellm_fallback: ... falling back to gemini-2.5-flash`**.

원인은 API 키 누락이 아님(동일 키로 소형 요청은 200). **프롬프트·도구·히스토리 합산 토큰이 Groq 무료/온디맨드 한도를 초과**하는 것이 핵심이다.

## 근거

### 1) 재현 (aads-server 컨테이너 → LiteLLM → Groq)

소형 요청(도구 없음, 짧은 메시지):

- `POST .../chat/completions` `model=groq-qwen3-32b` → **HTTP 200** (정상).

대형 요청(실제 채팅과 유사하게 **시스템 프롬프트 대량 + `get_eager_tools()` 전체(39개) 도구**):

- **HTTP 429**, Groq 본문 예시:

```json
{
  "error": {
    "message": "Request too large for model `qwen/qwen3-32b` ... service tier `on_demand` on tokens per minute (TPM): Limit 6000, Requested 13829, please reduce your message size..."
  },
  "type": "tokens",
  "code": "rate_limit_exceeded"
}
```

→ **요청 토큰(13829) > 티어 한도(6000)** 로 거절.

### 2) 코드 경로

- `app/services/model_selector.py`: Groq/DeepSeek 실패 시 `gemini-2.5-flash`로 스트리밍 폴백.
- `app/services/chat_service.py`: `use_tools`가 꺼져 있어도 `get_eager_tools()`가 매 요청에 포함되는 경로가 있어, **비-Claude 모델에도 대량 `tools`가 실릴 수 있음** (도구 개수 39).

### 3) 현재 서비스 상태

- 동일 환경에서 **짧은 프롬프트 + Groq** 는 성공하므로, **장애가 아닌 한도·페이로드 설계 이슈**로 분류하는 것이 맞다.

## 대응 방안 (우선순위)

| 방안 | 설명 |
|------|------|
| A | Groq/Gemini 등 **OpenAI 호환 비-Claude** 호출 시 **`tools` 생략 또는 축소**(예: 도구 0, 또는 소수만). |
| B | 인텐트가 도구 불필요(`casual` 등)일 때 **시스템 프롬프트/컨텍스트 압축**으로 입력 토큰 감소. |
| C | Groq 콘솔에서 **Dev Tier/유료 티어**로 TPM 상향 (운영 정책에 따름). |
| D | 폴백 발생 시 UI에 **“Groq 한도 초과 → Gemini 응답”** 한 줄 표시 (사용자 혼란 감소). |

## 검증

- 컨테이너 내 `httpx`로 LiteLLM 호출 재현: 소형 200 / 대형(도구+긴 system) 429.

## 비밀정보

- API 키·조직 id는 본 보고서에 기재하지 않음. 로그 인용 시 마스킹 유지.
