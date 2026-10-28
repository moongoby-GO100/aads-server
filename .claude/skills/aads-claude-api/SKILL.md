---
name: aads-claude-api
description: "AADS 에서 Claude(Anthropic) 호출 코드를 새로 쓰거나 고칠 때 쓴다. 기존 call_llm_with_fallback 재사용 여부 확인, anthropic_client 경유, OAuth 토큰 폴백 순서, 모델 id·가격 최신값 확인, 스트리밍·도구 호출·캐싱 변경 시 테스트, 비용이 드는 검증의 승인 절차를 안내한다. 'Claude 호출 추가해줘', 'LLM 호출 코드 고쳐줘', '모델 바꿔줘', '프롬프트 캐싱 적용' 같은 요청에 쓴다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
executor: model-instructions
source: "https://github.com/anthropics/skills/tree/main/skills/claude-api"
license: Apache-2.0
adapted: 2026-10-28
---
# AADS Claude 호출 코드 작성 순서

Claude 를 부르는 코드는 인증·폴백·사용량 기록이 한곳에 모여 있다. 새로 SDK 를 만들면 그 보호를 전부 잃는다.
이 스킬은 작성 순서만 안내하며 호출 권한은 주지 않는다.

## 우선순위
이 스킬과 원본 claude-api 지침이 CLAUDE.md R-AUTH·R-KEY 와 다르면 **R-AUTH 가 이긴다.**
`ANTHROPIC_API_KEY` 를 요청하거나 새로 읽지 않는다. 키·토큰 값은 코드·로그·보고에 쓰지 않는다.

## 인증 원칙 (R-AUTH 요약, 원문은 CLAUDE.md)
- 인증은 Anthropic OAuth 토큰(`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN_2`)만 쓰고 `.env` 경유로만 주입된다.
- 앱 Python 코드에서 `os.getenv("ANTHROPIC_API_KEY")` 를 신규로 쓰지 않는다. 기존 레거시 참조(`app/core/auth_provider.py`, `app/config.py`)는 늘리지 않는다.
- 외부 모델(Gemini·DeepSeek·Qwen 등)은 LiteLLM 프록시(`LITELLM_BASE_URL`)로만 부른다. 직접 REST 호출 금지.
- 사용자 본인 키(BYOK)는 별개 기능이다. `user_id` 를 넘기면 `user_api_keys` 테이블의 키를 쓰는 경로(`_try_user_key_claude`)가 이미 있으니 환경변수 키로 흉내 내지 않는다.

## 작성 순서
1. **재사용부터 찾는다 (Grep).** `call_llm_with_fallback`, `call_llm_messages_with_fallback`, `call_background_llm` 의 호출처를 `Grep` 으로 보고, 하려는 일과 같은 호출이 이미 있으면 그것을 쓰거나 확장한다. 중앙 정의는 `app/core/anthropic_client.py` 다.
   - 단문 응답(평가·추출·분류): `call_llm_with_fallback` 이 텍스트(또는 전부 실패 시 `None`)를 돌려준다.
   - 도구 호출·멀티턴·서브에이전트: `call_llm_messages_with_fallback(**messages.create 인자)` 가 응답 객체를 돌려주고, 전부 실패하면 마지막 예외를 올린다.
   - 반환이 `None` 이거나 예외가 올 수 있다는 점을 호출부가 처리하는지 확인한다.
2. **직접 SDK 가 꼭 필요하면** (스트리밍처럼 위 래퍼가 못 하는 경우) 클라이언트를 `app/core/auth_provider.py` 의 `create_anthropic_client()` 로 만든다(`anthropic_client.get_client()` 도 같은 곳을 부른다). `AsyncAnthropic(...)` 를 직접 만들거나 토큰을 환경변수에서 읽지 않는다. 필요 이유를 보고에 적는다.
3. **폴백 순서를 보존한다.** 현재 코드 기준: (사용자 키, `user_id` 가 있을 때) → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_AUTH_TOKEN_2` → 운영 DB `llm_fallback_chains` 의 LiteLLM 저비용 체인. 토큰 순서는 `get_oauth_tokens_async()` 가 운영자 설정(쿨다운·비활성 슬롯)을 반영해 정한다.
   - 지시서나 오래된 프롬프트에 "Gemini 3순위" 라고 적혀 있어도 `anthropic_client.py` 에 "Gemini 3순위 폴백 제거(2026-09-24)" 주석이 있다. **문서보다 현재 코드를 따른다.**
   - 429 재시도·토큰 전환·사용량 기록(`log_usage`, `bg_llm_usage_log`)은 래퍼 안에 있다. 호출부에서 재시도 루프를 또 만들지 않는다.
4. **모델 id·가격·파라미터는 기억하지 않는다.** 모델 이름은 빨리 바뀐다. 쓰기 직전에 현재 값을 확인한다.
   - 사내 기준: `app/services/model_router.py`(역할별 매핑·가격 주석), `app/services/model_selector.py`, 그리고 `Grep` 으로 같은 용도 호출의 현재 `model=` 값.
   - 공식 값: https://docs.anthropic.com/ (모델 목록·가격·API 레퍼런스). 사내 값과 공식 값이 다르면 임의로 고르지 말고 보고한다.
   - 확인하지 못한 id 를 코드에 넣지 않는다.
5. **기능별 주의.**
   - 스트리밍: 긴 출력·큰 `max_tokens` 는 타임아웃 위험이 있다. 활성 SSE 를 끊지 않도록 배포는 `reload-api.sh` 규칙을 따른다(R-DOCKER).
   - 도구 호출: 도구 정의와 `tool_use`/`tool_result` 짝이 맞는지, 실패 시 예외를 올리는 래퍼를 쓰는지 확인한다.
   - 캐싱: `cache_control` 헬퍼는 `app/core/cache_config.py` 에 있다. 앞부분이 바뀌면 캐시가 무효가 되므로 고정 내용을 앞에 둔다. 효과는 `cache_read_input_tokens` 로 확인한다.
6. **테스트.** 변경 범위에 맞는 단위 테스트를 추가·수정하고 `bash scripts/run_unit_tests.sh tests/unit/test_tools_and_pipeline.py` 와 관련 파일(예: `tests/unit/test_anthropic_client_token_selection.py`, `tests/unit/test_bg_llm_primary.py`)을 실행해 종료코드 0 을 확인한다. 외부 호출은 mock 으로 대체한다. 테스트가 실패하면 삭제하지 말고 고친다.
7. **비용이 드는 검증은 승인 후.** 실제 모델을 부르는 확인(실호출 스모크, 대량 배치, 평가 루프)은 호출 수·모델·예상 비용을 적어 승인을 받은 뒤에만 한다. 승인 전에는 mock 테스트와 코드 읽기까지만 한다.

## 하지 않는 것
- `ANTHROPIC_API_KEY` 를 요구·export·예시로 쓰는 것 금지, 키를 소스·compose·yaml 에 쓰는 것 금지 (R-KEY).
- 원본 문서의 "API 키 우선, 없으면 로그인 안내" 절차를 따르는 것.
- 인증 핵심 파일(`auth_provider.py`, `model_selector.py`, `docker-compose.yml` 등) 변경을 이 스킬의 범위로 보는 것. 필요하면 별도 승인 절차(`ALLOW_AUTH_COMMIT`)를 사용자에게 안내한다.

## 보고 형식
`재사용한 함수 또는 직접 SDK 가 필요한 이유 / 모델 id 와 확인 출처 / 폴백 영향 / 실행한 테스트와 종료코드 / 승인 대기 중인 실호출 검증`.

## 승격 조건
현재 `status: candidate`. 아래를 모두 만족해야 `active` 로 올린다.
1. 실제 사용 증거 **2건** (세션 id 또는 러너 job_id). 시험용 호출은 증거로 세지 않는다.
2. 두 건 모두 `call_llm_with_fallback` 계열 재사용 또는 `create_anthropic_client` 경유로 끝났고 `ANTHROPIC_API_KEY` 신규 참조가 없음을 확인했다.
3. 본문의 파일·함수 이름이 현재 코드와 일치한다(Grep 으로 재확인).
4. 사람의 승인. 스킬이 스스로 상태를 바꾸지 않는다.

## 출처 및 변경 고지
- 출처: anthropics/skills 저장소의 `skills/claude-api` (https://github.com/anthropics/skills/tree/main/skills/claude-api).
- 라이선스: Apache License 2.0. 라이선스 전문은 위 저장소의 `LICENSE.txt` 에 있다.
- 변경 사실: 원문을 옮기지 않고 방법론(현재 값 확인 우선, 래퍼 재사용, 스트리밍·도구·캐싱 주의점)을 이해한 뒤 한국어로 새로 썼다.
  원본의 언어별 하위 폴더·스크립트·가격표·코드 예시는 가져오지 않았다.
  원본의 자격증명 절(`ANTHROPIC_API_KEY` 우선, 로그인 안내)과 Admin API 키 안내는 AADS R-AUTH·R-KEY 와 충돌하므로 모두 버리고,
  OAuth 토큰·`create_anthropic_client`·`call_llm_with_fallback`·LiteLLM 경유 원칙으로 대체했다. 2026-10-28 각색.
