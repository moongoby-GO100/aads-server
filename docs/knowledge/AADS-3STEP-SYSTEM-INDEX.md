# AADS 3단계 시스템 파악 인덱스
최종 갱신: 2026-06-18

신규 러너/에이전트가 AADS 전체를 빠르게 파악할 때 사용하는 읽기 순서다.
목표는 backend, dashboard, DB, runner, prompt governance, deploy flow를 한 번에 연결해서 보는 것이다.

## 0. 작업 전 금지사항
- `docker build`, `docker compose`, `docker restart` 실행 금지
- `npm run build`, `npm start`, `next build` 실행 금지
- `supervisorctl`, `systemctl`, `service restart` 실행 금지
- `kill`, `pkill` 실행 금지
- `ANTHROPIC_API_KEY`를 코드에서 직접 참조하거나 새로 추가 금지
- Gemini/DeepSeek 같은 외부 LLM은 반드시 LiteLLM 프록시 경유, 직접 REST 호출 금지
- 중앙 LLM 호출은 `app/core/anthropic_client.py`의 `call_llm_with_fallback()`만 사용

## 1. 1단계: 전체 구조를 먼저 읽는다
읽을 파일:
- `docs/knowledge/CTO-SYSTEM-MAP.md`
- `docs/BLUEGREEN_DEPLOY_SPEC.md`
- `docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md`
- `docs/pipeline-runner/PIPELINE-RUNNER-API-REFERENCE.md`
- `docs/AUTH_SPEC_v2.md`

확인 포인트:
- backend, dashboard, DB, runner, deploy flow의 연결
- blue-green 배포와 inactive slot 전환 방식
- pipeline runner의 claim/execute/approve 흐름

## 2. 2단계: 운영 규칙과 prompt governance를 읽는다
읽을 파일:
- `CLAUDE.md`
- `docs/knowledge/AADS-KNOWLEDGE.md`
- `app/core/anthropic_client.py`
- `app/services/model_selector.py`
- `app/services/model_registry.py`
- `app/core/prompts/system_prompt_v2.py`
- `app/core/memory_recall.py`
- `app/services/response_completion_contract.py`
- `app/services/workspace_preloader.py`

확인 포인트:
- R-AUTH 우선순위와 OAuth 토큰 폴백
- prompt governance, memory injection, completion guard
- 모델 라우팅과 LiteLLM/Anthropic 경로 구분

## 3. 3단계: 현재 상태와 작업 맥락을 읽는다
읽을 파일:
- 루트 `HANDOVER.md`
- `docs/HANDOVER.md`
- `docs/reports/*.md`
- `docs/handover-notes/*.md`

확인 포인트:
- 최신 장애/정책/배포 상태
- 현재 dirty 범위와 작업 잔여사항
- 바로 이어서 처리해야 할 후속 작업

## 4. 핵심 경로 요약
### Backend
- `app/main.py`
- `app/routers/chat.py`
- `app/services/chat_service.py`
- `app/core/anthropic_client.py`
- `app/services/model_selector.py`
- `app/services/model_registry.py`
- `app/services/tool_registry.py`

### Dashboard
- 별도 레포: `/root/aads/aads-dashboard`
- `src/app/chat/page.tsx`
- `src/hooks/useChatSSE.ts`
- `src/services/chatApi.ts`
- 채팅 UI와 SSE, 모델 선택, 상태 표시를 먼저 본다

### DB
- `migrations/`
- `app/core/credential_vault.py`
- `app/core/llm_key_provider.py`
- `app/services/model_registry.py`
- 주요 도메인: `chat_*`, `llm_*`, `pipeline_*`, `memory_*`

### Runner
- `app/api/pipeline_runner.py`
- `app/services/pipeline_runner_service.py`
- `scripts/pipeline-runner.sh`
- `scripts/aads-pipeline-runner.service`

### Prompt governance
- `CLAUDE.md`
- `app/core/prompts/system_prompt_v2.py`
- `app/core/memory_recall.py`
- `app/services/workspace_preloader.py`
- `app/services/response_completion_contract.py`

### Deploy flow
- `docs/BLUEGREEN_DEPLOY_SPEC.md`
- `deploy.sh`
- `docker-compose.prod.yml`
- `scripts/reload-api.sh`

## 5. 빠른 체크리스트
1. `CLAUDE.md`
2. `docs/knowledge/CTO-SYSTEM-MAP.md`
3. `docs/knowledge/AADS-KNOWLEDGE.md`
4. `docs/AUTH_SPEC_v2.md`
5. `docs/BLUEGREEN_DEPLOY_SPEC.md`
6. `docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md`
7. 루트 `HANDOVER.md`
8. `docs/HANDOVER.md`
