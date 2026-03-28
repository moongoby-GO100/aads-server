# AADS-003 등 채팅 불능 의심 — 원인·조치 (2026-03-28)

## 현상

- `58bf1337…` 세션 포함 채팅창에서 **대화가 이어지지 않음**(긴급 보고).

## 원인 (코드)

1. **`model_selector.py` LiteLLM URL 이중 정의 불일치**  
   - `_LITELLM_URL`( `_get_anthropic_client()` · 전역 `_anthropic`)만 기본값 **`http://litellm:4000`**.  
   - `LITELLM_BASE_URL`(httpx `_stream_litellm_*`)은 기본 **`http://aads-litellm:4000`**.  
   - `LITELLM_BASE_URL` 환경변수가 비어 있거나 없으면 import 시 **서로 다른 호스트**로 고정됨 → SDK 경로와 REST 경로가 갈라져 간헐적/환경별 **연결 실패·스트림 중단** 가능.  
   - 세션 ID와 무관한 **전역 설정 버그**라 특정 워크스페이스만 터지는 것처럼 보일 수 있음(모델/폴백 경로에 따라).

2. **`_switch_oat_token()`**  
   - `create_anthropic_client()` 실패 시에도 **토큰 순서만 바뀐 채** `True`에 가까운 동작이 가능했음 → **조용한 상태 오류**.  
   - **조치**: 실패 시 `rotate_oauth_primary_fallback()` 한 번 더 호출해 **순서 롤백**, `return False`.

## 조치

- `LITELLM_BASE_URL` / `_LITELLM_URL` → **`_LITELLM_BASE_RESOLVED` 단일 소스**  
  - `(os.getenv("LITELLM_BASE_URL") or "").strip() or "http://aads-litellm:4000"`  
- `_RETRYABLE_STATUS`에 **403** 유지/명시.  
- 위 `_switch_oat_token` 롤백.

## 검증

- `python3.11 -m py_compile app/services/model_selector.py` **PASS**

## 배포

- `deploy.sh code` 또는 `aads-server` 재시작 후 동일 세션에서 송신 확인.

## 체크

- [ ] 컨테이너 내 `python3 -c "from app.services import model_selector as m; print(m.LITELLM_BASE_URL, m._LITELLM_URL)"` → **동일 문자열 출력**
