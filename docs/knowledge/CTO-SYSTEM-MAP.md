# CTO-SYSTEM-MAP: AADS 시스템 전체 아키텍처 지도
_생성: 2026-03-30 | 갱신: 2026-03-31 | Phase 1 시스템 완전 파악 + Pipeline Runner 반영_

## 인프라 (서버68, Docker Compose)

| 컨테이너 | 이미지 | 포트 | 역할 |
|----------|--------|------|------|
| aads-server | 자체빌드 | 8100→8080 | FastAPI 백엔드 (메모리 2G) |
| aads-server-green | 동일 | 8102→8080 | Blue-Green 무중단 배포 (profile) |
| aads-postgres | pgvector/pg15 | 5433→5432 | PostgreSQL + pgvector |
| aads-redis | redis:7-alpine | 내부 6379 | 캐시 128MB + AOF 영속화 |
| aads-litellm | LiteLLM proxy | 4000 | LLM 프록시 26모델 |
| aads-dashboard | Next.js 16 | 3100 | CEO 대시보드 20페이지 |
| aads-socket-proxy | docker-socket-proxy | 내부 2375 | 보안 Docker 소켓 |
| aads-searxng | SearXNG | 8888→8080 | 무료 메타검색 |

소스 볼륨 마운트: `/root/aads/aads-server/app:/app/app:rw` — 코드 수정 시 재빌드 불필요.
배포: `docker-compose.prod.yml` — AADS Blue-Green은 `deploy.sh bluegreen`.

## LLM 라우팅 체계

```
메시지 → intent_router.py(Gemini Flash-Lite, 65인텐트)
  → model_selector.py(2126줄)
    ├─ 키 조회 → DB llm_api_keys(Fernet 암호화) → 복호화 후 provider 키 반환, DB 장애 시 .env 폴백
    ├─ Claude 인텐트 → Anthropic OAuth 직접
    │   5단계 폴백: Opus 4.7(Gmail)→Opus 4.7(Naver)→Sonnet(Gmail)→Sonnet(Naver)→Gemini
    ├─ Gemini 인텐트 → LiteLLM 프록시 (newtalk/aads 2개 키 로드밸런싱)
    └─ Gemini Direct (grounding/deep_research) → Google API
```

인증 중앙: `app/core/auth_provider.py` — Gmail/Naver OAuth 교대, `rotate_oauth_primary_fallback()`.
키 저장: DB `llm_api_keys` (Fernet 암호화, `.env` 폴백)로 중앙 관리.
배경 작업: `app/core/anthropic_client.py` — `call_llm_with_fallback()` (Claude → Gemini).

LiteLLM 모델 (litellm-config.yaml):
- Gemini: 2.5-flash/pro, 3.0-pro/flash, 3.1-pro/flash-lite, gemma-3-27b
- DeepSeek: chat, reasoner
- Groq: llama-70b/8b, llama4-scout, qwen3-32b
- OpenRouter: grok-4-fast, deepseek-v3, mistral-small, nemotron-free, minimax-m2
- Claude: sonnet-4-6, opus-4-7 (LiteLLM 경유)

## 채팅 시스템 (핵심)

| 파일 | 줄 수 | 역할 |
|------|------|------|
| `app/routers/chat.py` | 1,157 | v2 라우터 — Workspace/Session/Message/Artifact/Branch CRUD |
| `app/services/chat_service.py` | 4,146 | 비즈니스 로직 — SSE 스트리밍, heartbeat 3s, Background completion |
| `app/services/context_builder.py` | 552 | 3계층 Context Engineering (Layer1 정적/Layer2 동적/Layer3 히스토리) |
| `app/core/prompts/system_prompt_v2.py` | — | Layer1 시스템 프롬프트 원문 |
| `app/services/workspace_preloader.py` | 194 | Layer2.5 — 프로젝트별 facts + 에러패턴 + CEO 관심사 자동 주입 |

DB: chat_messages 11,503건 (2026-03-30 기준).

## 코드 수정 파이프라인

### Pipeline Runner (활성 — 현재 사용 중)

```
CEO 채팅 → pipeline_runner_submit(project, instruction)
  → DB INSERT (pipeline_jobs)
  → pipeline-runner.sh (호스트 systemd, 5초 폴링)
  → Claude Code CLI 실행 (6단계 모델+계정 폴백)
  → AI Reviewer (Sonnet 검수)
  → awaiting_approval → CEO approve
  → git push → 프로젝트별 배포 → done
```

| 항목 | 상세 |
|------|------|
| API | `app/api/pipeline_runner.py` — `/api/v1/pipeline/jobs` |
| 실행기 | `scripts/pipeline-runner.sh` (호스트 systemd 독립 프로세스) |
| 6단계 폴백 | Sonnet(Naver)→Sonnet(Gmail)→Opus(Naver)→Opus(Gmail)→Haiku(Naver)→Haiku(Gmail) |
| 프로젝트별 배포 | AADS→`deploy.sh bluegreen`, KIS→`systemctl restart kis-v41-api`, GO100→`systemctl restart go100`, SF→docker restart |
| **상세 문서** | `docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md`, `docs/pipeline-runner/PIPELINE-RUNNER-API-REFERENCE.md` |
| 서버 재시작 영향 | 없음 (호스트 프로세스) |
| 중복 방지 | DB UNIQUE(project+status='running') |

### Pipeline C (레거시 — 보존, 미사용)

`app/services/pipeline_c.py` (2,130줄) — 코드 보존 상태. 현재 실행 경로에서 사용하지 않음.
Pipeline Runner가 완전 대체. 향후 참조/롤백 용도로 유지.

## 도구 시스템

`app/services/tool_registry.py` (2,202줄) — 87개 도구, Anthropic Tool Use API 포맷.
`app/api/ceo_chat_tools.py` (3,247줄) — 실행 엔진 (read_file, query_db, browser 6종, 원격 DB, 검색 등).
`app/api/ceo_chat_tools_db.py` (501줄) — 프로젝트별 원격 DB (KIS=PostgreSQL, SF/NTV2=MySQL+SSH터널).

Tier 분류: 상시로드(~25), 온디맨드(~62). defer_loading 메타데이터로 관리. ToolExecutor dispatch: 82개.

## 메모리/진화 시스템

| 모듈 | 역할 |
|------|------|
| `app/core/memory_recall.py` (921줄) | 7섹션 자동 주입 (session_notes/preferences/tool_strategy/directives/discoveries/learned/correction) |
| `app/core/memory_gc.py` (758줄) | GC(confidence 감쇠) + 중복 병합 + Sleep-Time Agent(인사이트 생성) |
| `app/services/self_evaluator.py` (769줄) | Haiku로 응답 품질 평가 → Reflexion 자동 루프 |
| `app/services/workspace_preloader.py` (194줄) | 에러패턴 경고 + 최근 facts + CEO 관심사 예측 |
| `app/services/fact_extractor.py` | 대화에서 사실 자동 추출 → memory_facts |
| `app/services/ceo_pattern_tracker.py` | CEO 행동 패턴 학습 → 관심사 예측 |

DB 테이블: ai_observations(328건+), ai_meta_memory, memory_facts, session_notes, experience_memory.
카테고리: discovery(133), ceo_preference(74), ceo_correction(35), project_pattern(36), tool_strategy(21).

## 자율 운영 (APScheduler, 17개 잡)

| 주기 | 잡 | 역할 |
|------|---|------|
| 30초 | healing_cycle | 서비스 헬스체크 + 자동복구 (unified_healer.py, 777줄) |
| 2분 | alert_eval | 규칙 기반 알림 평가 |
| 5분 | auto_fix_dispatcher | error_log → 자동 수정 작업 제출 |
| 2시간 | background_compaction | 미압축 세션 자동 압축 |
| 3시간 | learning_health_check | 대화 vs 학습 비율 체크 |
| 매일 09:00 | daily_summary | CEO 텔레그램 일일요약 |
| 매일 12:00 | memory_gc | ai_observations 가비지 컬렉션 |
| 매일 13:00 | memory_consolidation | 중복 병합, 참조 강화 |
| 매일 14:00 | sleep_time_agent | 인사이트 생성 + 프롬프트 최적화 |
| 매일 15:00~16:30 | quality chain | stats→regression→feedback→research→experience |
| 매주 월 | weekly_briefing + quality | CEO 주간 브리핑 + 품질 분석 |

## DB 스키마 (72개 테이블, 주요 도메인)

- **채팅**: chat_workspaces, chat_sessions, chat_messages, chat_artifacts, chat_files, chat_drive_files
- **메모리**: ai_observations, ai_meta_memory, memory_facts, session_notes, experience_memory
- **파이프라인**: pipeline_jobs, task_logs, commit_log, approval_queue
- **프로젝트**: projects, project_tasks, project_plans, project_artifacts, project_memory
- **모니터링**: error_log, alert_history, system_metrics, monitored_services, circuit_breaker_state
- **CEO**: ceo_chat_messages/sessions, ceo_decision_log, ceo_facts, ceo_interaction_patterns, ceo_agenda
- **지시서**: directive_lifecycle
- **품질**: response_critiques, debate_logs/sessions, design_reviews, code_reviews
- **비용**: cost_tracking, task_cost_log
- **카카오봇**: kakao_msgbot_config/logs, kakaobot_contacts/scheduled/templates/anniversaries, kakao_pc_agent_tokens
- **LiteLLM**: LiteLLM_CronJob, LiteLLM_ManagedFileTable

마이그레이션: 011~040 (30개 SQL, `migrations/` 디렉토리).

## 추가 시스템

| 시스템 | 파일 | 역할 |
|--------|------|------|
| PC 에이전트 | `app/api/pc_agent.py` (240줄) + `app/services/pc_agent_manager.py` | WebSocket, agent_id 기반 16개 명령 (AADS-195) |
| Agent SDK | `app/services/agent_sdk_service.py` (380줄) | Claude Agent SDK 통합, bridge fallback |
| 카카오봇 | `app/api/kakao_bot.py` + `app/services/kakaobot_ai.py` | 카카오톡 자동응답 + AI |
| MCP | `app/mcp/client.py` + `app/core/mcp_server.py` | MCP 클라이언트/서버 |
| LangGraph | `app/graph/builder.py`, `state.py`, `routing.py` | 에이전트 실행 체인 |
| 에이전트 16개 | `app/agents/` | pm, supervisor, developer, qa, architect, devops, researcher, strategist, planner, judge 등 |

## 대시보드 (aads-dashboard, Next.js 16)

20개 페이지: /chat(핵심), /ops, /managers, /agenda, /kakaobot, /memory, /reports, /projects, /project-status, /decisions, /tasks, /conversations, /server-status, /settings, /lessons, /flow, /channels, /genspark, /login, /signup.

CEO Chat 구성: ChatInput.tsx, ChatSidebar.tsx, ChatArtifactPanel.tsx, MarkdownRenderer.tsx, api.ts, types.ts.

## 코드 규모 요약

| 영역 | 파일 수 | 핵심 대형 파일 |
|------|---------|--------------|
| app/api/ | 73개 | ceo_chat_tools.py(3247), ceo_chat_tools_db.py(501) |
| app/services/ | 96개 | chat_service.py(4146), pipeline_c.py(2130, 레거시), model_selector.py(2126), tool_registry.py(2202) |
| app/core/ | 18개 | memory_recall.py(921), memory_gc.py(758) |
| app/agents/ | 16개 | — |
| app/models/ | 12개 | chat.py(대형) |
| app/graph/ | 3개 | — |
| app/routers/ | 1개 | chat.py(1157) |
| migrations/ | 30개 | 011~040 |

## 운영 참조

- 서버 재시작: `docker exec aads-server supervisorctl restart aads-api` (R-DOCKER: docker compose up 전체 금지)
- 헬스체크: `curl -s https://aads.newtalk.kr/api/v1/health`
- 배포: `docker compose -f docker-compose.prod.yml up -d --build aads-server`
- 테스트: `docker exec aads-server python3 -m pytest tests/ -v`
- 긴급: GitHub PAT 2026-05-27 만료(~58일), 서버114 디스크 79%

## 관련 문서

- HANDOVER.md: `/root/aads/aads-docs/HANDOVER.md` (v14.0, 67KB) — 메인 인수인계서
- CEO-DIRECTIVES: `/root/aads/aads-docs/CEO-DIRECTIVES.md` (35KB) — CEO 규칙 전체
- AADS-KNOWLEDGE: `docs/knowledge/AADS-KNOWLEDGE.md` — 파이프라인/교차검증/함정
- BLUEGREEN_DEPLOY_SPEC: `docs/BLUEGREEN_DEPLOY_SPEC.md` (12KB) — 무중단 배포 상세
- MEMORY_EVOLUTION: `docs/MEMORY_EVOLUTION_ARCHITECTURE.md` (24KB) — AI 진화 시스템 설계
