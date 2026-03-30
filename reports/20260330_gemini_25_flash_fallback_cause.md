# `[gemini-2.5-flash]` 폴백 원인 정리

**일시**: 2026-03-30 (KST)  
**범위**: CEO 채팅 스트림에서 어시스턴트 메타가 **`gemini-2.5-flash`**로 표시되는 경우(사용자가 Groq·OpenRouter 등을 골랐을 때)

---

## 1. 한 줄 요약

**의도한 1차 모델(Groq/DeepSeek/OpenRouter 경유 LiteLLM)이 실패하면**, `model_selector.call_stream`이 **자동으로 `gemini-2.5-flash`를 2차로 스트리밍**한다. 실패의 대표 원인은 **Groq 측 HTTP 429(TPM 한도 초과 등)**이며, **시스템·히스토리·도구 스키마까지 합친 요청 토큰이 커질수록** 재현된다.

---

## 2. 코드상 폴백 경로 (확정)

파일: `app/services/model_selector.py`

### 2.1 Groq / DeepSeek

```397:409:app/services/model_selector.py
    # Groq / DeepSeek 모델 → LiteLLM 경유 (OpenAI 호환, 실패 시 Gemini Flash 폴백)
    if model in _GROQ_MODELS or model in _DEEPSEEK_MODELS:
        _had_error = False
        async for event in _stream_litellm(model, system_prompt, messages, tools=tools):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"litellm_fallback: {model} failed, falling back to gemini-2.5-flash")
                break
            yield event
        if _had_error:
            async for event in _stream_litellm("gemini-2.5-flash", system_prompt, messages, tools=tools):
                yield event
        return
```

- 1차: `_stream_litellm` → 비-Claude는 **`_stream_litellm_openai`** (`/chat/completions`).
- 스트림 중 **`type: "error"`** 이벤트가 한 번이라도 나오면 `_had_error = True` → 로그 `litellm_fallback: ... gemini-2.5-flash` → **동일 메시지/도구로 `gemini-2.5-flash` 재요청**.

### 2.2 OpenRouter 계열

```411:428:app/services/model_selector.py
    # OpenRouter 모델 → LiteLLM 경유 (openrouter/ prefix 붙여서 전달, 실패 시 Gemini Flash 폴백)
    if model in _OPENROUTER_MODELS:
        ...
        async for event in _stream_litellm_openai(_or_model, system_prompt, messages, tools=tools):
            if event.get("type") == "error":
                _had_error = True
                logger.warning(f"openrouter_fallback: {model} ({_or_model}) failed, falling back to gemini-2.5-flash")
                break
            ...
        if _had_error:
            async for event in _stream_litellm("gemini-2.5-flash", system_prompt, messages, tools=tools):
                yield event
        return
```

- 실패 시에도 **동일하게 `gemini-2.5-flash`**로 폴백 (로그 키만 `openrouter_fallback`).

### 2.3 `type: "error"`가 나오는 조건 (`_stream_litellm_openai`)

- LiteLLM 응답 **`status_code != 200`** → 예: **429 rate limit**, 4xx/5xx, 본문에 Groq/LiteLLM 에러 JSON 일부가 로그에 포함됨.
- 요청 처리 중 **예외** → `yield {"type": "error", "content": str(e)}`.

핵심 코드:

```748:752:app/services/model_selector.py
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield {"type": "error", "content": f"LiteLLM error {resp.status_code}: {body.decode()[:200]}"}
                    return
```

---

## 3. 운영에서 관측된 대표 원인 (Groq)

상세 실측은 `reports/20260330_groq_failure_root_cause.md`와 동일.

| 항목 | 내용 |
|------|------|
| 증상 | Groq 모델 선택 후에도 UI 메타가 `[gemini-2.5-flash]` |
| 직접 원인 | Groq **`429`**, `rate_limit_exceeded`, **on_demand TPM 한도(예: 6000)** 대비 **단일 요청 토큰 과다(예: 13k+)** |
| 왜 토큰이 큰가 | 워크스페이스 **시스템 프롬프트**, **긴 맥락(메모리/히스토리)**, **`tools` 대량 포함**(도구 스키마) 등이 한 요청에 합산 |
| 키/네트워크 | 동일 키로 **짧은 요청은 200** → 키 누락이 아닌 **한도·페이로드** 이슈로 분류 |

브라우저 검증(`reports/20260330_TEST001_groq_free_browser_test.md`): 고맥락 세션에서 Groq 라벨 전부 `gemini-2.5-flash` 메타 → 위 경로와 일치.

---

## 4. 다른 Gemini 폴백과의 구분

| 상황 | 폴백 대상 | 사용자에게 보이는 힌트 |
|------|-----------|-------------------------|
| Groq/DeepSeek/OpenRouter LiteLLM 실패 | **`gemini-2.5-flash`** | 없음(메타만 실제 사용 모델 표시) |
| Claude CLI Relay·SDK 전 계정 소진 | **`gemini-3.1-flash-lite-preview`** | 스트림 앞에 `[Claude 장애 → Gemini 전환]` 델타 |
| Gemini primary 실패 | **Claude Haiku** | `gemini_fallback` 로그 |

따라서 **`[gemini-2.5-flash]` 메타만 보고** “Claude 전체 장애”로 오해하면 안 되고, **비-Claude LiteLLM 경로 실패**를 의미한다.

---

## 5. 권장 대응 (요약)

1. **입력 토큰 감소**: 맥락 압축, Compaction, 새 세션, 불필요 시 **도구 목록 축소**(Groq 경로에 대량 `tools` 실리지 않게).
2. **Groq 정책**: 콘솔에서 **티어/TPM 상향** 또는 요청 분할.
3. **가시성**: 폴백 시 UI에 “1차 모델 실패 → Gemini Flash 응답” 한 줄(선택).
4. **장애 분석**: 서버 로그에서 **`litellm_fallback:`** / **`openrouter_fallback:`** 직후 LiteLLM 본문(429 여부) 확인.

---

## 6. 변경 파일

- 본 문서 `reports/20260330_gemini_25_flash_fallback_cause.md` (신규)

**배포**: 문서만 — 앱 재시작 불필요.
