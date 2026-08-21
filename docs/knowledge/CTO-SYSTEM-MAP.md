# CTO-SYSTEM-MAP: AADS 시스템 전체 아키텍처 지도

_최종 갱신: 2026-08-21 (실측)_

> 수치와 운영 상태는 별도 표기가 없는 한 2026-08-21 실행 환경에서 조회했다. 출처 태그: `[SRC:CODE]` 저장소 코드 정적 조회, `[SRC:DOCKER]` `docker ps`, `[SRC:DB]` PostgreSQL SELECT, `[SRC:DOC]` 관련 설계 문서.

## 1. 시스템 경계와 실행 흐름

```text
CEO / Dashboard → FastAPI (app/main.py)
  → /api/v1/* API·채팅·운영·관리·Runner
  → services (채팅·도구·모델·Runner·품질·메모리)
  → core (DB pool·인증·프롬프트·프로젝트 정규화)
  → PostgreSQL + pgvector / Redis
  → LiteLLM 프록시(코드 설정) → 외부 LLM
```

## 2. 인프라: 실측 컨테이너와 포트

| 컨테이너 | 이미지 | 호스트 포트 | 상태/역할 |
|---|---|---:|---|
| `aads-server` | `aads-server-aads-server` | `127.0.0.1:8100→8080` | healthy, FastAPI blue |
| `aads-server-green` | `aads-server-aads-server-green` | `127.0.0.1:8102→8080` | healthy, FastAPI green |
| `yeoljeong-finance` | `aads-server-yeoljeong-finance` | `127.0.0.1:8110→8080` | healthy, 금융 부속 API |
| `aads-dashboard` | `aads-server-aads-dashboard` | `127.0.0.1:3100→3100` | healthy, Next.js |
| `aads-dashboard-green` | `aads-server-aads-dashboard-green` | `127.0.0.1:3101→3101` | healthy, standby |
| `aads-postgres` | `pgvector/pgvector:pg15` | `0.0.0.0:5433→5432` | healthy, PostgreSQL/pgvector |
| `aads-redis` | `redis:7-alpine` | 내부 `6379` | healthy, 캐시/락/스트림 |
| `aads-searxng` | `searxng/searxng:latest` | `0.0.0.0:8888→8080` | healthy, 메타검색 |
| `aads-socket-proxy` | `tecnativa/docker-socket-proxy:latest` | 내부 `2375` | Docker API 제한 프록시 |
| `aads-nginx` | `nginx:alpine` | 호스트 공개 진입점 | reverse proxy |
| `antigravity-test` | `d764629ce0dd` | 없음 | 테스트 컨테이너 |

`aads-litellm`은 현재 `docker ps` 실측 목록에 없었다. 애플리케이션과 Compose에는 `aads-litellm:4000` 프록시 설정이 남아 있으므로, LLM 프록시의 설정 존재와 현재 컨테이너 실행을 구분한다. `[SRC:DOCKER] [SRC:CODE:docker-compose.prod.yml, app/services/model_selector.py]`

Blue-Green은 API `8100/8102`, Dashboard `3100/3101` 두 슬롯을 유지하고 nginx upstream을 전환하는 구조다. 배포 진입점은 `deploy.sh bluegreen`이다. `[SRC:DOCKER] [SRC:CODE:deploy.sh, docker-compose.prod.yml]`

## 3. API 라우터 실측

- `app/api/`: Python 파일 68개(백업 `.bak`, 검증 `.verify` 제외), `APIRouter` 정의 파일 65개. `[SRC:CODE:find, rg]`
- `app/routers/`: Python 파일 3개(`__init__.py`, `agent_vault.py`, `chat.py`), `APIRouter` 정의 2개. `[SRC:CODE:find, rg]`
- `app/main.py`: `include_router` 활성 등록 64개. 주석 처리된 legacy CEO chat 등록 1개까지 텍스트상 65개다. `[SRC:CODE:app/main.py:1991-2055]`

등록 prefix는 다음과 같다.

| 등록 prefix | 등록 범위 |
|---|---|
| `/api/v1` | health, projects, chat/stream, auth/context, memory, ops, admin, governance, QA, Runner, LLM, 파일/문서 외 다수 |
| `/api/v1/documents` | documents |
| `/api/v1/image` | image |
| `/api/v1/fact-check` | fact-check |
| `/api/v1/agenda` | agenda |
| `/api/v1/review` | code review router의 내부 prefix와 결합 |
| `/api/v1/braming` | braming router 자체 prefix |
| `/api/v1/local` | local media router 자체 prefix |
| `/pc-ollama` | PC Ollama bridge |

세부 라우터 내부 prefix(`/voice`, `/external/chat`, `/browser-bridge`, `/kakao-bot`, `/llm-keys`, `/llm-models`, `/agent-vault`, `/browser-tasks` 등)는 `/api/v1` 등록 prefix와 결합된다. `[SRC:CODE:app/main.py, app/api/*.py, app/routers/*.py]`

## 4. 코드 모듈 실측

| 영역 | Python 파일 수 | 핵심 모듈 |
|---|---:|---|
| `app/services/` | 135 | `chat_service.py`, `pipeline_runner_service.py`, `model_selector.py`, `tool_registry.py`, `prompt_compiler.py`, `agent_orchestrator.py`, `unified_healer.py` |
| `app/core/` | 21 | `anthropic_client.py`, `auth_provider.py`, `db_pool.py`, `project_config.py`, `memory_recall.py`, `memory_gc.py`, `feature_flags.py` |
| `app/routers/` | 3 | `chat.py`, `agent_vault.py` |

채팅 핵심 경로는 `app/routers/chat.py`/`app/services/chat_service.py` → `context_builder.py` → `PromptCompiler` → 모델 라우터다. 도구 호출은 `tool_registry.py`와 `tool_executor.py`가 담당한다. `[SRC:CODE:find, app/services, app/core, app/routers]`

## 5. 인증과 LLM 라우팅

인증 호출의 중앙 경로는 `app/core/anthropic_client.py`의 `call_llm_with_fallback()`이다. 계정 선택은 `app/core/auth_provider.py`에서 OAuth 인증 토큰 1순위, 설정된 fallback 2순위, 이후 Gemini LiteLLM fallback 순서로 관리한다. 외부 Gemini/DeepSeek 등은 LiteLLM 프록시 경로를 사용하도록 모델 라우팅이 구성되어 있다. `[SRC:CODE:app/core/anthropic_client.py, app/core/auth_provider.py, app/services/model_selector.py]`

```text
요청 → intent_router / model_selector → OAuth 계정 선택
     → Claude 중앙 fallback 또는 LiteLLM proxy
     → response validator / critic / 비용 기록
```

## 6. Pipeline Runner와 Blue-Green 배포

```text
pipeline_runner_submit → pipeline_jobs INSERT
  → claim/dedup/dependency/work-lock → Claude Code 작업 실행
  → code_reviewer 검수 → awaiting_approval → CEO approve
  → 프로젝트별 promote/deploy → AADS: deploy.sh bluegreen
```

- API: `app/api/pipeline_runner.py` (`/api/v1/pipeline/jobs`, 상태·승인·batch·lock status). `[SRC:CODE]`
- 오케스트레이터: `app/services/pipeline_runner_service.py`; DB job, 의존성 cascade, 중복 방지, 동시성 lock, 결과 수거를 담당한다. `[SRC:CODE]`
- AADS 배포는 blue/green 슬롯 전환과 standby 동기화를 포함한다. 승인 전에는 배포하지 않는 `awaiting_approval` 경계가 핵심이다. `[SRC:CODE:app/services/pipeline_runner_service.py, deploy.sh]`
- DB 실측 `pipeline_jobs`: 572행, `deploy_history`: 51행. `[SRC:DB:SELECT count(*)]`

## 7. 프롬프트 거버넌스

`PromptCompiler`가 대화의 base prompt 뒤에 활성 `prompt_assets`를 L1 Global → L2 Project → L3 Role → L4 Intent → L5 Model 순서로 조립하고, `compiled_prompt_provenance`에 적용 결과와 hash/문자 수를 기록한다. `[SRC:CODE:app/services/prompt_compiler.py, docs/knowledge/5-LAYER-PROMPT-GOVERNANCE.md]`

| 테이블 | 행 수 |
|---|---:|
| `prompt_assets` | 137 |
| `compiled_prompt_provenance` | 13,778 |
| `role_profiles` | 28 |
| `session_blueprints` | 1 |
| `llm_models` | 498 |
| `llm_api_keys` | 13 |

`prompt_assets`의 scope/priority/enabled 조건과 provenance를 함께 확인해야 실제 적용 여부를 판단할 수 있다. `[SRC:DB:SELECT count(*), 5-LAYER-PROMPT-GOVERNANCE.md]`

## 8. 프로젝트 별칭 레이어

`app/core/project_config.py`가 프로젝트 정규 키, 표시명, 별칭을 단일 맵으로 관리한다. `resolve_project()`는 대소문자 무시 완전일치·표시명·`[PROJECT] 표시명`을 정규 키로 변환하고, `normalize_project_label()`은 DB 저장 라벨을 정규화한다. `[SRC:CODE:app/core/project_config.py]`

| 정규 키 | 주요 별칭/표시명 | 서버 |
|---|---|---|
| AADS | `aads`, AADS 자율개발시스템 | contabo116 |
| KIS | `kis`, 자동매매, kis-autotrade | contabo14 |
| GO100 | `go100`, 백억이, 백억이투자분석 | contabo14 (KIS workdir 공유) |
| SF | `sf`, ShortFlow, 숏폼 | cafe24_114 |
| NTV2 | `ntv2`, NewTalk, newtalk-v2 | cafe24_114 |

실행 대상이 아닌 표시 전용 프로젝트(FOOD, NAS, CEO, WORK 등)는 별도 집합으로 구분한다. `[SRC:CODE:app/core/project_config.py]`

## 9. PostgreSQL 주요 테이블 및 행 규모

아래는 `pg_stat_user_tables.n_live_tup` 전체 목록과 주요 테이블 `SELECT count(*)`를 함께 확인한 결과다. 주요 표의 행 수는 같은 시점의 정확한 `count(*)` 기준이다.

| 도메인 | 테이블 | 행 수 |
|---|---|---:|
| 채팅 | `chat_messages` | 48,125 |
| 채팅 | `chat_sessions` | 198 |
| 채팅 | `chat_workspaces` | 57 |
| 채팅 | `chat_artifacts` | 25,686 |
| 채팅 | `chat_turn_executions` | 10,535 |
| 메모리 | `memory_facts` | 61,074 |
| 메모리 | `ai_observations` | 515 |
| 메모리 | `ai_meta_memory` | 959 |
| Runner | `pipeline_jobs` | 572 |
| Runner | `task_logs` | 185 |
| 운영 | `error_log` | 1,583 |
| 운영 | `deploy_history` | 51 |
| LLM | `llm_models` | 498 |
| LLM | `llm_api_keys` | 13 |
| 프롬프트 | `prompt_assets` | 137 |
| 프롬프트 | `compiled_prompt_provenance` | 13,778 |
| 품질 | `code_reviews` | 10,195 |
| 품질 | `response_critiques` | 3,373 |
| 미디어 | `media_generation_jobs` | 190,601 |

전체 사용자 테이블은 156개로 확인되었다. 대표 도메인에는 채팅·메모리·Runner·LLM·프롬프트·품질·비용·카카오봇·agent vault·열정국밥/금융 데이터가 포함된다. `[SRC:DB:pg_stat_user_tables, SELECT count(*)]`

## 10. 운영 확인 포인트

- API health/ops: `/api/v1/health`, `/api/v1/ops/health-check`.
- 활성 작업/충돌: `pipeline_jobs` 상태, dependency, work-lock, `chat_workspace_change_ledger`.
- LLM 장애: 인증 토큰 계정 상태, LiteLLM proxy 실행 여부, `llm_models` 활성 설정.
- 프롬프트 변경: `prompt_assets` scope/enabled/priority → 실제 `compiled_prompt_provenance`.
- 배포: active/standby 컨테이너 health와 nginx upstream 전환 상태.

## 관련 문서

- `docs/knowledge/AADS-3STEP-SYSTEM-INDEX.md` — 신규 러너 읽기 순서
- `docs/knowledge/5-LAYER-PROMPT-GOVERNANCE.md` — 프롬프트 거버넌스 상세
- `docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md` — Runner 상세
- `docs/BLUEGREEN_DEPLOY_SPEC.md` — Blue-Green 배포 상세
- `docs/HANDOVER.md` — 운영 인수인계 기록
