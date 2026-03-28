# 긴급: OAuth 폴백 확장 + LiteLLM 호스트 기본값 (2026-03-28)

## 요약

1. **CEO Chat `_call_anthropic` / tools / execute / cross-project QA**  
   - `402`만 보던 것을 **`402`, `429`, `403`** 및 응답/본문에 **`limit`**(대소문자 무관) 포함 시 다음 OAuth로 재시도하도록 확장.  
   - `anthropic_client` ×2(동일 키) 제거 → **`get_oauth_tokens()`마다 `create_anthropic_client(token=…)`** 로 실제 1·2순위 토큰 순회.

2. **채팅 스트림 `_stream_anthropic` (`model_selector`)**  
   - 동일 **quota-class** 판별(`_quota_class_http_error`) + **`rotate_oauth_primary_fallback()`** 후 **`create_anthropic_client()`** 로 전역 `_anthropic`을 직접 Anthropic 클라이언트로 갱신(한도 시 2번째 토큰 사용).  
   - 재시도 허용 HTTP에 **`403`** 추가, `402`/quota-class 전용 백오프 유지.

3. **`auth_provider`**  
   - **`rotate_oauth_primary_fallback()`** 추가.

4. **LiteLLM 베이스 URL**  
   - 코드·compose 기본값을 **`http://aads-litellm:4000`**(또는 `model_router`만 `/v1` 포함)으로 통일.  
   - `docker-compose.prod.yml`의 `aads-server`에 **`LITELLM_BASE_URL` / `LITELLM_MASTER_KEY`** 명시.

## 변경 파일

- `app/api/ceo_chat.py`, `app/core/auth_provider.py`, `app/services/model_selector.py`
- `app/services/intent_router.py`, `deep_crawl_service.py`, `design_auditor.py`, `autonomous_executor.py`, `model_router.py`, `app/api/chat.py`
- `docker-compose.yml`, `docker-compose.prod.yml`, `.env.example`

## 검증

- `/usr/local/bin/python3.11 -m py_compile` 위 변경 모듈: **PASS**

## 배포

- 컨테이너 재기동 또는 `deploy.sh code`로 반영.  
- 기존 `.env`에 `LITELLM_BASE_URL`이 `litellm` 호스트를 가리키면 **`http://aads-litellm:4000`으로 수정** 권장.

## 체크

- [ ] 프로덕션에서 Claude 전량 실패 시 Gemini( LiteLLM ) 폴백 로그 확인  
- [ ] OAuth 토큰 1개만 있을 때 `rotate` 실패 로그만 나오고 무한 스위치 없는지 확인
