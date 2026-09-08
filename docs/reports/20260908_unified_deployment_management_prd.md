# AADS 통합 배포관리 시스템 상세 기획, 설계, PRD

- 작성 시각: 2026-09-08 08:32 KST
- 대상: AADS, AADS Dashboard, GO100, KIS, SF, NTV2, NAS, 문서/정적 리포트, DB/설정 배포
- 문서 목적: 모든 배포 경로를 하나의 Ops DB 원장과 상태 화면으로 통합 관리하기 위한 현재 현황, 누락 항목, 목표 아키텍처, 기술스택, PRD, 구축 순서 정의
- 작성 근거:
  - `/root/aads/AGENTS.md`
  - `/root/aads/aads-server/deploy.sh`
  - `/root/aads/aads-dashboard/deploy.sh`
  - `app/api/ops.py`
  - `app/services/deploy_observability.py`
  - `app/services/pipeline_runner_service.py`
  - `migrations/150_deploy_observability_v1.sql`
  - `migrations/161_ops_deploy_request_queue.sql`
  - GO100 remote `scripts/deploy.sh`, `scripts/deploy_frontend_blue_green.sh`, `scripts/go100_deploy_gate.sh`
  - NTV2 remote `deploy.sh`
  - 운영 DB `deploy_runs`, `deploy_phase_events`, `pipeline_jobs` 조회

## 1. 결론

현재 AADS에는 통합 배포관리의 핵심 기반이 일부 구축되어 있다. AADS API는 `deploy_runs/deploy_phase_events` 원장, blue/green, clean SHA build, same-digest standby sync, 5분 P0/P1 모니터링까지 들어가 있다. 채팅 아티팩트와 Ops 화면도 `/api/v1/ops/deploy/status`를 통해 phase, 경과시간, 예상잔여시간, 프로젝트별 요약을 읽는 구조다.

하지만 "모든 배포 통합관리" 기준으로는 아직 완성 상태가 아니다. 운영 DB 실측상 `deploy_runs`는 AADS만 기록하고 있고, GO100/KIS/SF/NTV2/NAS는 프로젝트별 스크립트, 러너 이력, 임시 큐 파일에 흩어져 있다. 특히 프론트 배포, 문서 배포, DB/설정 배포, 외부 프로젝트 배포가 같은 원장에 동일한 품질로 기록되지 않는다.

따라서 목표 구조는 "Chat/Runner는 빠르게 commit/push/deploy request를 등록하고 종료, Ops Deploy Coordinator가 프로젝트별 adapter를 통해 실제 배포를 수행, 모든 phase와 검증 결과를 `deploy_runs` 계열 DB에 남기고, 채팅 아티팩트/Ops/푸시 알림이 같은 원장을 표시"하는 방식이다.

## 2. 현재 실측 현황

### 2.1 운영 DB 기준

| 항목 | 실측값 | 출처 |
|---|---:|---|
| `deploy_runs`에 기록된 프로젝트 | AADS only | 운영 DB 조회, 2026-09-08 08:34 KST |
| AADS 성공 배포 표본 | 9건 | 운영 DB 조회 |
| AADS 성공 배포 평균 | 1,572,534ms, 약 26분 13초 | 운영 DB 조회 |
| AADS 성공 배포 p50 | 1,541,000ms, 약 25분 41초 | 운영 DB 조회 |
| AADS 성공 배포 p90 | 2,028,200ms, 약 33분 48초 | 운영 DB 조회 |
| `deploy_runs` 최근 blocked | AADS 34건 | 운영 DB 조회 |
| `deploy_runs` 최근 failed | AADS 66건 | 운영 DB 조회 |
| `pipeline_jobs` 프로젝트 이력 | AADS, GO100, KIS, SF, NTV2 | 운영 DB 조회 |
| AADS 서버 상태 | `aads-server`, `aads-server-green`, `aads-dashboard` healthy | Docker 조회 |
| contabo116 디스크 사용률 | 89%, 172G/193G | `health_check`, 2026-09-08 08:34 KST |

### 2.2 프로젝트별 현재 배포 체계

| 프로젝트/컴포넌트 | 현재 배포 방식 | 중앙 원장 반영 | 핵심 리스크 |
|---|---|---:|---|
| AADS API | `deploy.sh bluegreen`, clean release archive, Docker image per SHA, candidate health, nginx cutover, standby same digest, 5분 monitoring | 높음 | build/standby sync 시간이 길고 blocked/failed 원장이 많음 |
| AADS Dashboard | 별도 `aads-dashboard/deploy.sh`, blue/green, clean archive, nginx cutover, rollback | 낮음~중간 | API 원장에 component 단위로 first-class 기록되지 않음 |
| AADS 문서/정적 리포트 | `docs/` volume 또는 repo 파일 노출, 일부는 Dashboard public/static | 낮음 | "배포 없이 열림"과 "대시보드 public 반영" 경계가 불명확 |
| GO100 backend | `scripts/deploy.sh`, git pull, backend 변경 시 pip/migration/py_compile/graceful reload | 낮음 | 중앙 `deploy_runs` 미기록, project-local queue file 중심 |
| GO100 frontend | `scripts/deploy_frontend_blue_green.sh --apply`, blue/green unit, queue file, deploy gate | 낮음 | 중앙 원장 미기록, lock stale 시 관제 불일치 가능 |
| KIS | GO100와 같은 저장소/스크립트 경로 공유 | 낮음 | GO100와 repo 공유로 project/component 분리 추적 필요 |
| SF | cafe24/114 쪽 개별 배포 스크립트 추정, 현재 도구 매핑에서 git repo root 확인 실패 | 미확인 | canonical repo path와 배포 entrypoint 표준화 필요 |
| NTV2 frontend | `/srv/newtalk-v2/deploy.sh frontend`, docker compose build 후 `up -d --no-build frontend` | 낮음 | full blue/green/same-digest/central queue 미적용 |
| NTV2 app | Laravel optimize, Reverb restart | 낮음 | app/reverb restart가 중앙 승인/원장에 남지 않음 |
| NAS | 현재 AADS 도구 목록에 별도 프로젝트 타입 없음 | 미확인 | 배포 entrypoint와 health 기준 정의 필요 |
| DB migration | 각 프로젝트 스크립트 또는 수동 적용 | 낮음 | schema deploy가 앱 배포와 원자적으로 연결되지 않음 |
| Prompt/LLMOps config | DB seed/migration/prompt_assets 변경 | 낮음~중간 | 코드 배포 없이 반영되는 설정 변경의 release provenance 약함 |
| PC/Android Agent | 패키지/설치/업데이트 큐 별도 | 낮음 | agent version, rollout target, rollback 원장 필요 |

## 3. 문제 정의

### 3.1 CEO 관점 문제

| 문제 | 현상 | 영향 |
|---|---|---|
| 배포가 어디까지 됐는지 한 화면에서 안 보임 | AADS는 `deploy_runs`, GO100은 queue file/runner, NTV2는 스크립트 로그 | 실제 운영 반영 여부 판단 지연 |
| 프론트 배포 포함 여부가 헷갈림 | AADS Dashboard는 별도 스크립트, GO100 frontend는 별도 BG 스크립트 | 화면 변경이 운영에 빠졌는지 확인 어려움 |
| 채팅 응답이 배포 완료까지 붙잡힘 | 일부 흐름은 개선됐지만 모든 프로젝트에 동일하지 않음 | 채팅 사용성이 떨어지고 중단 시 최종보고 유실 |
| 동시 작업/동시 배포 시 누락 우려 | 프로젝트별 lock/queue가 제각각 | 뒤늦게 큐에 밀리거나 stale lock이 대기로 보임 |
| 변경 요약 추적 부족 | release_sha와 수정 기능, 파일, 검증 결과 연결이 약함 | "무엇이 반영됐는지"를 다시 추적해야 함 |

### 3.2 기술 문제

| 원인 | 현재 근거 | 개선 방향 |
|---|---|---|
| 원장 입력원이 AADS 중심 | `deploy_runs` 프로젝트가 AADS만 존재 | 모든 프로젝트 배포 요청을 `deploy_runs`에 먼저 등록 |
| component 모델 부재 | API/Dashboard/docs/DB/config를 project 단위 하나로 섞음 | `component`, `deploy_type`, `target_env` 추가 |
| 프로젝트별 adapter 부재 | GO100/NTV2/SF가 각자 shell script 호출 | Deploy Adapter Registry 도입 |
| 큐/락 이중화 | AADS는 DB/flock, GO100은 `/tmp/*.jsonl`와 flock | DB queue를 source of truth로 통합 |
| 상태/시간 계산 불일치 | `deploy_runs`는 phase 기준, dashboard script는 log 파일 기준 | phase event 표준 contract 적용 |
| 실패 재조정 자동화 부족 | blocked/failed 원장 다수 | stale lock/run reconcile worker와 승인 정책 필요 |
| 문서 배포 경계 불명확 | `docs` volume mount와 dashboard public 배포가 혼재 | docs publish pipeline을 별도 component로 정의 |

## 4. 통합관리 목표

### 4.1 목표

1. 모든 배포 요청은 `deploy_runs`에 먼저 생성한다.
2. Chat/Runner는 배포 완료까지 기다리지 않고 `deploy_run_id`, 상태 URL, 예상 큐 위치를 반환한다.
3. 실제 배포는 Ops Deploy Coordinator가 프로젝트별 adapter로 수행한다.
4. API, frontend, worker, static docs, DB migration, config/prompt, agent package를 모두 component 단위로 기록한다.
5. 동일 프로젝트/컴포넌트에서 동시 요청이 오면 최신 SHA만 실행하고 이전 queued SHA는 `superseded` 처리한다.
6. 배포 완료는 candidate health, routed health, same digest 또는 프로젝트별 equivalent gate, post QA, monitoring까지 통과해야만 `success/completed`로 기록한다.
7. CEO 화면에는 phase, 시작시간, 종료시간, 경과시간, 예상잔여시간, 변경 기능 요약, 수정 파일, 검증 결과, rollback 가능성을 표시한다.

### 4.2 비목표

| 제외 | 이유 |
|---|---|
| active API 직접 restart 허용 | SSE/채팅 유실과 AADS 배포 규칙 위반 |
| 모든 프로젝트를 무조건 Docker blue/green으로 강제 | KIS/GO100 backend, NTV2 Laravel처럼 런타임 특성이 다름 |
| 5분 P0/P1 monitoring 삭제 | `/root/aads/AGENTS.md` release certification 조건 위반 |
| dirty worktree 포함 배포 허용 | 수정 코드 누락/추적 불가의 직접 원인 |
| force push, DROP/TRUNCATE, full compose stack deploy | 운영 금지 규칙 |

## 5. 대상 배포 유형 표준화

| deploy_type | 대상 | 표준 처리 |
|---|---|---|
| `api_bluegreen` | AADS API, Docker API 서비스 | clean SHA image build, candidate health, nginx cutover, standby sync |
| `dashboard_bluegreen` | AADS Dashboard, GO100 frontend | clean SHA build, inactive slot start, external route check, rollback |
| `backend_graceful_reload` | GO100/KIS gunicorn backend | py_compile/test, migration guard, `HUP` reload, health check |
| `docker_service_replace` | NTV2 frontend 등 compose 단일 서비스 | build, `up -d --no-build <service>`, health, rollback image tag |
| `php_optimize_reload` | NTV2 Laravel app | config/cache/route optimize, queue/reverb safe reload |
| `worker_restart` | SF/worker/queue services | drain, restart single worker, job queue health |
| `static_docs_publish` | `docs/reports`, `app/static/reports`, dashboard public reports | file existence, doc API access, route capture/API check |
| `db_migration` | PostgreSQL migrations, prompt_assets seed | dry-run/transaction/backout SQL, before/after SELECT |
| `config_prompt_release` | L1~L5 prompt assets, model routing config | DB upsert, provenance compile check, rollback snapshot |
| `agent_package_release` | PC Agent, Android Agent | version artifact, staged rollout, device heartbeat check |

## 6. 목표 아키텍처

```text
CEO Chat / Admin UI / Runner / CLI
  |
  +-- POST /api/v1/ops/deploy/requests
        |
        +-- deploy_runs row 생성
        +-- deploy_components rows 생성
        +-- release_manifest 저장
        +-- chat은 deploy_run_id 반환 후 종료
        |
        v
Ops Deploy Coordinator
  |
  +-- DB lease claim: project + component + target_env
  +-- stale run/lock reconcile
  +-- adapter 선택
        |
        +-- AADS API adapter -> /root/aads/aads-server/deploy.sh bluegreen
        +-- AADS Dashboard adapter -> /root/aads/aads-dashboard/deploy.sh
        +-- GO100 backend adapter -> scripts/deploy.sh backend/graceful
        +-- GO100 frontend adapter -> scripts/deploy_frontend_blue_green.sh --apply
        +-- NTV2 adapter -> /srv/newtalk-v2/deploy.sh frontend/app/all
        +-- SF/NAS adapter -> 표준 entrypoint 확정 후 연결
        +-- DB/config/docs adapter
        |
        v
deploy_phase_events + deploy_logs + deploy_artifacts
        |
        v
/api/v1/ops/deploy/status
        |
        +-- Chat artifact deploy tab
        +-- Ops dashboard
        +-- Push/voice notification
        +-- HANDOVER/release note generator
```

## 7. 기술스택

| 계층 | 기술 | 역할 |
|---|---|---|
| API | FastAPI 0.115 | 배포 요청, 상태, 승인, reconcile API |
| DB | PostgreSQL 15 | deploy 원장, phase event, component, lock, release manifest |
| Queue | PostgreSQL `FOR UPDATE SKIP LOCKED` + advisory lock | 프로젝트/컴포넌트 직렬화, 최신 SHA supersede |
| Runner | Pipeline Runner | 코드 수정, 검증, 커밋, 푸시, deploy request 등록 |
| Workflow | LangGraph | 장기 배포 workflow checkpoint, interrupt/resume, 승인 단계 |
| Trace | LangSmith-compatible internal trace, Langfuse optional | LLMOps 진단, tool/run/deploy 연결 |
| Container | Docker Compose | AADS API/Dashboard, NTV2 frontend 등 |
| Routing | nginx | 짧은 cutover lock, external routed health |
| Remote exec | SSH/MCP adapters | GO100/KIS/SF/NTV2 remote deploy 호출 |
| UI | Next.js 16 | 채팅 아티팩트 배포 탭, Ops 통합 배포 화면 |
| Notification | Web Push, Telegram, voice/TTS hook | 배포 완료/실패/승인 필요 알림 |
| Test | pytest, shell contract tests, Playwright/capture | 배포 계약, API, 화면 검증 |

## 8. DB 설계

### 8.1 기존 테이블 유지

| 테이블 | 역할 | 보강 필요 |
|---|---|---|
| `deploy_runs` | 배포 run 단위 원장 | component/target/env/manifest 필드 부족 |
| `deploy_phase_events` | phase timeline | adapter 표준 phase 확장 |
| `deploy_history` | legacy history | 읽기/마이그레이션 참조만 유지 |
| `pipeline_jobs` | 코드수정/러너 작업 | deploy_run_id 역참조 강화 |

### 8.2 권장 신규/확장 스키마

```sql
ALTER TABLE deploy_runs
  ADD COLUMN IF NOT EXISTS component TEXT DEFAULT 'api',
  ADD COLUMN IF NOT EXISTS deploy_type TEXT DEFAULT 'api_bluegreen',
  ADD COLUMN IF NOT EXISTS target_env TEXT DEFAULT 'production',
  ADD COLUMN IF NOT EXISTS release_title TEXT,
  ADD COLUMN IF NOT EXISTS release_summary TEXT,
  ADD COLUMN IF NOT EXISTS rollback_plan TEXT,
  ADD COLUMN IF NOT EXISTS approval_policy TEXT DEFAULT 'auto_if_green';

CREATE TABLE IF NOT EXISTS deploy_components (
  id BIGSERIAL PRIMARY KEY,
  deploy_run_id BIGINT REFERENCES deploy_runs(id) ON DELETE CASCADE,
  project TEXT NOT NULL,
  component TEXT NOT NULL,
  deploy_type TEXT NOT NULL,
  release_sha TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  phase TEXT NOT NULL DEFAULT 'queued',
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  duration_ms BIGINT,
  health_url TEXT,
  route_url TEXT,
  image_digest TEXT,
  standby_digest TEXT,
  log_path TEXT,
  error_summary TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS deploy_release_manifests (
  id BIGSERIAL PRIMARY KEY,
  deploy_run_id BIGINT REFERENCES deploy_runs(id) ON DELETE CASCADE,
  project TEXT NOT NULL,
  release_sha TEXT NOT NULL,
  title TEXT,
  summary TEXT,
  changed_files JSONB NOT NULL DEFAULT '[]'::jsonb,
  tests JSONB NOT NULL DEFAULT '[]'::jsonb,
  commits JSONB NOT NULL DEFAULT '[]'::jsonb,
  risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS deploy_locks (
  project TEXT NOT NULL,
  component TEXT NOT NULL,
  target_env TEXT NOT NULL DEFAULT 'production',
  deploy_run_id BIGINT,
  owner_instance TEXT,
  owner_epoch TEXT,
  lease_expires_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY(project, component, target_env)
);
```

### 8.3 상태 표준

| status | 의미 | CEO 표시 |
|---|---|---|
| `queued` | 실행 전 대기 | 대기 |
| `claimed` | worker가 소유권 확보 | 준비 중 |
| `running` | 배포 실행 중 | 진행 중 |
| `verifying` | health/QA/monitoring | 검증 중 |
| `syncing_standby` | blue/green standby 동기화 | B/G 동기화 |
| `success` | 인증 완료 | 완료 |
| `failed` | 실패, 기존 active 유지 또는 rollback 완료 | 실패 |
| `blocked` | preflight 차단 | 차단 |
| `superseded` | 더 최신 SHA로 대체 | 최신 배포로 대체 |
| `cancelled` | 승인 취소/수동 중단 | 취소 |

## 9. API 설계

### 9.1 배포 요청

`POST /api/v1/ops/deploy/requests`

```json
{
  "project": "AADS",
  "component": "dashboard",
  "deploy_type": "dashboard_bluegreen",
  "release_sha": "a54bd927451c",
  "runner_job_id": "runner-xxxx",
  "requested_by": "pipeline_runner",
  "request_source": "chat_artifact",
  "commit_status": "committed",
  "push_status": "pushed",
  "auto_start": true,
  "metadata": {
    "title": "docs route bootstrap",
    "changed_files": ["src/middleware.ts"],
    "tests": ["npm run lint"]
  }
}
```

반환:

```json
{
  "status": "queued",
  "deploy_run_id": 140,
  "project": "AADS",
  "component": "dashboard",
  "release_sha": "a54bd927451c",
  "queue_position": 1,
  "deduplicated": false,
  "worker_start": {
    "started": true,
    "status": "started"
  },
  "next_check": "/api/v1/ops/deploy/status"
}
```

### 9.2 상태 조회

`GET /api/v1/ops/deploy/status`

필수 응답 필드:

| 필드 | 내용 |
|---|---|
| `active_deployments` | 현재 진행 중인 배포 |
| `queued_deployments` | 대기 중 배포 |
| `project_deployments` | 프로젝트별 최신 상태 |
| `component_deployments` | 프로젝트/컴포넌트별 최신 상태 |
| `recent_completed_deployments` | 최근 완료 배포와 변경 요약 |
| `recent_durations_per_project` | 프로젝트별 배포시간 |
| `phase_timeline` | phase 시작/종료/경과/예상잔여 |
| `stale_zombie_signals` | 러너/락/원장 불일치 |
| `next_deploy_readiness` | 다음 배포 가능 여부와 차단 사유 |

### 9.3 수동 제어

| API | 용도 | 승인 |
|---|---|---|
| `POST /ops/deploy/{id}/approve` | 위험 배포 승인 | CEO |
| `POST /ops/deploy/{id}/cancel` | 대기 배포 취소 | CEO/CTO |
| `POST /ops/deploy/{id}/retry` | 실패 배포 재시도 | CTO, preflight green |
| `POST /ops/deploy/reconcile` | stale run/lock 정리 | dry-run 기본, apply는 승인 |
| `GET /ops/deploy/{id}/logs` | 배포 로그 조회 | admin |

## 10. Deploy Adapter 설계

### 10.1 Adapter 공통 인터페이스

```python
class DeployAdapter(Protocol):
    project: str
    component: str
    deploy_type: str

    async def preflight(self, request: DeployRequest) -> PreflightResult: ...
    async def start(self, request: DeployRequest) -> DeployStartResult: ...
    async def poll(self, deploy_run_id: int) -> DeployPollResult: ...
    async def verify(self, deploy_run_id: int) -> VerifyResult: ...
    async def rollback(self, deploy_run_id: int) -> RollbackResult: ...
```

### 10.2 Adapter별 적용

| Adapter | 실행 명령 | 필수 gate |
|---|---|---|
| `AadsApiBlueGreenAdapter` | `bash /root/aads/aads-server/deploy.sh bluegreen` | clean SHA, one image per SHA, candidate health, same digest, monitoring |
| `AadsDashboardBlueGreenAdapter` | `bash /root/aads/aads-dashboard/deploy.sh` | clean SHA, inactive slot, nginx cutover, external `/login` health |
| `Go100BackendAdapter` | `bash scripts/deploy.sh` 또는 backend-only mode 추가 | dirty gate, py_compile, migration guard, gunicorn HUP, health |
| `Go100FrontendAdapter` | `bash scripts/deploy_frontend_blue_green.sh --apply` | GO100 deploy gate, inactive slot, asset verify, external route health |
| `KisBackendAdapter` | KIS 전용 backend command 분리 필요 | trading process drain, market-time guard, health |
| `Ntv2FrontendAdapter` | `bash /srv/newtalk-v2/deploy.sh frontend` | build, `up -d --no-build`, HTTP 200, rollback tag |
| `Ntv2AppAdapter` | `bash /srv/newtalk-v2/deploy.sh app` | Laravel optimize, Reverb health |
| `SfAdapter` | canonical path 확정 후 등록 | repo clean, worker queue drain, health |
| `DocsPublishAdapter` | file mount/API/public sync check | file exists, `/docs` content API, dashboard route |
| `DbMigrationAdapter` | migration runner | transaction, before/after SELECT, rollback SQL |
| `PromptConfigAdapter` | prompt asset upsert | compiled provenance check |

## 11. UI/UX 설계

### 11.1 채팅 옆 아티팩트 배포 탭

첫 화면에서 보여야 할 정보:

| 영역 | 표시 |
|---|---|
| 전체 상태 | 다음 배포 가능/차단, active count, queued count |
| 진행 카드 | project, component, phase, started_at, elapsed, estimated_remaining |
| 프로젝트별 현황 | AADS/API, Dashboard, GO100 backend/frontend, KIS, SF, NTV2, NAS |
| 최근 반영 내역 | 기능명, release_sha, 변경 파일 수, 주요 파일, 검증 결과 |
| 문제 신호 | stale runner, stale lock, blocked preflight, digest mismatch |
| 액션 | 새로고침, 상세 보기, 로그 보기, 승인/취소 |

### 11.2 Ops 통합 배포 화면

```text
[배포 통합 관제]
  - 전체: Ready / Blocked / Deploying
  - 프로젝트 필터: AADS GO100 KIS SF NTV2 NAS
  - 컴포넌트 필터: api dashboard frontend backend worker docs db config agent

[현재 진행]
  project | component | phase | 시작 | 경과 | 예상잔여 | SHA | worker

[대기 큐]
  position | project | component | release title | requested_by | supersede policy

[최근 반영]
  완료시각 | project | component | 기능 요약 | 수정 파일 | 검증 | rollback

[진단]
  stale lock | failed phase | failed reason | recommended action
```

### 11.3 알림

| 이벤트 | 알림 |
|---|---|
| 배포 큐 등록 | 채팅 시스템 메시지 + 아티팩트 배포 탭 업데이트 |
| 승인 필요 | 푸시/텔레그램/채팅 badge |
| cutover 완료 | push 알림, 아직 release certified 전임을 명시 |
| release certified | push + 음성 안내 |
| failed/rollback | push + 음성 안내 + 다음 조치 카드 |

## 12. PRD

### 12.1 제품명

AADS Unified Deployment Control Plane v1

### 12.2 사용자

| 사용자 | 요구 |
|---|---|
| CEO | 채팅을 기다리지 않고 모든 프로젝트 배포 진행/완료/실패를 즉시 확인 |
| PM/CTO AI | 수정 코드가 빠짐없이 커밋/푸시/배포됐는지 원장으로 검증 |
| Pipeline Runner | 코드 수정 후 배포를 직접 오래 잡지 않고 DB 큐에 등록 |
| Ops Worker | 배포를 직렬화하고 phase별 검증/롤백을 자동 수행 |
| 프로젝트 운영자 | 프로젝트별 특수 배포 절차를 표준 adapter로 유지 |

### 12.3 기능 요구사항

| ID | 요구사항 | 우선순위 | 완료 기준 |
|---|---|---:|---|
| FR-001 | 모든 배포 요청을 `deploy_runs`에 등록 | P0 | AADS 외 프로젝트도 DB row 생성 |
| FR-002 | project/component/deploy_type 단위 상태 관리 | P0 | API/Dashboard/Docs/DB/Config 구분 표시 |
| FR-003 | commit/push/deploy request를 채팅 응답과 분리 | P0 | 채팅은 10초 내 `deploy_run_id` 반환 |
| FR-004 | 동일 project+component 동시 배포 직렬화 | P0 | active 1건, queued N건, stale lock 0건 |
| FR-005 | 최신 SHA supersede | P0 | 과거 queued SHA 자동 `superseded` |
| FR-006 | 시작/종료/경과/예상잔여시간 표시 | P0 | 채팅 아티팩트와 Ops 모두 표시 |
| FR-007 | 변경 기능/수정 파일/검증 결과 표시 | P0 | release manifest 기반 최근 반영 내역 표시 |
| FR-008 | AADS Dashboard 배포를 first-class component로 등록 | P0 | Dashboard 배포도 `deploy_runs` 또는 `deploy_components`에 기록 |
| FR-009 | GO100 backend/frontend adapter 연결 | P0 | 중앙 큐에서 GO100 배포 요청/상태/완료 추적 |
| FR-010 | NTV2 adapter 연결 | P1 | frontend/app 배포 phase 기록 |
| FR-011 | SF/NAS canonical path와 adapter 등록 | P1 | repo path/health/deploy command 문서화 및 원장 연결 |
| FR-012 | DB/config/prompt 배포 원장화 | P1 | migration/config 변경의 before/after 검증 기록 |
| FR-013 | stale deploy/runner reconcile | P1 | dry-run/apply 분리, 승인 필요 항목 표시 |
| FR-014 | LangGraph/LangSmith-compatible trace 연결 | P1 | graph_run_id, trace_id, deploy_run_id 상호 참조 |
| FR-015 | release note/HANDOVER 자동 생성 | P2 | 배포 성공 시 문서 기록 draft 생성 |

### 12.4 비기능 요구사항

| 항목 | 기준 |
|---|---|
| 안정성 | 기존 active 슬롯은 candidate health 전 건드리지 않음 |
| 추적성 | release_sha, runner_job_id, deploy_run_id, component_id 연결 |
| 응답성 | 배포 요청 API p95 3초 이하, 채팅 최종응답 10초 이내 |
| 안전성 | dirty worktree, unpushed commit, full compose, active restart 차단 |
| 복구성 | cutover 실패 시 자동 rollback, failed phase와 rollback 결과 기록 |
| 관찰성 | phase duration, log path, error_summary, retry count, worker owner 기록 |
| 보안 | 시크릿 출력 금지, DB destructive command 금지, 승인 필요한 작업 분리 |
| 확장성 | 프로젝트 추가 시 adapter registry 설정만으로 연결 |

### 12.5 성공 지표

| 지표 | 현재 | 목표 |
|---|---:|---:|
| AADS 외 프로젝트 `deploy_runs` coverage | 0건 | 100% |
| 채팅 배포 요청 반환 시간 | 일부 장기 대기 | 10초 이내 |
| dirty 포함 배포 | gate별 편차 | 0건 |
| release SHA 추적 불가 배포 | 존재 가능 | 0건 |
| 프론트 반영 여부 미확인 | 존재 | 0건 |
| stale lock으로 30분 이상 대기 | 발생 이력 있음 | 0건 |
| 배포 후 변경 요약 표시 | 일부 | 100% |

## 13. 구축 단계

### Phase 0: 표준 확정

| 작업 | 산출물 | 검증 |
|---|---|---|
| 프로젝트별 deploy inventory 확정 | adapter matrix | 모든 프로젝트 command/path/health 확인 |
| deploy component taxonomy 확정 | deploy_type enum | API/UI 문서 반영 |
| 위험 정책 확정 | approval_policy 표 | dirty/DB/market-time/full-compose 차단 |

### Phase 1: DB/API 기반

| 작업 | 대상 | 검증 |
|---|---|---|
| `deploy_runs` component 확장 | migration | before/after schema SELECT |
| `deploy_components` 추가 | migration | insert/select unit test |
| release manifest 저장 | `deploy_observability.py` | changed_files/title 표시 |
| `/ops/deploy/requests` schema 확장 | `app/api/ops.py` | request/response test |
| status response 확장 | `get_deploy_status` | `component_deployments` test |

### Phase 2: AADS/Dashboard 통합

| 작업 | 대상 | 검증 |
|---|---|---|
| AADS API adapter 정식화 | 기존 `deploy.sh` | dry-run/status test |
| Dashboard adapter 정식화 | `/root/aads/aads-dashboard/deploy.sh` | Dashboard row 생성 |
| dashboard deploy log DB 기록 | deploy script wrapper | started/completed 표시 |
| docs publish component | docs API/file mount | 문서 딥링크 열림 확인 |

### Phase 3: GO100/KIS 통합

| 작업 | 대상 | 검증 |
|---|---|---|
| GO100 backend/frontend adapter | contabo14 | 중앙 큐에서 request 가능 |
| GO100 local queue bridge | `/tmp/go100*.jsonl` -> DB event | stale lock 재현 test |
| KIS 분리 | 같은 repo 내 component 분리 | GO100 작업과 KIS 배포 충돌 없음 |
| market-time guard | KIS/GO100 | 장중 위험 deploy 승인 필요 |

### Phase 4: SF/NTV2/NAS 통합

| 작업 | 대상 | 검증 |
|---|---|---|
| SF canonical path 확정 | cafe24_114 | git status/deploy script 확인 |
| NTV2 adapter | `/srv/newtalk-v2/deploy.sh` | frontend/app phase 기록 |
| NAS adapter 정의 | cafe24_114 | health/deploy command 확정 |
| worker restart policy | SF/NAS workers | queue drain 후 restart |

### Phase 5: LLMOps 진단/시연

| 작업 | 대상 | 검증 |
|---|---|---|
| LangGraph run 연결 | runner/deploy workflow | graph_run_id -> deploy_run_id |
| LangSmith-compatible trace | `ohvis_harness_traces` | trace detail 화면 |
| failure replay set | failed deploy phase | 오류 재현/개선 결과 |
| CEO 시연 | Ops 화면 + chat artifact | 큐 등록, 진행, 성공, 실패 시연 |

## 14. 배포시간 단축 전략

### 14.1 단축 가능한 영역

| 영역 | 단축안 | 안전성 |
|---|---|---|
| 채팅 대기시간 | 배포 완료 대기 제거, 큐 등록 후 응답 종료 | 높음 |
| 중복 배포 | 최신 SHA supersede, 같은 component queued 1건 유지 | 높음 |
| build context | clean archive, context size gate, cache 관리 | 높음 |
| 프론트 build | dashboard/GO100 frontend 독립 배포, 변경 파일 기반 선택 | 중간 |
| standby sync | stale stream classifier, 실제 live stream만 보호 | 중간 |
| 검증 병렬화 | candidate health, static checks, manifest checks 병렬 | 중간 |

### 14.2 줄이면 안 되는 영역

| 영역 | 사유 |
|---|---|
| candidate health 전 cutover | 장애 유입 |
| routed health | 실제 사용자 경로 검증 필수 |
| same digest standby | rollback/standby 신뢰성 |
| 5분 P0/P1 monitoring | release certified 필수 계약 |
| dirty/clean SHA gate | 수정 코드 누락 방지 핵심 |

## 15. 동시 작업/동시 배포 흐름

### 15.1 정상 흐름

```text
세션 A: AADS API 수정 -> commit/push -> deploy_runs(AADS/api/SHA1 queued)
세션 B: AADS Dashboard 수정 -> commit/push -> deploy_runs(AADS/dashboard/SHA2 queued)
세션 C: GO100 frontend 수정 -> commit/push -> deploy_runs(GO100/frontend/SHA3 queued)

Coordinator:
  - AADS/api와 AADS/dashboard는 nginx cutover lock만 공유하고 build는 분리
  - 같은 project+component는 1건만 active
  - 같은 component에 더 최신 SHA가 오면 이전 queued는 superseded
  - 각 run은 phase와 health를 DB에 기록
```

### 15.2 B/G 동기화 중 새 배포

| 상황 | 처리 |
|---|---|
| 같은 project+component 새 SHA | 즉시 실행하지 않고 queued, 이전 queued는 superseded |
| 다른 component | shared resource가 다르면 preflight 후 진행, nginx cutover만 짧게 직렬화 |
| same digest sync 진행 중 | active slot 보호, 새 배포는 candidate build 가능 여부만 adapter 정책으로 판단 |
| running lock stale | dry-run reconcile 후 worker lease 만료 시 자동 정리 후보 |

## 16. 위험과 대안

| 리스크 | 영향 | 대안 |
|---|---|---|
| 프로젝트별 스크립트 품질 편차 | 중앙 원장에는 success지만 실제 health 불일치 가능 | adapter별 verify contract 강제 |
| SF/NAS 경로 미확정 | 통합 누락 | Phase 0에서 canonical path/health 먼저 확정 |
| GO100/KIS 같은 repo 공유 | project 간 dirty/commit 충돌 | component lock + project path ownership |
| Dashboard와 API nginx 공유 | cutover race | build는 병렬, nginx lock은 cutover 순간만 공유 |
| DB migration 실패 | 앱 배포 후 schema mismatch | expand/contract migration, before/after SELECT, rollback SQL |
| 알림 과다 | CEO 피로도 증가 | queued/started/certified/failed만 push, phase 상세는 화면 |

## 17. 검증 기준

### 17.1 단위 검증

| 검증 | 명령/도구 |
|---|---|
| schema migration idempotent | `psql` before/after SELECT |
| deploy request enqueue | pytest `test_deploy_observability.py` |
| latest SHA supersede | queued SHA 2건 삽입 후 최신 1건 active |
| adapter registry | project/component별 command resolution test |
| dirty gate | clean/dirty fixture test |

### 17.2 통합 검증

| 검증 | 완료 기준 |
|---|---|
| AADS API 배포 | `deploy_runs` success, same digest, health 200 |
| AADS Dashboard 배포 | component row success, `/login` external 200 |
| 문서 배포 | `/docs?...file_path=...` 로그인 후 열림, API fallback 200 |
| GO100 frontend | 중앙 row + remote BG log + external health |
| GO100 backend | central row + gunicorn reload + `/health` 200 |
| NTV2 frontend/app | central row + route health |
| 실패 시나리오 | candidate health 실패 시 rollback 기록 |

### 17.3 화면 검증

| 화면 | 기준 |
|---|---|
| 채팅 아티팩트 배포 탭 | 모든 프로젝트/컴포넌트 현황, 시작/끝/경과/예상잔여 표시 |
| Ops 배포 화면 | active/queued/recent/failed/stale/reconcile 표시 |
| 최근 반영 내역 | 기능명, 수정 파일, 검증, release_sha 표시 |
| 모바일 | 카드 텍스트 겹침 없음, 주요 상태 한 화면 확인 |

## 18. 승인 전 결정 필요 사항

| 결정 | 권장안 |
|---|---|
| 중앙 원장 source of truth | `deploy_runs + deploy_components` |
| project/component lock 단위 | `project + component + target_env` |
| AADS Dashboard first-class 여부 | 즉시 포함 |
| GO100/KIS 중앙 큐 강제 여부 | GO100 frontend/backend부터 P0 포함 |
| SF/NAS 선행 작업 | canonical path/entrypoint 확인 후 P1 |
| 외부 LangSmith SaaS 사용 | 내부 compatible trace 우선, 외부 SaaS는 opt-in |
| 배포시간 단축 우선순위 | 채팅 대기 제거, 중복 supersede, build context/cache, standby stale 분류 순 |

## 19. 즉시 구축 권장안

| 우선순위 | 작업 | 예상 파일/대상 |
|---|---|---|
| P0-1 | `deploy_runs`에 component/deploy_type/manifest 필드 추가 | migration, `deploy_observability.py` |
| P0-2 | AADS Dashboard 배포를 `/ops/deploy/requests`에 first-class 등록 | `app/api/ops.py`, dashboard deploy wrapper |
| P0-3 | GO100 backend/frontend adapter를 중앙 원장에 연결 | `deploy_observability.py`, remote command adapter |
| P0-4 | 채팅 아티팩트 배포 탭에 component별 카드/최근 반영 요약 완성 | `ChatArtifactPanel.tsx` |
| P0-5 | stale lock/run reconcile dry-run API 추가 | ops API, deploy lock service |
| P1-1 | NTV2/SF/NAS adapter 정리 | remote path inventory, adapter registry |
| P1-2 | DB/config/prompt release provenance 연결 | prompt compiler, migration runner |
| P1-3 | LangGraph/LangSmith-compatible trace 연결 | ohvis harness, pipeline runner |

## 20. 완료 정의

통합 배포관리 v1은 아래 조건이 모두 충족되어야 완료다.

1. AADS API, AADS Dashboard, GO100 backend, GO100 frontend 배포가 중앙 `deploy_runs/deploy_components`에 기록된다.
2. 채팅/러너는 배포 완료까지 기다리지 않고 `deploy_run_id`를 반환한다.
3. 채팅 아티팩트 배포 탭과 Ops 화면에서 모든 프로젝트의 active/queued/recent 상태를 볼 수 있다.
4. 배포 카드에 phase, 시작시간, 종료시간, 경과시간, 예상잔여시간, release title, changed files, 검증 결과가 표시된다.
5. dirty/unpushed/full-compose/active-restart 배포가 gate에서 차단된다.
6. B/G 동기화 중 새 배포 요청은 같은 component면 queued/superseded로 처리되고, 다른 component면 shared resource만 짧게 직렬화된다.
7. 실패 배포는 failed phase, error_summary, rollback 결과, 다음 조치가 원장에 남는다.
8. 최소 1회 AADS API, AADS Dashboard, GO100 frontend 배포 시나리오가 운영 또는 staging-equivalent에서 검증된다.

## 21. 이번 문서 작성 상태

| 항목 | 상태 |
|---|---|
| 상세 기획 | 완료 |
| 설계/기술스택 | 완료 |
| PRD | 완료 |
| 화면 구성안 | 완료 |
| 구축 작업 | Phase 1 일부 반영: component/deploy_type/target_env 원장 확장, release manifest, component별 상태 API, 채팅/Ops 표시 |
| 커밋/푸시/배포 | 구현 반영 단계에서 별도 수행 |

## 22. 승인 후 즉시 구현 반영 상태

| 항목 | 반영 상태 | 검증 |
|---|---|---|
| DB 원장 확장 | `deploy_runs` component 필드, `deploy_components`, `deploy_release_manifests`, `deploy_locks` 추가 | `migrations/162_unified_deploy_components.sql` 운영 DB 적용 |
| 배포 요청 API | `/api/v1/ops/deploy/requests`가 `component`, `deploy_type`, `target_env`, `rollback_plan`, `approval_policy` 수신 | `python3 -m py_compile app/api/ops.py` |
| 상태 API | `/api/v1/ops/deploy/status`가 `project_deployments`, `component_deployments` 반환 | `tests/unit/test_deploy_observability.py` |
| 배포 스크립트 | `deploy.sh`가 직접/큐 배포 모두 component row 생성 및 phase 동기화 | `bash -n deploy.sh`, 단위 테스트 |
| 채팅 아티팩트 | 배포 탭에 프로젝트별/컴포넌트별 현황, 시작/종료/경과/잔여, 변경 요약 표시 | `npm run lint` |
| Ops 화면 | 공통 배포 관제에 component별 최신 배포 카드 추가 | `npm run lint` |

남은 구축 범위는 GO100/KIS/SF/NTV2/NAS 원격 deploy adapter가 실제 중앙 worker에서 실행되는 단계다. 이번 반영은 그 adapter들이 중앙 원장에 안전하게 들어올 수 있는 DB/API/UI 기반을 먼저 완성한 것이다.
