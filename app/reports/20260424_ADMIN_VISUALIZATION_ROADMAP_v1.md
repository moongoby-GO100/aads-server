# AADS Admin Visualization — 45 Pages Roadmap v1.0

- **문서 유형**: Lay out (L) 산출물 — CEO 종합 기획서 실측 매핑본
- **작성일**: 2026-04-24 KST (Friday)
- **작성자**: AADS CTO AI (Claude Opus 4.7)
- **원본 기획서**: CEO 2026-04-22 종합 기획서 v1.0 (45 페이지, 9 모듈, 5 레이어)
- **참고 규정**: R-COMMIT, R-AUTH, R-DOCKER, R-QUALITY, FLOW

---

## 0. Executive Summary

CEO 종합 기획서 45 페이지를 현재 AADS 어드민(`aads.newtalk.kr`)에 실측 매핑한 결과,
**전체 반영률 24.4%(11/45)** — 완전 반영 2개, 부분 반영 9개, DB만 준비 14개, 미반영 20개.

핵심 결론 3줄:
1. "작업 지시·추적·리플레이"(P35/P36/P43) 영역이 **가장 큰 공백**이며 Phase 1 최우선.
2. 45개 중 **23개는 기존 DB 테이블(88개) 재사용**으로 단기 구현 가능.
3. 코드맵/ERD/API 영역은 **신규 그래프 인프라** 필요 — Phase 3 분리.

---

## 1. 실측 현황 (2026-04-24 10:13 KST)

### 1.1 현재 페이지 (45 page.tsx 존재)

| 카테고리 | 페이지 |
|---------|--------|
| Auth | `/login`, `/signup` |
| Home/Chat | `/`, `/chat`, `/chat/[id]`, `/chat/terminal` |
| 작업관리 | `/tasks`, `/agenda`, `/decisions` |
| 프로젝트 | `/projects`, `/projects/[id]/{approve-plan,costs,full-cycle,select-item,stream}`, `/project-status`, `/managers` |
| 운영 | `/ops`, `/ops/{memory,pc-agents,recovery,servers}`, `/server-status`, `/reports` |
| 지식 | `/memory`, `/lessons`, `/docs`, `/flow`, `/braming` |
| 커뮤니케이션 | `/conversations`, `/channels`, `/genspark` |
| KakaoBot | `/kakaobot` + 8개 서브 |
| Admin | `/admin/model-parity`, `/admin/prompts`, `/settings` |

### 1.2 API 라우터 (`app/api/*.py`, 45+ 파일)

Chat(ceo_chat/chat/conversations/stream), Tools/Artifact(ceo_chat_tools*·artifacts), Project(projects/project_dashboard/managers/plans), Ops(ops/health/watchdog/approval/pipeline_runner), Code/Deploy(code_review/hot_reload/checkpoints/directives), LLM/Keys(llm_keys/llm_models/credential_vault), Memory(memory/context/briefing), QA(qa/mobile_qa/visual_qa/fact_check/quality).

### 1.3 PostgreSQL 88 테이블 (주요 카운트 실측)

| 테이블 | 행수 | 기획서 매핑 |
|--------|------|-------------|
| `project_tasks` | 1,231 | P35 태스크 보드 |
| `pipeline_jobs` | 276 | P36 세션 리플레이 |
| `chat_artifacts` | 11,655 | P06 문서 / P39 아티팩트 |
| `approval_queue` | 59 | HITL 공통 |
| `deploy_history` | 2 | P37 배포 현황 |
| `commit_log` | 124 | P05/P17/P21 |
| `agent_executions` | 0 | P36/P43 (테이블만) |
| `prompt_versions` | 0 | P39 (스키마만) |
| `llm_models` | 240 | P27/P28 |

**핵심 발견**: 45개 중 23개 페이지의 데이터 레이어가 이미 준비됨 — UI만 그리면 되는 상태.

---

## 2. 45개 매핑 매트릭스

상태 표기: ✅ 완전 / 🟡 부분 / 🟦 DB만 / ❌ 미반영

| # | 페이지 | 우선 | 상태 | 실측 근거 | 재사용 자원 |
|---|--------|------|------|-----------|-------------|
| P01 | 로그인 | CRIT | ✅ | `/login` | — |
| P02 | 사용자/팀 | MED | ❌ | 없음 | `saas_users` |
| P03 | 메인 대시보드 | CRIT | 🟡 | `/page.tsx` | 요약 API 추가 |
| P04 | 프로젝트 관리 | CRIT | 🟡 | `/projects`+`/project-status` 2중화 | 통합 필요 |
| P05 | 문서 목록 | STD | 🟡 | `/docs` | `project_artifacts` |
| P06 | 문서 뷰어 | STD | 🟡 | `/docs` 단일 | `chat_artifacts`(11,655) |
| P07 | 문서 리뷰 큐 | STD | ❌ | 없음 | `approval_queue`(59) |
| P08 | 코드맵 Bird's Eye | MED | ❌ | 없음 | 신규 |
| P09 | Module View | HIGH | ❌ | 없음 | React Flow + dep-cruiser |
| P10 | File Detail | NICE | ❌ | 없음 | tree-sitter |
| P11 | ERD 뷰어 | HIGH | ❌ | 없음 | Mermaid |
| P12 | ERD Diff | HIGH | ❌ | 없음 | prisma diff |
| P13 | DB 헬스 | STD | ❌ | 없음 | `system_metrics` |
| P14 | 모듈 레지스트리 | MED | ❌ | 없음 | package.json 파싱 |
| P15 | 모듈 상세 | STD | ❌ | 없음 | P14 종속 |
| P16 | 아키텍처 다이어그램 | MED | ❌ | 없음 | Mermaid |
| P17 | 아키텍처 히스토리 | STD | 🟦 | UI 없음 | `commit_log`(124) + `directive_lifecycle` |
| P18 | ADR 관리 | STD | 🟦 | UI 없음 | `design_reviews`+`debate_sessions` |
| P19 | API 카탈로그 | HIGH | ❌ | 없음 | FastAPI OpenAPI 자동 |
| P20 | API 테스트 | MED | 🟡 | `/chat/terminal` 유사 | Swagger UI |
| P21 | API 변경 이력 | STD | 🟦 | UI 없음 | `commit_log` + oasdiff |
| P22 | 시크릿 관리 | HIGH | 🟦 | UI 없음 | `llm_api_keys`, `credential_vault.py` |
| P23 | 시크릿 접근 로그 | MED | 🟦 | UI 없음 | `llm_key_audit_logs`, `oauth_usage_log` |
| P24 | MCP 레지스트리 | MED | 🟦 | UI 없음 | `app/mcp/` 존재 |
| P25 | 도구 모니터링 | MED | 🟦 | UI 없음 | `tool_results_archive`, `ceo_chat_tools*.py` |
| P26 | AGENTS.md 편집기 | NICE | ❌ | 없음 | 신규 |
| P27 | 모델 벤치마크 | STD | ✅ | `/admin/model-parity` | 고도화 여지 |
| P28 | 모델 업데이트 | NICE | ❌ | 없음 | `runner_model_config`, `llm_models`(240) |
| P29 | 보안 센터 | HIGH | 🟦 | UI 없음 | `error_log`, `circuit_breaker_state` |
| P30 | 샌드박스 & IAM | CRIT | 🟡 | `run_remote_command` 화이트리스트 | UI 없음 |
| P31 | 정책 관리 | CRIT | ❌ | 없음 | CLAUDE.md 규칙 DB화 필요 |
| P32 | 비용 센터 | HIGH | 🟦 | UI 없음 | `cost_tracking`, `task_cost_log`, `bg_llm_usage_log` |
| P33 | 인시던트 센터 | HIGH | 🟦 | UI 없음 | `error_log`, `recovery_log`, `alert_history` |
| P34 | 감사 로그 | MED | 🟦 | UI 없음 | `llm_key_audit_logs`, `oauth_usage_log` |
| P35 | **태스크 보드** | **HIGH** | 🟡 | `/tasks` 리스트뷰만 | `project_tasks`(1,231) |
| P36 | 태스크 상세/리플레이 | HIGH | ❌ | 없음 | `pipeline_jobs`(276), `chat_messages` |
| P37 | 배포 현황 | HIGH | 🟦 | UI 없음 | `deploy_history`, `hot_reload.py` |
| P38 | 환경 설정 관리 | MED | ❌ | 없음 | `server_env_history` |
| P39 | 프롬프트 레지스트리 | MED | 🟡 | `/admin/prompts` 버전관리X | `prompt_versions` 스키마만 |
| P40 | 피드백 & 교정 허브 | MED | 🟦 | UI 없음 | `response_critiques`, `code_reviews` |
| P41 | 알림 센터 | MED | 🟦 | UI 없음 | `alert_history` + send_alert_message |
| P42 | 서비스 헬스 | STD | 🟡 | `/ops/servers` | `monitored_services`, `system_metrics` |
| P43 | 에이전트 관리 | HIGH | ❌ | 없음 | `agents/` 디렉, `agent_activity_log` |
| P44 | 의존성 취약점 | MED | ❌ | 없음 | pip-audit/npm-audit |
| P45 | 분석 & 리포트 | NICE | 🟡 | `/reports` | 집계 쿼리 |

### 2.1 반영률 요약

| 상태 | 수 | 비율 |
|------|-----|------|
| ✅ 완전 반영 | 2 | 4.4% |
| 🟡 부분 반영 | 9 | 20.0% |
| 🟦 DB만 준비 | 14 | 31.1% |
| ❌ 미반영 | 20 | 44.4% |

**데이터 레이어 기준 준비율 = 55.5%(25/45)** — UI만 그리면 되는 상태가 많음.

---

## 3. AADS 고유 기능 (기획서 외)

| AADS 기능 | 경로/DB | 기획 편입 위치 |
|-----------|--------|----------------|
| CEO Chat (메모리 자동 주입) | `/chat`, `memory_recall.py` | "Directive Chat" 신규 모듈 |
| 브레인스토밍 | `/braming`, `braming_sessions/nodes` | P09 아이디어→아키텍처 연결 |
| FLOW 프레임워크 | `/flow` 4단계 | P35 칸반 스테이지 |
| Managers | `/managers` 6-프로젝트 매니저 AI | P43 Meta-Agent 층 |
| Decisions / Agenda | `/decisions`, `/agenda` | P18 ADR + P07 리뷰 큐 |
| Lessons | `/lessons`, shared-lessons/ | P40 피드백 영구 저장 |
| 메모리 자동 주입 | `memory_facts`(36K+) | 전체 공통 "Context Pill" |

---

## 4. 14주 Phase 로드맵

### Phase 1 (1~2주) — Control Loop 완성
- **AADS-200 P35 태스크 보드 칸반화** (M, sonnet, 2일)
- **AADS-201 P43 에이전트 관리** (M, sonnet, 2일)
- **AADS-202 P36 세션 리플레이 간이판** (M, sonnet, 1.5일)
- **AADS-203 P37 배포 현황** (S, sonnet, 1일)

### Phase 2 (3~4주) — Governance & Safety
P31 정책 관리 / P29 보안 센터 / P33 인시던트 센터 / P22 시크릿 관리 / P23 시크릿 로그 / P41 알림 센터

### Phase 3 (5~8주) — Architecture Visualization
P09 Module View / P11 ERD 뷰어 / P12 ERD Diff / P19 API 카탈로그 / P17 아키텍처 히스토리 / P18 ADR 관리

### Phase 4 (9~11주) — Cost & Quality
P32 비용 센터 / P40 피드백 허브 / P39 프롬프트 레지스트리 / P34 감사 로그 / P27·P28 고도화

### Phase 5 (12~14주) — Ecosystem
P24 MCP 레지스트리 / P25 도구 모니터링 / P44 의존성 취약점 / P14·P15 모듈 / P08·P10 코드맵 / P26 AGENTS.md / P07 문서 리뷰 / P38 환경 설정 / P45 분석 고도화 / P20 API 테스트 / P02 사용자/팀

---

## 5. Phase 1 상세 구현 설계

### 5.1 P35 태스크 보드 칸반화
- 현재: `/tasks/page.tsx` 리스트
- 목표: 5컬럼 칸반 (Backlog/Planning/Running/Review/Done)
- 데이터: `project_tasks`(1,231) + `directive_lifecycle`
- 신규 API: `GET /api/v1/tasks/kanban`, `PATCH /api/v1/tasks/{id}/status`
- 컴포넌트: `KanbanBoard.tsx` + `@dnd-kit`
- 작업 지시 모달 → `pipeline_runner_submit` 재사용

### 5.2 P43 에이전트 관리
- 목표: Meta-Agent(6) + Worker-Agent(Coder/Reviewer/Debater) 통합 레지스트리
- 페이지: `/admin/agents` — 카드 레이아웃
- 설정: 모델 선택, 도구 허용 목록, 활성화 토글
- 백엔드: `runner_model_config`, `agent_activity_log` 연동

### 5.3 P36 세션 리플레이 (간이)
- 페이지: `/tasks/[id]/replay`
- 타임라인: `pipeline_jobs` + `chat_messages` + `commit_log`
- Phase 1 범위: 시간순 이벤트 리스트 (Langfuse 풀 리플레이는 Phase 4)

### 5.4 P37 배포 현황
- 페이지: `/ops/deployments`
- 상단: 서비스 카드 3개 (aads-server/dashboard/postgres) - 커밋 SHA + 시각
- 중단: 최근 배포 10건 테이블
- 하단: 진행 중 파이프라인 (실시간)

### 5.5 공통 인프라
| 작업 | 파일 | 목적 |
|------|------|------|
| Sidebar 재구성 | `components/Sidebar.tsx` | 9-모듈 계층화 |
| Context Pill | `components/ContextPill.tsx` | `memory_facts` 표시 |
| 위험 뱃지 | `components/RiskBadge.tsx` | LOW/MED/HIGH/CRIT |
| 승인 큐 위젯 | `components/ApprovalQueueMini.tsx` | 공통 |

---

## 6. 기술 스택 결정

| 영역 | 기획 제안 | AADS 결정 | 사유 |
|------|----------|-----------|------|
| 오케스트레이션 | LangGraph | **LangGraph 1.0.10 유지** + Pipeline Runner | 이미 도입 |
| 관측성 | Langfuse + AgentOps | **Langfuse 1차** | AgentOps는 Phase 4+ |
| 코드 분석 | dep-cruiser + tree-sitter | **동일 채택** | 오픈소스 |
| 그래프 DB | Neo4j | **PG recursive CTE 1차** | 1만 노드 초과 시 Neo4j |
| 다이어그램 | Eraser + Mermaid | **Mermaid만** | 외부 SaaS 의존 제거 |
| 시크릿 | Infisical | **`.env` + `credential_vault` 유지** | Phase 4+ 검토 |
| MCP | 공식 SDK | **이미 도입** (`app/mcp/`) | 확장 |
| 대시보드 | Next.js + D3 + React Flow | **Next.js 16 + React Flow 신규** | D3는 Recharts 대체 |
| CI/CD | GH Actions + ArgoCD | **pre-commit + deploy.sh bluegreen** | K8s 이관 시 재검토 |

---

## 7. DB 마이그레이션 계획 (Phase 1~2)

```sql
-- P35 태스크 보드
ALTER TABLE project_tasks
  ADD COLUMN IF NOT EXISTS kanban_column VARCHAR(20) DEFAULT 'backlog',
  ADD COLUMN IF NOT EXISTS priority VARCHAR(10) DEFAULT 'P2',
  ADD COLUMN IF NOT EXISTS risk_level VARCHAR(10) DEFAULT 'LOW',
  ADD COLUMN IF NOT EXISTS assigned_agent_id VARCHAR(100);

-- P43 에이전트 레지스트리
CREATE TABLE IF NOT EXISTS agents_registry (
  id VARCHAR(100) PRIMARY KEY,
  name VARCHAR(200) NOT NULL,
  role VARCHAR(50) NOT NULL,
  model_id VARCHAR(100),
  tools_allowed TEXT[],
  status VARCHAR(20) DEFAULT 'active',
  config JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- P31 정책 관리
CREATE TABLE IF NOT EXISTS policies (
  id SERIAL PRIMARY KEY,
  code VARCHAR(30) UNIQUE NOT NULL,  -- R-AUTH, R-COMMIT ...
  scope VARCHAR(30),
  rule_text TEXT NOT NULL,
  risk_level VARCHAR(10),
  active BOOLEAN DEFAULT true,
  version INT DEFAULT 1,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- P18 ADR
ALTER TABLE design_reviews
  ADD COLUMN IF NOT EXISTS adr_status VARCHAR(20) DEFAULT 'proposed',
  ADD COLUMN IF NOT EXISTS superseded_by INT REFERENCES design_reviews(id);
```

**주의**: 컬럼 추가만 허용, DROP/RENAME 금지 (CEO 절대 규칙).

---

## 8. 위험 & 완화

| 위험 | 완화 |
|------|------|
| Sidebar 비대화 (24→45+) | 9-모듈 계층 + 즐겨찾기 + Cmd-K |
| Next.js 빌드 지연 | route group + dynamic import |
| 88 테이블 중복 (projects vs project_tasks) | Phase 2에 view 통합 레이어 |
| React Flow 성능 (>1K 노드) | 클러스터링 + Sigma.js 백업 |
| `--no-verify` 유혹 | R-COMMIT 절대 규칙 + CI 이중 |
| `docker compose up -d` 오남용 | R-DOCKER — `--no-deps <서비스>` |

---

## 9. 성공 지표 (Phase 1 완료 시)

| 지표 | 현재 | Phase 1 목표 |
|------|------|-------------|
| 칸반 보드 사용률 | 0% | ≥80% |
| 러너 리플레이 조회율 | 0% | ≥50% |
| 배포 현황 조회 빈도 | 0회/일 | ≥10회/일 |
| 에이전트 설정 변경 시간 | ~15분 | <1분 |
| 기획서 반영률 | 24.4% | 35.6% |

---

## 10. 다음 액션 (CEO 승인 요청)

Phase 1 Pipeline Runner 4건 즉시 제출 가능:

| Task ID | 제목 | Size | Model | 소요 |
|---------|------|------|-------|------|
| AADS-200 | P35 태스크 보드 칸반화 | M | sonnet | 2일 |
| AADS-201 | P43 에이전트 관리 | M | sonnet | 2일 |
| AADS-202 | P36 세션 리플레이 간이판 | M | sonnet | 1.5일 |
| AADS-203 | P37 배포 현황 페이지 | S | sonnet | 1일 |

- **총 소요**: 6.5 영업일
- **예상 비용**: $25~35 (sonnet 라우트)
- **순차 제출**: 충돌 0, 6~7일
- **병렬 제출** (Worktree 분기): 2~3일, 충돌 리스크 존재

---

## 부록 A. 기존 AADS 자산 재사용 맵

| 기획 페이지 | 바로 쓰는 테이블 | UI 작업량 |
|------------|------------------|-----------|
| P17 아키텍처 히스토리 | `commit_log`(124) | 0.5일 |
| P18 ADR 관리 | `design_reviews`+`debate_sessions` | 1일 |
| P21 API 변경 이력 | `commit_log` 필터 | 0.5일 |
| P22 시크릿 관리 | `llm_api_keys`+`credential_vault.py` | 1일 |
| P23 시크릿 로그 | `llm_key_audit_logs` | 0.5일 |
| P24 MCP 레지스트리 | `app/mcp/` 파일 목록 | 1일 |
| P25 도구 모니터링 | `tool_results_archive` | 1일 |
| P29 보안 센터 | `error_log`+`circuit_breaker_state` | 1.5일 |
| P32 비용 센터 | `cost_tracking`+`task_cost_log`+`bg_llm_usage_log` | 1.5일 |
| P33 인시던트 센터 | `error_log`+`recovery_log`+`alert_history` | 1일 |
| P34 감사 로그 | `oauth_usage_log`+`llm_key_audit_logs` | 1일 |
| P40 피드백 허브 | `response_critiques`+`code_reviews` | 2일 |
| P41 알림 센터 | `alert_history` | 1일 |

**합계: 13개 페이지 × 평균 1일 = 13일** (Phase 2~4 분산)

---

## 부록 B. 변경 이력

| v | 날짜 | 작성자 | 변경 |
|---|------|--------|------|
| 1.0 | 2026-04-24 | CTO AI | 최초 작성, 실측 매핑 + 14주 로드맵 |

끝.
