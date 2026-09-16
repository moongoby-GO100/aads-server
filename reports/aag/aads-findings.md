# AAG L1/L2 — AADS 아키텍처 결함 리포트

생성 2026-09-16 16:04 KST · 결함 96건 · 판정 불가(UNRESOLVED) 110건

UNRESOLVED 는 **결함 수에 포함하지 않는다**. 판정 못 한 것을 결함으로 세면
숫자가 부풀고, 부풀린 숫자는 아무도 손대지 않아 규칙 전체가 무시된다.

## 스캔 범위

| 대상 | 수 |
|---|---|
| 앱 파이썬 파일 | 381 |
| 라우터 디렉터리 파일 | 87 |
| APIRouter 정의 모듈 | 81 |
| include_router 호출 | 82 |
| 마운트된 라우트 | 880 |
| 네임스페이스 | 82 |
| 프런트 파일 | 232 |
| 해석된 프런트 호출 | 337 |
| SQL 참조 테이블 | 219 |
| 그래프 노드/엣지 | 758 / 1151 |

## 규칙별 건수

| 규칙 | 심각도 | 건수 |
|---|---|---|
| `DUP_MODULE` | P1 | 1 |
| `DOUBLE_MOUNT` | P1 | 9 |
| `ORPHAN_ROUTER` | P2 | 1 |
| `TABLE_NO_MODEL` | P1 | 44 |
| `PATH_DRIFT` | P1 | 1 |
| `ROUTE_MISSING` | P0 | 6 |
| `STALE_BACKUP` | P2 | 34 |

| **합계** | | **96** |

## DUP_MODULE (1건)

- [P1] 모듈명 `chat.py` 이 2개 디렉터리에 중복 존재: `app/api/chat.py`, `app/routers/chat.py`

## DOUBLE_MOUNT (9건)

- [P1] 네임스페이스 `/api/v1/admin` 를 3개 모듈이 소유 — `app/api/admin.py`(31개 라우트), `app/api/admin_users.py`(1개 라우트), `app/api/design_modifications.py`(8개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)
- [P1] 네임스페이스 `/api/v1/chat` 를 3개 모듈이 소유 — `app/api/chat.py`(4개 라우트), `app/api/directive_drafts.py`(4개 라우트), `app/routers/chat.py`(78개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)
- [P1] 네임스페이스 `/api/v1/directives` 를 2개 모듈이 소유 — `app/api/directives.py`(3개 라우트), `app/api/ops.py`(1개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)
- [P1] 네임스페이스 `/api/v1/llm-models` 를 2개 모듈이 소유 — `app/api/llm_models.py`(9개 라우트), `app/api/llm_report.py`(2개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)
- [P1] 네임스페이스 `/api/v1/ohvis` 를 3개 모듈이 소유 — `app/api/ohvis_harness.py`(5개 라우트), `app/api/ohvis_llmops.py`(12개 라우트), `app/api/ohvis_tasks.py`(8개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)
- [P1] 네임스페이스 `/api/v1/ops` 를 3개 모듈이 소유 — `app/api/hot_reload.py`(2개 라우트), `app/api/memory_monitor.py`(5개 라우트), `app/api/ops.py`(72개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)
- [P1] 네임스페이스 `/api/v1/projects` 를 4개 모듈이 소유 — `app/api/checkpoints.py`(5개 라우트), `app/api/project_dashboard.py`(4개 라우트), `app/api/projects.py`(7개 라우트), `app/api/stream.py`(1개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)
- [P1] 네임스페이스 `/api/v1/settings` 를 3개 모듈이 소유 — `app/api/directives.py`(2개 라우트), `app/api/pipeline_runner.py`(2개 라우트), `app/routers/chat.py`(7개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)
- [P1] 네임스페이스 `/api/v1/user` 를 2개 모듈이 소유 — `app/api/user_api_keys.py`(4개 라우트), `app/api/user_project_servers.py`(4개 라우트). 정확히 겹치는 METHOD+경로는 0건 (0건이어도 부채다 — 한 네임스페이스의 주인이 둘이면 라우트 추가 시 어느 쪽에 넣을지가 매번 우연에 맡겨진다)

## ORPHAN_ROUTER (1건)

- [P2] `app/api/ceo_chat.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다

## TABLE_NO_MODEL (44건)

- [P1] 테이블 `aads_conversations` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/project_dashboard.py`)
- [P1] 테이블 `agent_activity_log` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/cross_validator.py`)
- [P1] 테이블 `agent_registry` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/project_dashboard.py`)
- [P1] 테이블 `ai_persona_references` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/image.py`)
- [P1] 테이블 `api_tokens` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/device.py`)
- [P1] 테이블 `auto_trade_orders` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/tool_registry.py`)
- [P1] 테이블 `bridge_activity_log` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ops.py`, `app/services/cross_validator.py`)
- [P1] 테이블 `ceo_chat_messages` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ceo_chat.py`)
- [P1] 테이블 `ceo_chat_sessions` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ceo_chat.py`)
- [P1] 테이블 `ceo_decision_log` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/cross_validator.py`)
- [P1] 테이블 `ceo_facts` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ceo_chat.py`)
- [P1] 테이블 `ceo_session_summaries` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ceo_chat.py`)
- [P1] 테이블 `checkpoint_logs` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/checkpoints.py`)
- [P1] 테이블 `checkpoints` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/projects.py`)
- [P1] 테이블 `claude_max_usage_snapshot` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/oauth_usage_tracker.py`)
- [P1] 테이블 `commit_log` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ops.py`, `app/services/cross_validator.py`, `app/services/health_checker.py`)
- [P1] 테이블 `cost_tracking` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ops.py`, `app/services/cross_validator.py`, `app/services/tenant_usage_limits.py`)
- [P1] 테이블 `deploy_recent_durations` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/deploy_observability.py`)
- [P1] 테이블 `directive_lifecycle` 을 16개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/briefing.py`, `app/api/channels.py`, `app/api/ops.py`)
- [P1] 테이블 `directive_model_config` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/directives.py`, `app/services/directive_draft_service.py`)
- [P1] 테이블 `doc_chunks` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/project_docs.py`, `app/services/doc_index.py`)
- [P1] 테이블 `escalation_recovery` 을 4개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ops.py`, `app/services/escalation_engine.py`, `app/services/project_healing.py`)
- [P1] 테이블 `governance_emergency_actions` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/admin.py`)
- [P1] 테이블 `intent_temperatures` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/intent_router.py`)
- [P1] 테이블 `jobs` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/admin.py`)
- [P1] 테이블 `kg_entities` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/core/knowledge_graph.py`, `app/core/memory_recall.py`, `app/services/kg_query.py`)
- [P1] 테이블 `kg_relations` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/core/knowledge_graph.py`, `app/core/memory_recall.py`, `app/services/kg_query.py`)
- [P1] 테이블 `lessons` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/lessons.py`)
- [P1] 테이블 `llm_fallback_chains` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/core/llm_fallback_engine.py`)
- [P1] 테이블 `llm_key_health_log` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/llm_admin.py`)
- [P1] 테이블 `maintenance_schedule` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ops.py`)
- [P1] 테이블 `ohvis_tasks` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ohvis_tasks.py`, `app/services/ohvis_task_manager.py`, `app/services/temporal_controller.py`)
- [P1] 테이블 `orders` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/tool_registry.py`)
- [P1] 테이블 `pipeline_c_jobs` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/main.py`)
- [P1] 테이블 `pipeline_jobs` 을 23개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/admin.py`, `app/api/ceo_chat_tools.py`, `app/api/ops.py`)
- [P1] 테이블 `project_tasks` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/context.py`, `app/api/project_dashboard.py`)
- [P1] 테이블 `prompt_versions` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/admin.py`)
- [P1] 테이블 `server_env_history` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ops.py`, `app/services/cross_validator.py`)
- [P1] 테이블 `session_blueprints` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/prompt_compiler.py`)
- [P1] 테이블 `session_relay` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/session_relay.py`)
- [P1] 테이블 `system_metrics` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/channels.py`, `app/api/ops.py`, `app/services/cross_validator.py`)
- [P1] 테이블 `task_cost_log` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/project_dashboard.py`)
- [P1] 테이블 `task_tracking` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/api/ceo_chat.py`)
- [P1] 테이블 `yeoljeong_bank_transactions` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `app/services/yeoljeong_accounting_service.py`, `app/services/yeoljeong_dashboard_service.py`)

## PATH_DRIFT (1건)

- [P1] `/root/aads/aads-dashboard/src/lib/api.ts:522` POST `/api/v1/chat/messages` — 경로는 있으나 메서드가 GET 다 (/api/v1/chat/messages)

## ROUTE_MISSING (6건)

- [P0] `/root/aads/aads-dashboard/src/app/kakaobot/history/page.tsx:50` GET `/api/v1/kakao-bot/history` — 일치하는 라우트 없음
- [P0] `/root/aads/aads-dashboard/src/app/kakaobot/history/page.tsx:51` GET `/api/v1/kakao-bot/history/stats` — 일치하는 라우트 없음
- [P0] `/root/aads/aads-dashboard/src/app/kakaobot/page.tsx:39` GET `/api/v1/kakao-bot/stats` — 일치하는 라우트 없음
- [P0] `/root/aads/aads-dashboard/src/app/kakaobot/scheduled/page.tsx:51` POST `/api/v1/kakao-bot/scheduled/{}/cancel` — 일치하는 라우트 없음
- [P0] `/root/aads/aads-dashboard/src/app/kakaobot/settings/page.tsx:67` GET `/api/v1/kakao-bot/settings` — 일치하는 라우트 없음
- [P0] `/root/aads/aads-dashboard/src/app/kakaobot/settings/page.tsx:81` PUT `/api/v1/kakao-bot/settings` — 일치하는 라우트 없음

## STALE_BACKUP (34건)

- [P2] `app/api/ceo_chat.py.bak` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/chat.py.bak.T073` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/context.py.bak.T089` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/context.py.bak.T090` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/context.py.bak.T091` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/context.py.bak.T107` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/conversations.py.bak.T077` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/conversations.py.bak.T089` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/kakao_bot.py.bak` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/llm_keys.py.bak_aads188` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/memory.py.bak_20260305` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/pipeline_runner.py.bak_model` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T058` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T068` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T070` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T072` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T074` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T078` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T080` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T081` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T082` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T089` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T090` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/api/project_dashboard.py.bak.T107` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/auth.py.bak_manual` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/main.py.bak.T048` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/main.py.bak_pool_fix` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/models/pc_agent.py.bak` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/services/sandbox.py.bak` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `app/static/apps/yeoljeong-finance/index.html.bak-20260714-1605-static-short` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `scripts/claude-oauth-wrapper.sh.bak_20260326_122801` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `scripts/pipeline-runner.sh.bak.litellm` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `scripts/pipeline-runner.sh.bak_litellm_fix` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다

## UNRESOLVED (결함 아님 — 판정 불가)

### FRONTEND_URL (58건)
- `/root/aads/aads-dashboard/src/app/braming/api.ts:13` fetch() URL 해석 불가 — 변수가 경로 세그먼트 일부에만 붙어 경로를 확정할 수 없음: `${BASE}/braming${path}`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1091` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/pipeline/jobs?session_id=${sessionId}&limit=50`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1245` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/${qs}`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1279` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1335` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/${agendaId}`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1360` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/${agendaId}/restore`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:1383` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/agenda/${agendaId}`
- `/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx:2511` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/artifacts/${activeArtifact.id}/export`
- `/root/aads/aads-dashboard/src/app/chat/ChatInput.tsx:188` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/voice/transcribe`
- `/root/aads/aads-dashboard/src/app/chat/RunnerHostStatus.tsx:55` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/pipeline/runner/status?window_hours=1`
- `/root/aads/aads-dashboard/src/app/chat/api.ts:80` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작: `${BASE_URL}${path}`
- `/root/aads/aads-dashboard/src/app/chat/api.ts:162` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작: `${BASE_URL}/chat/files/upload?session_id=${sessionId}&uploaded_by=user`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:5342` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/sessions/${sessionId}/resume`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:5455` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/executions/${executionId}/events?last_event_id=${encodeURIComponent(replayLastEventId)}`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:7508` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/image/generate`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:8026` fetch() URL 해석 불가 — URL 이 문자열/템플릿이 아님 (변수 또는 함수 결과): fetchUrl
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:8351` fetch() URL 해석 불가 — URL 이 문자열/템플릿이 아님 (변수 또는 함수 결과): retry with backoff
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:8737` fetch() URL 해석 불가 — URL 이 문자열/템플릿이 아님 (변수 또는 함수 결과): resumeUrl
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:9178` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/sessions/${sid}/stop`
- `/root/aads/aads-dashboard/src/app/chat/page.tsx:9247` fetch() URL 해석 불가 — base 를 알 수 없는 변수로 시작 — `BASE_URL` 은 `./api` 에서 import 되는데 그 모듈이 스캔 범위 안에 없다: `${BASE_URL}/chat/sessions/${sid}/stop`
- … 외 38건

### FRONTEND_VAR_SEGMENT (1건)
- `/root/aads/aads-dashboard/src/app/admin/loops/page.tsx:65` POST /api/v1/loops/{}/{} — 변수 세그먼트라 확정 불가 (후보 /api/v1/loops/{}/safety)

### SQL_TABLE (51건)
- `app/api/admin.py:1045` 테이블 이름이 런타임 보간이라 확정 불가
- `app/api/admin_users.py:79` 테이블 이름이 런타임 보간이라 확정 불가
- `app/api/admin_users.py:180` 테이블 이름이 런타임 보간이라 확정 불가
- `app/api/memory_monitor.py:281` 테이블 이름이 런타임 보간이라 확정 불가
- `app/api/memory_monitor.py:301` 테이블 이름이 런타임 보간이라 확정 불가
- `app/core/credential_vault.py:705` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/ab_test_problems.py:263` SQL 조각(문장 키워드로 시작하지 않음) — 테이블 확정 불가
- `app/services/financial_audit.py:75` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/financial_audit.py:96` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:272` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:296` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:353` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:366` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:257` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:261` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_evaluator.py:316` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_store.py:442` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_store.py:1203` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_store.py:1201` 테이블 이름이 런타임 보간이라 확정 불가
- `app/services/llmops_store.py:201` 테이블 이름이 런타임 보간이라 확정 불가
- … 외 31건

