# Gemini·Groq 정상 작동 스모크 테스트 보고  
**일시:** 2026-03-30 KST

## 1. 요약

| 구분 | 결과 |
|------|------|
| LiteLLM (`/v1/chat/completions`, 호스트→`:4000`) | Gemini 5종 + Groq 4종 **전부 PASS** |
| AADS 컨테이너 → `aads-litellm:4000` | `gemini-2.5-flash`, `groq-llama-8b` **PASS** |

## 2. 호스트에서 LiteLLM 직접 호출

- **URL:** `http://127.0.0.1:4000/v1/chat/completions`
- **인증:** `Authorization: Bearer` + 마스터 키 (환경과 동일)
- **페이로드:** `messages: [{role:user, content:"Reply with exactly: OK"}]`, `max_tokens: 32`
- **성공 기준:** HTTP 200, JSON에 `choices[0].message.content` 존재, `error` 없음

| model (litellm alias) | 결과 |
|----------------------|------|
| gemini-2.5-flash | PASS |
| gemini-2.5-flash-lite | PASS |
| gemini-3-flash-preview | PASS |
| gemini-3-pro-preview | PASS |
| gemini-3.1-flash-lite-preview | PASS |
| groq-qwen3-32b | PASS (응답에 reasoning 블록이 앞에 붙을 수 있으나 API·본문 정상) |
| groq-kimi-k2 | PASS |
| groq-llama-70b | PASS |
| groq-llama4-scout | PASS |

## 3. AADS 서버 컨테이너 내부 경로

채팅과 동일하게 Docker 네트워크에서 LiteLLM으로 요청하는지 확인.

- `urllib.request` → `LITELLM_BASE_URL` + `/v1/chat/completions`
- **gemini-2.5-flash:** PASS (`OK`)
- **groq-llama-8b:** PASS (`OK`)

## 4. 적용·배포

- 별도 코드 변경 없음 (검증만).
- 프로덕션 채팅 UI E2E(로그인·SSE)는 본 스모크 범위에 포함하지 않음. UI 확인은 동일 모델 선택 후 한 턴 발화로 재확인 권장.

## 5. 후속

- [ ] 장시간·고토큰 시 Groq/Gemini 레이트 리밋 모니터링
- [ ] `groq-compound` 등 장시간 응답 모델은 타임아웃 별도 점검
