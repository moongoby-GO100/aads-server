# Gemini 스위치·Groq 반영 점검 및 수정  
**일시:** 2026-03-29 KST

## 원인 (Gemini)

- 채팅 스트림은 `app/services/model_selector.py`의 `call_stream()`에서 `model_override`로 받은 ID가 **`_GEMINI_MODELS`에 없으면** `unknown_model_fallback`으로 **`claude-sonnet`으로 덮어씀**.
- 대시보드 `ModelSelector.tsx`에는 있으나 **`_GEMINI_MODELS`에 빠져 있던 ID:**
  - `gemini-3-pro-preview`
  - `gemini-2.5-pro`
  - `gemini-2.5-flash-image`
- → UI에서 위 모델을 고르면 **실제로는 Claude로 응답**하여 “제미니 스위치가 안 된다”로 보임.

## 수정

- `model_selector.py`: 위 3개를 `_GEMINI_MODELS` 및 `_COST_MAP`에 추가, thinking 플래그용 `_GEMINI_THINKING_MODELS`에 `gemini-3-pro-preview`, `gemini-2.5-pro` 반영.
- `get_model_for_override`에 이미 있던 **`groq-llama4-maverick`** 이 `_GROQ_MODELS`에 없어 동일 증상이 날 수 있어 **`_GROQ_MODELS`·`_COST_MAP`에 추가**.

## Groq 반영 확인

- **`litellm-config.yaml`**: `groq-*` 항목 및 `GROQ_API_KEY` 환경변수 사용 — 반영됨.
- **호스트 스모크**: `POST /v1/chat/completions` + `model: groq-qwen3-32b` 정상 응답.
- **Gemini 스모크**: 동 엔드포인트 + `gemini-2.5-flash` 정상 응답.

## 배포

- `aads-server` 컨테이너 **재시작** 후 반영 (볼륨 마운트 소스 수정 시에도 uvicorn reload 미사용이면 재시작 필요).

```bash
cd /root/aads/aads-server && docker compose restart aads-server
```

## 검증 제안

1. 채팅 UI에서 **Gemini 3 Pro Preview / 2.5 Pro / Flash Image** 선택 후 짧은 메시지 → 응답 메타에 선택 모델 표시 확인.
2. **Groq Qwen3 32B** 등 선택 후 응답 확인.
