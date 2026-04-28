# AADS HANDOVER

## 현재 진행 상태 (2026-04-28)
- **LLM 최신모델 자동 업데이트 및 GPT-5.5 반영 완료**:
  - `migrations/059_llm_model_discovery.sql`로 `llm_models`에 discovery/execution/verification/pricing/capabilities 컬럼을 추가하고 `llm_model_discovery_runs` 이력 테이블을 도입했다.
  - `app/services/model_registry.py`가 OpenAI/Gemini/LiteLLM catalog를 운영 컨테이너에서 조회해 DB 레지스트리에 병합한다. 최종 startup 기준 OpenAI 115개, Gemini 50개, LiteLLM 76개 발견. Anthropic은 현재 OAuth 토큰만 있어 `missing_anthropic_api_key` skip으로 기록한다.
  - Codex CLI `gpt-5.5`를 `model_registry`, `model_selector`, `claude_relay_server.py`, `pipeline_runner_service.py`, `pipeline-runner.sh`, 대시보드 selector/settings에 반영했다.
  - 실제 Codex relay E2E: `/codex-stream` `model=gpt-5.5`가 `AADS_GPT55_OK`, `model: gpt-5.5`로 응답 확인.
  - API E2E: active 모델 140개, `codex:gpt-5.5`와 `openai:gpt-5.5` 모두 active 확인.
- **채팅 모델 상단고정 provider별 분리 완료**:
  - 원인: `chat_model_preferences`가 `model_id` 단일 PK라 `openai:gpt-5.5`와 `codex:gpt-5.5`가 `gpt-5.5`로 충돌했다.
  - `migrations/060_chat_model_preferences_provider_scope.sql`로 PK를 `preference_key`로 전환했다. 형식은 `provider:model_id`, 자동 라우팅은 `mixture`.
  - `app/api/llm_models.py`, `aads-dashboard/src/components/settings/LlmRegistryWorkspacePanel.tsx`, `aads-dashboard/src/app/chat/page.tsx`를 provider-qualified 기준으로 수정했다.
  - 최종 API 검증: `codex:gpt-5.5:true`, `openai:gpt-5.5:false`. 라우팅 검증: `openai:gpt-5.5 -> openai_compatible_direct`, `codex:gpt-5.5 -> codex_cli`.
  - 서버 `deploy.sh` 6단계 통과, 대시보드 blue-green 배포 및 프론트 QA 통과.
- **채팅 SSE 재진입 UX 3건 패치 완료** (b24b47f + 56ed27c):
  - **BUG #3**: `app/routers/chat.py` streaming-status DB fallback에서 `tool_count`/`last_tool`을 `tools_called` JSON에서 산출 (running/just_completed/placeholder 3분기). asyncpg가 jsonb를 str로 반환하는 케이스도 처리.
  - **Patch A** (`aads-dashboard/src/app/chat/page.tsx:1742`): `streaming-status` 응답의 `partial_content`/`tool_count`/`last_tool`을 즉시 `setStreamBuf`/`setToolStatus`로 주입. 진입 시 빈 버블 방지.
  - **Patch B** (`aads-dashboard/src/app/chat/page.tsx:1322`): `attachExecutionReplay`가 SSE 18종 모두 처리(이전 3종). `tool_use`/`tool_result`/`thinking`/`stream_start`/`stream_reset`/`yellow_limit`/`model_info`/`sdk_*`/`error` 핸들러 추가 — sendMessage 메인 루프와 동등.
  - **배포**: `docker compose build aads-dashboard` (image f9c82f89) → `up -d aads-dashboard` healthy. `bash scripts/reload-api.sh` 68개 모듈 재로드.
  - **푸시 확인**: `b24b47f` (aads-server main), `56ed27c` (aads-dashboard main) — 모두 origin 반영 완료.
  - **문서**: `docs/knowledge/SSE-STREAMING-ARCHITECTURE.md` v2.0 → **v2.1** 업데이트 (Layer 7: Re-attach Full SSE Replay 추가). `docs/chat/CHAT-CHANGELOG.md` 2026-04-28 항목 추가.
  - **별도 보고서**: `reports/20260428_session_fork_analysis.md` — 누적 4000건 세션 분기 권유 정밀 분석 + 개선안 5종.

## 현재 진행 상태 (2026-04-27)

- **5-Layer Prompt 시스템 마감 검증 (직접 작업)**:
  - **DB**: prompt_assets 6 컬럼(layer_id/role_scope/target_models/workspace_scope/intent_scope/model_variants) + 시드 10건 활성 — L1 글로벌 2건, L2 프로젝트 3건, L3 역할 2건, L4 인텐트 2건, L5 모델 1건. compiled_prompt_provenance 테이블 정상.
  - **백엔드**: PromptCompiler.compile()이 5축(workspace/intent/target_models/role_scope) 모두 SQL 필터로 처리. chat_service.py:3873에서 매 채팅 턴 호출.
  - **API**: app/api/admin.py에 /admin/prompt-assets CRUD 5종(GET/POST/PUT/PATCH toggle/DELETE) + preview 완비.
  - **프런트**: aads-dashboard/src/app/admin/prompts/page.tsx(268줄) 5-Layer 카드/필터/편집/미리보기 UI. api.ts에 5종 메서드. Sidebar에 📝 Prompts 메뉴(/admin/prompts) 노출.
  - **provenance 0건 진단 패치**: chat_service.py PromptCompiler 호출부에 [PROMPT_COMPILER] 4단계 진단 로그(enter/compiled/recorded/failed) 추가, session_id를 str() 명시 캐스팅, record_prompt_provenance 실패를 별도 except로 분리. 다음 채팅 턴부터 compiled_prompt_provenance 적재 추적 가능.
  - **Hot-Reload**: scripts/reload-api.sh 62개 모듈 재로드 완료(10:49 KST), SSE 영향 없음.

최종 업데이트: 2026-04-24

## 현재 진행 상태 (2026-04-25)
- **2026-04-25 Governance v2.1 마감 (직접 작업)**:
  - **P0 temperature 배선 완료**: `model_selector.py`에 `contextvars` 기반 `_ctx_temperature`를 도입해 `call_stream()` → `_stream_litellm_anthropic` / `_stream_litellm_openai` / `_stream_cli_relay` 3개 LLM 경로 모두에 인텐트별 temperature를 전달한다. `resolve_intent_temperature()` → `intent_policies.temperature` DB 조회 → 하드코딩 맵 폴백 체인으로 작동. 실측 검증: greeting=0.1, strategy=0.15, code_task=0.15, casual=0.2.
  - **P0 W3 DB 마이그레이션 완료**: `scripts/migrations/20260424_governance_v2_1_w3.sql` 실행으로 `prompt_assets`, `prompt_asset_versions`, `session_blueprints`, `prompt_change_requests`, `cr_approvals`, `compiled_prompt_provenance` 6개 테이블 생성. `session_blueprints`에 `default.standard` 시드 삽입.
  - **P1 prompt_compiler 활성화**: W3 테이블 생성으로 `PromptCompiler.compile()` (chat_service.py L3873)이 실제 `prompt_assets` + `session_blueprints` DB 조회 경로로 작동 시작. `record_prompt_provenance()`로 `compiled_prompt_provenance`에 빌드 이력 저장.
  - **P0 feature_flags.py 호스트 패치**: `governance_enabled()` helper 함수를 호스트 파일에 추가 (로컬 워크트리에만 존재하던 상태 보정).
  - **runner-af09281f 정리**: depends_on이 rejected_done인 영구 대기 러너를 error 상태로 전환.
  - **runner-34c0836a 제출**: Admin Dashboard 4개 페이지(governance/model-parity/deploy/sessions) 일괄 구현 러너 (실행 중).
  - **API Hot-Reload**: 54개 모듈 재로드 완료, health-check 전항목 정상 확인.

- **2026-04-24 직접 보강**: AADS 채팅 실행 복구를 `execution_id` 중심으로 전환했다. `chat_turn_executions`, `chat_messages.execution_id`, `chat_sessions.current_execution_id`를 도입했고, `app/services/chat_service.py`, `app/routers/chat.py`, `app/services/redis_stream.py`, `app/services/stream_worker.py`, `app/main.py`에서 execution 단위 SSE attach/replay, 단일 assistant row 재사용, execution 기반 resume 스캐너를 반영했다. 기존 `recovered` 추론 복구는 fallback 성격으로 축소됐다.
- **2026-04-24 운영 조치**: 서버 `deploy.sh`의 `code` 모드 health 대기 시간을 기본 30초에서 60초로 늘려, graceful restart 직후 앱이 정상 복귀했는데도 배포 스크립트가 거짓 실패로 종료하던 false negative를 줄였다. 대시보드 `deploy.sh`는 비활성 대상 슬롯 컨테이너가 남아 있을 때 선정리 후 기동하도록 보강했다.
- **2026-04-24 검증 결과**: Governance v2.1 후속 검증을 다시 수행했다. 백엔드 단위테스트는 `python3.11 -m pytest tests/unit/test_governance_v21.py tests/unit/test_governance_change_requests.py tests/unit/test_prompt_compiler.py -q` 기준 `10 passed`였고, 실제 프런트 빌드 루트인 `/root/aads/aads-dashboard`는 `./node_modules/.bin/tsc --noEmit --incremental false` 타입체크가 통과했다. 다만 실제 대시보드 체크아웃에는 `src/app/admin/model-parity/page.tsx`만 존재하고 `governance/emergency/sessions/deploy` 페이지와 Sidebar 링크는 아직 없으며, 현재 워크스페이스의 `aads-dashboard/`는 `src/` 스냅샷만 있어 여기서는 Next 빌드를 돌릴 수 없다. 또한 DB 마이그레이션 실적용 여부는 이 세션의 샌드박스가 `psql` 소켓 생성을 `Operation not permitted`로 차단해 실측하지 못했다.
- **2026-04-24 직접 보강**: Governance v2.1 운영 가시화를 추가했다. `app/api/governance.py`에 `GET /governance/role-profiles`를 추가해 `role_profiles.project_scope/tool_allowlist`를 노출했고, `aads-dashboard/src/app/admin/emergency/page.tsx`에서 `governance_enabled` kill-switch, 기타 feature flag, governance audit log, 역할별 프로젝트 범위를 한 화면에서 제어/확인할 수 있게 했다. `Sidebar.tsx`, `aads-dashboard/src/lib/api.ts`, `tests/unit/test_governance_v21.py`도 함께 갱신했다.
- **2026-04-24 직접 보강**: Governance v2.1 런타임 결함을 보정했다. `app/core/feature_flags.py`에 `governance_enabled()` helper를 추가했고, `app/services/intent_router.py`의 intent temperature 조회를 실제 스키마인 `intent_policies.temperature`로 정렬했다. `app/api/governance.py`는 `temperature` 필드를 조회/저장하도록 보강했고, `tests/unit/test_governance_v21.py`로 회귀 테스트를 추가했다.
- **2026-04-24 직접 보강**: Runner Task Board가 제출 모델(`model`)과 실제 실행 모델(`actual_model`)을 혼동하던 문제를 보강했다. `scripts/pipeline-runner.sh`가 시도 시작 즉시 `pipeline_jobs.actual_model`을 갱신하도록 바꿨고, `/admin/tasks` 목록과 `aads-dashboard/src/app/admin/tasks/page.tsx`가 `actual_model`을 우선 표시하며 상세 패널에 `Actual/Configured/Worker Override`를 분리해 보여준다.
- **2026-04-24 직접 보강**: Admin Dashboard 잔여 누락을 로컬 워크트리에 직접 반영했다. `app/api/admin.py`에 `/admin/sessions`, `/admin/sessions/{job_id}`를 추가했고, `aads-dashboard/src/lib/api.ts`에 sessions/model-parity API 메서드를 보강했으며, `aads-dashboard/src/app/admin/model-parity/page.tsx`를 신규 추가하고 `Sidebar.tsx`에 Governance/Model Parity/Deploy/Sessions 링크를 정리했다.
- **승인 대기**: `runner-db5686da` — `/admin/governance` 세션 거버넌스 대시보드 (백엔드+프론트)
- **승인 대기**: `runner-18ddd734` — `/admin/model-parity` 모델 패리티 대시보드 (백엔드+프론트)
- **2026-04-24 운영 조치**: `claude-relay` 전역 동시성은 Pipeline Runner를 포함하지 않는 것으로 재확인했다. live는 systemd drop-in `/etc/systemd/system/claude-relay.service.d/runtime.conf`로 `CLAUDE_RELAY_MAX_CONCURRENT=5`, `CLAUDE_NONINTERACTIVE_WRAPPER=/root/aads/aads-server/scripts/claude-docker-wrapper-active.sh`를 고정했다. blue-green 전환 후에도 relay/Claude CLI가 `.active_container`를 따라 현재 활성 API 컨테이너를 사용한다.
- **2026-04-24 운영 조치**: 채팅 active stream 계측은 `executing / visible / recovery_pending / recent_placeholders` 기준으로 재정리했다. 재배포 drain에서 실제 활성 스트림이 `2 → 1 → 0`으로 집계되는 것을 확인했고, 이전처럼 resume/placeholder 세션이 있어도 `active=0`으로 보이던 오판을 줄였다.
- **거버넌스 v2.1 Phase 1-A 준비**: `scripts/migrations/20260424_governance_v2_1_tables.sql` 추가 — `governance_events`, `intent_policies`, `role_profiles`, `change_requests` 생성 마이그레이션과 시드(`intent_policies=7`, `role_profiles=5`)를 반영했다.
- **거버넌스 v2.1 P1-D 거버넌스 컬럼 확장 (temperature + project_scope)**: `scripts/migrations/20260424_governance_v2_1_columns.sql` 추가 — `intent_policies.temperature`, `role_profiles.project_scope` 컬럼 확장과 `intent_policies` 기본 temperature 시드 업데이트를 반영했다.
- **migration 054** (`054_llm_key_provider_normalization.sql`) — untracked, DB 정규화 대상 0건으로 적용 무해
- **migration 055** (`chat_model_preferences`) — DB 적용 완료
- **인증 우선순위**: `ANTHROPIC_AUTH_TOKEN_2`(moongoby, priority=1), `ANTHROPIC_AUTH_TOKEN`(moong76, priority=2)
- **2026-04-24 장애 조치**: `llm_models.metadata`가 JSON 문자열 row일 때 `model_selector._route_metadata()`와 `model_registry.sync_model_registry()`가 `dict(...)`로 바로 처리하며 `ValueError`를 내던 공통 장애를 수정했다. `app/services/model_selector.py`, `app/services/model_registry.py`에 metadata coercion을 추가했고, 문자열 metadata 회귀 테스트를 `tests/unit/test_model_selector_dynamic_routing.py`, `tests/unit/test_model_registry.py`에 남겼다.
- **2026-04-24 장애 조치**: `app/services/model_registry.py`의 `filter_executable_models()`에 `_normalize_model_id()`를 추가해 `codex:`, `litellm:`, `claude:` 접두사를 제거한 뒤 `llm_models.model_id`와 비교하도록 수정했다. `claude-sonnet` vs `claude-sonnet-4-6` 같은 버전 suffix는 `startswith`로 허용해 `runner_model_config` 설정이 전부 탈락하면서 `minimax-m2.7` 폴백으로 내려가던 문제를 막는다. 회귀 테스트는 `tests/unit/test_model_registry.py`에 반영했다.
- **AADS-200B backend 반영**: `migrations/056_braming_node_feedback.sql`로 `braming_nodes`에 `ceo_opinion/picked` 컬럼을 추가하고 `braming_node_votes` 테이블을 도입했다. `app/services/braming_service.py`, `app/api/braming.py`는 노드 상세 조회, CEO 의견 저장/삭제, 찬반 투표 토글, Pick/Unpick API와 그래프 응답의 `ceoOpinion/voteSummary/myVote/picked` enrichment를 지원한다. 회귀 테스트는 `tests/unit/test_braming_service.py`, `tests/unit/test_braming_api.py`에 추가했다.
- **AADS-200B frontend 블로커**: 요구된 프론트 경로 `/root/aads/aads-dashboard/src/app/braming/*` 는 현재 워크스페이스 쓰기 허용 범위 밖이라 본 런에서는 수정하지 못했다. 다음 작업은 해당 경로 쓰기 권한이 열린 환경에서 `api.ts`, `page.tsx`, `components/BramingCanvas.tsx`, `components/BramingNode.tsx`, `components/NodeDetailPanel.tsx`를 백엔드 계약에 맞춰 연결하면 된다.

## AADS-190E
- `scripts/claude_relay_server.py`에 Claude/Codex 실행 preflight와 `aads-tools` MCP bridge preflight를 추가했다. `docker exec` 경로와 `python3.11 -m mcp_servers.aads_tools_bridge` 직접 실행 경로를 후보로 두고, 실패 원인을 `docker_container_missing`, `python_module_missing` 같은 분류로 로그에 남긴다.
- `scripts/mcp_config_template.json`의 기본 bridge 실행기를 `python3`로 정리해 템플릿 경로와 relay가 선택하는 docker 경로가 같은 실행기를 가리키도록 맞췄다.
- 같은 파일에서 Claude 기본 실행 경로는 `scripts/claude-docker-wrapper.sh`를 우선 사용하도록 복원했고, Codex/Claude 모두 health 응답에 현재 command mode와 MCP bridge mode를 노출한다.
- `scripts/claude_relay_server.py`와 `app/services/model_selector.py`, `app/services/chat_service.py`는 `user cancelled MCP tool call`을 `session_cancelled_mcp_tool_call`로 재분류하고 `is_error/error_type/cancel_scope/raw_error`를 SSE까지 유지한다. 세션별 취소가 더 이상 일반 user cancel 문자열로만 뭉개지지 않는다.
- `mcp_servers/aads_tools_bridge.py`는 `/app` 외에 저장소 루트도 `sys.path`에 추가해 호스트 `python3.11 -m ...` 직접 실행 경로를 지원한다.
- `app/services/pipeline_runner_client.py`를 추가하고 `app/services/tool_executor.py`, `app/api/ceo_chat_tools.py`의 Pipeline Runner 내부 호출 URL을 공통 helper로 통일했다. 내부 self-call 기본값은 `http://localhost:8080`이며, 필요 시 `PIPELINE_RUNNER_INTERNAL_BASE_URL`로 오버라이드한다.
- `tests/unit/test_relay_diagnostics.py`를 추가해 내부 runner URL helper, direct Python MCP session 주입, relay 취소 재분류를 검증하는 회귀 테스트를 남겼다.

## AADS-190C
- `app/services/llm_account_usage.py` 추가로 `llm_api_keys`, `oauth_usage_log`, `pipeline_jobs.actual_model/worker_model`을 결합한 계정별 LLM 사용량 스냅샷 계층을 도입했다.
- background/provider 분류는 `codex:gpt-5.4`, `litellm:gemini-2.5-flash`, `litellm:openrouter-grok-4-fast`, `litellm:kimi-k2`, `litellm:minimax-m2.7`, `litellm:groq-qwen3-32b`와 같은 접두사/실모델 표기를 모두 인식한다.
- Anthropic 계정은 `oauth_usage_log` 기준 exact per-account 5h/7d 사용량과 recent error, 최신 rate-limit 헤더를 노출하고, 기타 provider는 `pipeline_jobs` 기준 provider-level observed usage 또는 key state only로 구분한다.
- `app/api/ops.py`에 `/api/v1/ops/account-usage` API를 추가했다.
- `tests/unit/test_llm_account_usage.py`로 접두사 기반 provider 매핑과 표시명 보강(Kimi, MiniMax, Codex CLI)을 검증한다.

## AADS-189B
- `app/services/model_registry.py`의 템플릿 metadata에 `execution_backend`, `execution_model_id`, `execution_base_url`를 추가해 “보이는 모델”과 “실제 실행 경로”를 같은 row에 담는다.
- direct provider 후보는 OpenAI, Groq, DeepSeek, OpenRouter, Qwen, Kimi, MiniMax로 정리했고, Anthropic은 `claude_cli_relay`, Codex는 `codex_cli`, Gemini는 `litellm_proxy` backend로 표시한다.
- `app/services/model_selector.py`는 레지스트리 row metadata를 읽어 `openai_compatible_direct` 모델을 우선 direct provider 경로로 호출한다. 정적 allowlist에 없는 신규 모델도 `llm_models`에 row가 있으면 direct route를 탈 수 있다.
- direct provider API 키는 provider별 활성 키 우선, 없으면 환경변수 폴백을 사용한다.
- 회귀 테스트는 `tests/unit/test_model_selector_dynamic_routing.py`에 추가했다. Qwen 신규 동적 row가 LiteLLM 하드코딩 경로가 아니라 direct route로 분기되는지를 검증한다.
- 운영 주의: `llm_models.metadata`는 DB/드라이버 상태에 따라 dict가 아니라 JSON 문자열로 읽힐 수 있다. selector/sync 양쪽 모두 문자열 metadata를 먼저 JSON object로 정규화한 뒤 사용해야 한다.

## AADS-189A
- `migrations/053_llm_model_registry.sql` 추가로 `llm_models`, `llm_key_audit_logs` 테이블을 도입했다.
- `app/services/model_registry.py` 추가로 provider 템플릿 기반 모델 레지스트리, provider 요약, 수동/자동 sync, cache invalidation 공통 계층을 구현했다.
- `app/api/llm_keys.py`는 create/update/activate/deactivate 시 priority 충돌 검증, 감사 로그 적재, stale key cache 제거, registry sync를 수행한다.
- `app/api/llm_models.py`와 `app/main.py` 라우터 등록으로 `/api/v1/llm-models`, `/api/v1/llm-models/providers/summary`, `/api/v1/llm-models/sync` API를 제공한다.
- `app/services/model_selector.py`, `app/services/pipeline_runner_service.py`, `app/api/pipeline_runner.py`, `app/services/code_reviewer.py`가 DB 레지스트리의 실행 가능 모델 필터를 우선 사용하고, 활성 모델이 비어 있으면 기존 하드코딩 경로로 안전 폴백한다.
- `tests/unit/test_model_registry.py`로 provider 정규화, unknown provider review 상태, executable filter 폴백 규칙을 검증한다.

## AADS-188
- `app/api/llm_keys.py` 추가로 `llm_api_keys` 조회·추가·수정·비활성화 API 제공.
- `app/main.py`에 `/api/v1/llm-keys` 라우터 등록.
- 대시보드 Settings 탭에서 LLM API 키 관리 UI를 연동하도록 백엔드 계약 추가.

## AADS-187
- `scripts/update_claude_all_servers.sh` 전면 재작성.
- 서버 114를 첫 순서로 즉시 처리하도록 배치.
- Claude Code CLI, Codex CLI, `claude-agent-sdk` 버전 전후 비교와 변경 시 Telegram 알림 추가.
- `/root/aads/.env` 로드, `/root/tmp` 기반 pip 설치, 서버별 실패 내성, 최종 성공/실패 요약 전송 추가.

## 운영 반영 포인트
- 목표 cron 라인: `0 4 * * * /root/aads/aads-server/scripts/update_claude_all_servers.sh >> /var/log/claude_update.log 2>&1`
- 현재 워크스페이스에는 실제 시스템 crontab과 원격 서버 상태가 없어서 파일 수정만 반영됨.

## AADS-CHAT-OPT (2026-04-28)
- **c46ddbe** `feat(chat): interrupt routing + retry P0 + ext-cache 1h + tool cache (4patch)` — origin/main push 완료, reload-api.sh로 08:31 KST 서버 메모리 반영 완료
- **4-patch 적용**: ①interrupt 자동 라우팅(routers/chat.py L239) ②LLM 재시도 5초×60회(anthropic_client.py L32) ③extended-cache 1h(cache_config.py L21) ④tool execution-scope LRU 캐시(tool_executor.py L88)
- **thinking UI 패치(f89ce6c)**: thinkingBuf 분리 + streamingThinking prop 렌더 — green 컨테이너 15:02 KST 반영
- **빈 버블 패치**: streamingContent 조건에 `&& streamBuf` 추가 — page.tsx L4936 호스트 반영 완료 (streaming=true && streamBuf="" 순간 빈 버블 방지)

## AADS-PROMPT-GOV-V2.1 (2026-04-28 08:25 KST)
- **prompt_assets 24건 시딩 완료** (L1:4 / L2:6 / L3:7 / L4:4 / L5:3) — 5-Layer 구조 모두 채워짐
- **PromptCompiler INSERT 패치**: `_record_provenance()`의 conn release 이슈 수정 — `compiled_prompt_provenance` 1건 첫 실측 INSERT 확인
- **runner-368675d8 승인**: `/admin/prompts` 페이지에 5-Layer CRUD 탭 추가 (Layer 필터 사이드바 + 모달 에디터 + JSON scope 검증)

## AADS-DOCS-INCREMENTAL-SCAN (2026-04-28 14:27 KST)
- `/docs` 문서 스캔을 기존 목록 재사용 + 증분 갱신 방식으로 보강했다.
- Backend: `app/api/project_docs.py`가 5분 메모리 캐시 외에 `/tmp/aads_project_docs_cache.json` 파일 캐시를 저장/복원하고, 강제 스캔 시 `delta.new/updated/removed/unchanged`를 계산한다.
- Frontend: `aads-dashboard/src/app/docs/page.tsx`가 `localStorage(aads.docs.scanResult.v1)`의 기존 목록을 즉시 렌더링한 뒤 백그라운드로 최신 목록을 갱신한다.
- 검증: `docker exec aads-server python3 -m py_compile /app/app/api/project_docs.py`, `npx eslint src/app/docs/page.tsx`, 컨테이너 직접 호출 기준 문서 1,431건 및 2회차 `cache_hit=True` 확인.
