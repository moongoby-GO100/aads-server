# 전 프로젝트 Ops/스킬/도구 아키텍처 전수 검수 및 공용화 기획서

- 작성 시각: 2026-09-07 09:11 KST
- 대상 프로젝트: AADS, KIS, GO100, SF, NTV2, NAS
- 작성 목적: 프로젝트별로 흩어진 Ops, 운영 스킬, 도구, 러너, 문서, 자동화 체계를 공용 모듈로 재사용할 수 있는 구조를 설계한다.
- 결론: 공통 운영 프롬프트와 중앙 도구 실행 기반은 이미 AADS에 존재한다. 그러나 각 프로젝트의 실제 Ops 실행 계층은 AADS/GO100 중심이고, SF/NTV2/NAS는 문서와 cron/파이프라인 중심이라 공용 Ops SDK와 프로젝트 어댑터 레이어가 필요하다.

## 1. 실측 요약

| 항목 | 실측 결과 | 출처 |
|---|---:|---|
| AADS 활성 컨테이너 | `aads-server`, `aads-server-green`, `aads-dashboard`, `aads-dashboard-green` 모두 healthy | `docker ps`, 2026-09-07 09:11 KST |
| AADS `/ops` API 엔드포인트 | 59개 | `rg -c "@router" app/api/ops.py` |
| AADS `pipeline_runner` API 엔드포인트 | 11개 | `rg -c "@router" app/api/pipeline_runner.py` |
| AADS `pc_agent` API 엔드포인트 | 16개 | `rg -c "@router" app/api/pc_agent.py` |
| AADS `loops` API 엔드포인트 | 8개 | `rg -c "@router" app/api/loops.py` |
| AADS tool registry 도구명 | 135개 | `rg -c '"name":' app/services/tool_registry.py` |
| prompt assets 총량 | 141건, 활성 140건 | DB `prompt_assets` |
| prompt asset layer별 | L1 12, L2 9, L3 95, L4 11, L5 14 | DB `prompt_assets` |
| Ops/SRE 관련 활성 prompt asset | 27건 | DB `prompt_assets` |
| tool 결과 아카이브 | 7,048건 | DB `tool_results_archive` |
| AI 관찰 원장 | 798건 | DB `ai_observations` |
| 세션 노트 | 1,135건 | DB `session_notes` |
| pipeline job 누적 | GO100 316, AADS 242, NTV2 91, KIS 9, SF 2 | DB `pipeline_jobs` |
| 현재 running runner | GO100 1건 | `pipeline_runner_status`, 2026-09-07 09:11 KST |
| KIS 서비스 | `kis-v41-api` active | SSH `systemctl is-active kis-v41-api` |
| GO100 서비스 | `go100` active, `go100-frontend` inactive | SSH `systemctl is-active` |
| 114/116.120 서버 컨테이너 | NTV2, ShortFlow, Redis, MySQL, n8n 구동 | SSH `docker ps` |

## 2. 현재 구현 아키텍처

### 2.1 AADS 중앙 Ops 계층

AADS는 현재 6개 프로젝트를 통제하는 중앙 Ops Control Plane 역할을 수행한다.

| 계층 | 구현 파일/대상 | 현재 역할 |
|---|---|---|
| 운영 API | `app/api/ops.py` | 헬스, 락, 작업 원장, 비용, 서버 상태, 복구 로그, 회로 차단기, active stream 확인 |
| 러너 API | `app/api/pipeline_runner.py` | 작업 제출, 중복/파일충돌 차단, 승인, 배포, 모델 라우팅, 배치 제출 |
| 러너 실행 | `app/services/pipeline_runner_service.py` | Claude/Codex/LiteLLM 후보 실행, AI 리뷰, 승인 후 배포, watchdog |
| 도구 레지스트리 | `app/services/tool_registry.py` | 135개 도구 스키마, eager/deferred loading, intent별 도구 매핑 |
| 도구 실행기 | `app/services/tool_executor.py` | DB/SSH/Git/Runner/브라우저/미디어/팩트체크 도구 dispatch |
| MCP 브리지 | `mcp_servers/aads_tools_bridge.py` | Codex/Claude Code에 AADS 도구 노출, 세션 바인딩, heartbeat |
| 프롬프트 컴파일러 | `app/services/prompt_compiler.py` | L1~L5 prompt asset 합성, provenance 기록 |
| PC Agent | `app/api/pc_agent.py`, `app/services/pc_agent_manager.py` | Windows/브라우저/로그인 사이트 자동화와 명령 실행 |
| 모바일 Agent | dashboard `/ops/mobile-agent` | 앱 설치, 페어링, WebView, 음성/상태 관리 |
| 대시보드 Ops | `/ops`, `/ops/servers`, `/ops/recovery`, `/ops/memory`, `/ops/pc-agents` | 운영 관측 화면 |
| 관리자 Ops | `/admin/tasks`, `/admin/agents`, `/admin/deploy`, `/admin/prompts`, `/admin/model-routing` | 작업·모델·프롬프트·배포 관리 |

판정: AADS는 공용화의 중심으로 사용할 수 있다. 단, 현재는 AADS 내부 구현과 프로젝트별 실행 어댑터가 섞여 있어 패키지/계약/API로 분리되지 않았다.

### 2.2 프롬프트/스킬 계층

현재 AADS DB에는 5-Layer prompt asset 구조가 있다.

| Layer | 건수 | 역할 |
|---|---:|---|
| L1 Global | 12 | 전 프로젝트 공통 운영 원칙, 검증, 응답 품질, 보안 |
| L2 Project | 9 | 프로젝트별 도메인 컨텍스트 |
| L3 Role | 95 | CTO, Ops, SRE, PM, QA, 리서치, 프로젝트별 역할 오버레이 |
| L4 Intent | 11 | 상태조회, 코드수정, 보고, 배포 등 작업 유형별 지침 |
| L5 Model | 14 | OpenAI, Anthropic, Gemini, Codex, Kimi 등 모델별 실행 지침 |

Ops/SRE 관련 에셋은 공통 `role-ops-monitor`, `role-sre-reliability`와 프로젝트별 `project-role-{aads,kis,go100,sf,ntv2,nas}-ops`가 활성이다. 즉 "운영 역할 지시"는 이미 6개 프로젝트에 반영되어 있다.

미흡점은 "프롬프트 적용"과 "실제 실행 도구" 사이가 완전히 닫혀 있지 않다는 점이다. 예를 들어 SF/NAS는 prompt asset은 있으나 AADS 중앙 API가 SF/NAS 내부 상태를 표준 스키마로 읽는 어댑터가 부족하다.

### 2.3 프로젝트별 현황

| 프로젝트 | 현재 Ops 구현 | 강점 | 약점 | 공용화 판정 |
|---|---|---|---|---|
| AADS | 중앙 Ops API, pipeline runner, tool registry, prompt compiler, dashboard Ops | 가장 완성도 높음 | AADS 내부 결합도가 높고 공용 SDK가 없음 | Core 추출 대상 |
| KIS | `generate_ops_manual.py`, `ops_manual_20260220.md`, systemd/crontab 중심 운영 | 서버/서비스/DB/자금/포지션/백테스트 자동 문서화 | AADS 공통 원장과 양방향 동기화 약함 | Project adapter 필요 |
| GO100 | `ops_integrity.py`, 단위테스트, 장중/장후 cron, 러너 작업 다수 | git/service/health/runner 정합성 검사가 코드화됨 | frontend inactive 같은 상태가 중앙 화면에 자동 승격되지 않음 | 우선 통합 대상 |
| SF | ShortFlow/StyleFlow 아키텍처 문서, worker/dashboard/n8n/cron | 영상 파이프라인과 스케줄 운영 체계 존재 | `/data/shortflow` 실행 코드 직접 파일 실측 제한, AADS 표준 Ops API 없음 | 문서 기반 adapter 우선 |
| NTV2 | Laravel/Next/MySQL/Redis, `normalize_ops_cron.py`, bridge/disk guard cron | 커머스/소셜/정산/AI Studio 운영 복잡도 높음 | Ops가 cron/스크립트 단위로 흩어짐 | health/runbook adapter 필요 |
| NAS | NAS 이미지 자동화 아키텍처/파이프라인 문서 | 이미지 처리 worker, QC, rsync, SQLite, NAS/114/116 경로 명확 | AADS 직접 원격 MCP 프로젝트로 노출되지 않음 | connector 등록 필요 |

## 3. 프로젝트별 상세 검수

### 3.1 AADS

확인된 구현:

- `/api/v1/ops/*`: streaming metrics, version, locks, directive lifecycle, cost, commits, workspace changes, bridge log, active streams, health check, server health, stalled, auto recover, maintenance, recovery logs, circuit breaker, docs sync, usage stats, tool stats, prompt profile.
- `/api/v1/pipeline/*`: job submit/list/detail/approve/retry, model stats, batch submit, lock status, runner model settings.
- `tool_registry.py`: 도구 135개, eager/deferred loading, intent별 도구 그룹.
- `tool_executor.py`: 로컬/원격 git, DB, SSH, 브라우저, PC agent, media, research, runner, task status, capture screenshot 실행.
- `prompt_compiler.py`: DB prompt asset 합성 및 `compiled_prompt_provenance` 기록.
- dashboard: `/ops/*`, `/admin/*`, `/projects/*`, `/authenticated-collector`.

판정:

AADS는 이미 "공용 Ops Control Plane"으로 설계되어 있다. 다만 패키지 경계가 `app/services/*` 내부 구현 중심이라 다른 프로젝트가 직접 import해서 쓰기 어렵다. 공용화하려면 `aads_ops_sdk`와 `project_adapter` 계약으로 분리해야 한다.

### 3.2 KIS

확인된 구현:

- `scripts/generate_ops_manual.py`: 서버 정보, systemd 6개 서비스, v4 DB 테이블 행 수, OPEN 포지션, 자금, 백테스트, 데이터 현황, crontab, git log를 자동 문서화한다.
- `docs/ops_manual_20260220.md`: 생성된 운영 매뉴얼. 서비스 active, DB row count, crontab, 데이터 범위가 포함된다.
- systemd: `kis-v41-api` active.

판정:

KIS는 "운영 문서 자동 생성"은 강하지만, AADS처럼 작업 원장/러너/검증 결과를 API로 노출하는 구조는 약하다. 공용 SDK를 붙일 때 첫 목표는 KIS 상태를 `ProjectHealthSnapshot`으로 변환하는 것이다.

### 3.3 GO100

확인된 구현:

- `backend/app/services/go100/ops_integrity.py`: git dirty/ahead, `go100`/`go100-frontend` systemd, backend health, runner ledger를 read-only로 점검한다.
- `backend/tests/test_go100_ops_integrity.py`: monkeypatch 기반으로 git dirty, service inactive, backend mismatch, runner unreachable, multiple failure를 테스트한다.
- crontab: 장중 1분/5분 수집, 08:20 target guard, 08:30/장중/16:00 data integrity, 뉴스/리포트/파동/거래정지/토큰 갱신 자동화가 등록되어 있다.
- 현재 서비스: `go100` active, `go100-frontend` inactive.
- AADS DB `pipeline_jobs`: GO100 316건, 현재 running 1건.

판정:

GO100은 AADS 다음으로 Ops 코드화 수준이 높다. 단, "운영 정합성 결과"가 GO100 내부 함수/테스트에 머물고 AADS 대시보드의 공통 프로젝트 상태 카드로 자동 반영되지 않는다. 가장 먼저 공용 adapter를 붙이면 효과가 크다.

### 3.4 SF

확인된 구현:

- ShortFlow/StyleFlow 문서: worker, n8n, FastAPI, dashboard, Redis, Supabase, NAS 동기화, YouTube 업로드, analytics 구조.
- 114/116.120 서버 컨테이너: `shortflow-worker` healthy, `shortflow-saas-dashboard`, `shortflow-dashboard`, `shortflow-n8n` 구동.
- crontab: v4 pipeline 09/13/18시, health pipeline 09:10/13:10/18:10, log rotation, daily report, alert_on_error, convert_batch 등록.

제한:

MCP `read_remote_file`은 중간에 transport closed가 발생했고, `/data/shortflow` 파일 목록은 SSH `find`에서 비어 있는 응답이 나왔다. 컨테이너와 cron은 확인됐으나, 실행 코드 파일 전수는 이번 세션에서 제한적이다.

판정:

SF는 "운영 스케줄과 미디어 파이프라인"은 살아 있지만, AADS 표준 Ops API/원장/스킬 승격과는 느슨하다. `shortflow-worker /health`, n8n schedule, upload job, originality score, YouTube upload result를 표준 snapshot으로 묶어야 한다.

### 3.5 NTV2

확인된 구현:

- 문서: Laravel 12 API, Next.js 16, MySQL 8, Redis 7, NAS image-auto, 외부 서비스 연동 구조.
- API 영역: Auth, Product, Purchase, Dashboard, Social, Marketplace, Brand, Content, V1 Migration, AI Intelligence.
- `scripts/normalize_ops_cron.py`: bridge watchdog와 disk guard 중복 cron 제거 도구.
- crontab: bridge watchdog, disk guard, bandwidth monitor, traffic report, Laravel scheduler, backup, daily check, report_to_aads, 68/211 감시 등이 등록되어 있다.
- 컨테이너: `newtalk-v2-frontend`, `newtalk-v2-app`, `newtalk-v2-queue`, `newtalk-v2-nginx`, `newtalk-v2-db`, `newtalk-v2-redis`, `newtalk-v2-reverb` 구동.

판정:

NTV2는 운영 대상이 많아 Ops 필요성이 크지만, 현재는 cron/스크립트/문서에 분산되어 있다. AADS에서 NTV2의 queue, nginx, Laravel scheduler, disk/bandwidth, bridge watchdog을 한 번에 보는 `NTV2OpsAdapter`가 필요하다.

### 3.6 NAS

확인된 구현:

- 문서: NAS DS1821+, Docker `newtalk-image-auto`, FastAPI 8100, 이미지 처리 worker, SQLite, QC UI, rsync, 114/116 연동.
- 파이프라인: 촬영 원본 -> folder parser -> auto classify -> batch pipeline -> resize -> filename mapper -> rsync -> 114 서버 DB/이미지.
- 구현 상태 문서: 자동보정/크랍은 완료, 폴더 직접 생성/코디 매칭/A컷 선별/배너 생성/DB 자동 업데이트/상세페이지 정렬은 부분 또는 미구현.

제한:

현재 AADS MCP 프로젝트 enum에는 NAS 직접 `read_remote_file/run_remote_command`가 노출되지 않았다. 따라서 이번 검수는 NTV2 서버 내 `project-docs-repo/nas-image` 문서 기반이며, NAS 실제 컨테이너/포트 실측은 미완료다.

판정:

NAS는 이미지 처리 도메인 adapter가 별도로 필요하다. AADS 공통 도구에 `NAS` 원격 실행/파일 읽기/헬스체크 connector를 정식 등록해야 전수 검수가 가능해진다.

## 4. 핵심 문제

| 문제 | 근거 | 영향 |
|---|---|---|
| 공통 프롬프트는 있으나 공통 실행 SDK가 없다 | `prompt_assets`에는 프로젝트별 Ops가 있으나 프로젝트별 adapter 코드가 표준화되지 않음 | 프로젝트마다 같은 운영 점검을 다시 구현 |
| AADS 도구는 중앙에 집중되어 있다 | `tool_registry.py`, `tool_executor.py`가 AADS 앱 내부에 위치 | KIS/GO100/SF/NTV2/NAS가 독립적으로 재사용하기 어려움 |
| 프로젝트별 상태 스키마가 다르다 | GO100은 `ops_integrity`, KIS는 ops manual, SF/NTV2는 cron/docker, NAS는 문서/QC 중심 | `/ops`에서 프로젝트 상태 비교가 어렵다 |
| 스킬이 "프롬프트/지침"과 "코드 실행"으로 분리되어 있지 않다 | DB prompt asset과 local Codex `SKILL.md`가 별도 체계 | 성공한 운영 절차를 자동으로 재사용 가능한 스킬로 승격하기 어렵다 |
| 원격 connector 범위가 불완전하다 | NAS는 MCP 프로젝트 실행 enum에 없음 | NAS 실측 자동화와 화면 검증이 제한된다 |
| tool bridge 안정성 이슈 | 이번 검수 중 `search_all_projects/read_remote_file`가 `Transport closed` 발생 | 전수 분석/운영 조치 중단 위험 |
| 러너 활용 편중 | `pipeline_jobs`: GO100 316, AADS 242, NTV2 91, KIS 9, SF 2, NAS 0 | 프로젝트별 자율 개발/검증 성숙도 차이 |

## 5. 목표 아키텍처

### 5.1 전체 구조

```
CEO / Chat / Dashboard
        |
        v
AADS Ops Control Plane
        |
        +-- Prompt Layer: L1~L5 prompt_assets, role/intent/model policy
        +-- Skill Layer: reusable runbooks, SKILL.md, recipe, verification checklist
        +-- Tool Layer: tool_registry, MCP bridge, ToolExecutor
        +-- Job Layer: pipeline_jobs, task cards, approval, deploy, rollback
        +-- Evidence Layer: tool_results_archive, ai_observations, session_notes, provenance
        |
        v
Project Adapter Interface
        |
        +-- AADSAdapter: docker, blue-green, ops API, dashboard
        +-- KISAdapter: systemd, v4 DB, trading safety, ops manual
        +-- GO100Adapter: ops_integrity, crontab, runner ledger, market calendar
        +-- SFAdapter: worker/n8n/dashboard, media jobs, upload, originality score
        +-- NTV2Adapter: Laravel/Next/MySQL/Redis, scheduler, bridge, commerce ops
        +-- NASAdapter: image-auto FastAPI, QC, rsync, 114/116 DB sync
```

### 5.2 공통 계약

모든 프로젝트 adapter는 아래 6개 메서드를 구현한다.

| 메서드 | 반환 | 목적 |
|---|---|---|
| `collect_health()` | `ProjectHealthSnapshot` | 서비스, 컨테이너, DB, 디스크, 큐 상태 |
| `collect_git_state()` | `GitReleaseSnapshot` | dirty/ahead/behind/head SHA/배포 가능성 |
| `collect_runtime_jobs()` | `RuntimeJobSnapshot` | cron, queue, runner, worker, scheduler 상태 |
| `collect_domain_metrics()` | `DomainMetricSnapshot` | 프로젝트 도메인별 핵심 지표 |
| `run_readonly_diagnostics()` | `DiagnosticReport` | 재시작/수정 없는 read-only 진단 |
| `build_release_plan()` | `ReleasePlan` | 배포 영향, 롤백, 검증, 금지 조건 |

### 5.3 공용 스킬 패키지

| 스킬 | 적용 프로젝트 | 내용 |
|---|---|---|
| `ops-health-audit` | 전체 | health/git/service/db/queue/cron snapshot 표준 수집 |
| `ops-release-gate` | 전체 | dirty/ahead/active runner/DB risk/rollback preflight |
| `ops-incident-triage` | 전체 | 증상 -> 로그 -> 원인 후보 -> 즉시 조치 -> 재발 방지 |
| `ops-runner-recovery` | AADS/GO100/KIS/SF/NTV2 | stale runner, approval hold, process dead, deploy preflight 오류 정리 |
| `ops-data-integrity` | KIS/GO100/NTV2/SF/NAS | 수집 누락, DB row freshness, 파일 sync, 백필 판단 |
| `ops-authenticated-automation` | AADS/NTV2/매장비서/마케팅 | 로그인 사이트 수집, CAPTCHA/OTP 정책, 사용자 승인 재개 |
| `ops-media-pipeline` | SF/NAS/NTV2 | 이미지/영상 queue, QC, upload, CDN sync 검증 |
| `ops-doc-sync` | 전체 | 문서 위치, `/docs` 노출, 보고서 저장/커밋 상태 점검 |

## 6. 구현 로드맵

### Phase 0. 기준선 고정

| 작업 | 기간 | 완료 기준 |
|---|---:|---|
| `ProjectAdapter` 타입 정의 | 0.5일 | 6개 메서드와 공통 snapshot Pydantic model 추가 |
| 프로젝트 connector 표준화 | 0.5일 | AADS/KIS/GO100/SF/NTV2/NAS path, host, db, health URL 설정 DB화 |
| NAS MCP connector 등록 | 0.5일 | `run_remote_command/read_remote_file/list_remote_dir`에서 NAS 선택 가능 |

### Phase 1. Read-only Ops 통합

| 작업 | 기간 | 완료 기준 |
|---|---:|---|
| AADS/GO100 adapter 구현 | 1일 | 기존 AADS ops와 GO100 `ops_integrity`를 공통 snapshot으로 변환 |
| KIS adapter 구현 | 1일 | `generate_ops_manual.py` 핵심 수치를 API JSON으로 변환 |
| SF/NTV2 adapter 구현 | 1~2일 | docker/crontab/health/log snapshot을 표준 응답으로 제공 |
| NAS adapter 구현 | 1일 | NAS image-auto health/QC/rsync 상태 수집 |
| `/api/v1/ops/projects/overview` 추가 | 0.5일 | 6개 프로젝트 상태를 한 응답으로 반환 |

### Phase 2. Skill Library와 Runner 결합

| 작업 | 기간 | 완료 기준 |
|---|---:|---|
| 성공 작업 -> skill 후보 추출 | 1일 | `pipeline_jobs`, `tool_results_archive`, `ai_observations`에서 재사용 절차 후보 생성 |
| `ops_skill_library` 테이블 추가 | 0.5일 | skill slug, scope, trigger, checklist, validation, source 저장 |
| 스킬 실행 전 preflight | 1일 | `ops-release-gate`, `ops-health-audit`가 runner instruction에 자동 삽입 |
| 프로젝트별 runbook 자동 링크 | 0.5일 | `/ops` 화면에서 프로젝트 선택 시 관련 문서와 skill 표시 |

### Phase 3. 대시보드 제품화

| 작업 | 기간 | 완료 기준 |
|---|---:|---|
| `/ops/projects` 화면 추가 | 1~2일 | 6개 프로젝트 상태, 이슈, 러너, 배포 가능성, 문서 링크 표시 |
| 프로젝트 상세 Ops 페이지 | 2일 | 서비스/컨테이너/DB/cron/최근 오류/권장 조치 표시 |
| 승인형 조치 버튼 | 1일 | 재시작/배포/백필/러너종료는 영향·롤백 표시 후 승인 필요 |
| E2E/visual QA | 1일 | 데스크톱/모바일 화면 캡처와 API 폴백 검증 |

### Phase 4. 자동 운영 루프

| 작업 | 기간 | 완료 기준 |
|---|---:|---|
| 프로젝트별 watch loop | 2일 | P0/P1 이상 탐지 시 세션/텔레그램 보고 |
| 상태 기반 runner 자동 제안 | 1일 | 직접 실행이 아니라 지시서 초안/승인 대기 생성 |
| 회귀 테스트 replay | 2일 | 과거 장애 패턴을 adapter snapshot으로 재현 검증 |

## 7. 우선순위 개선안

| 우선순위 | 개선안 | 기대효과 | 검증 기준 |
|---|---|---|---|
| P0 | NAS connector를 AADS 도구 enum에 정식 추가 | NAS 실측 검수 가능 | NAS `read_remote_file/run_remote_command` 성공 |
| P0 | `ProjectAdapter` 공통 스키마 구현 | 프로젝트별 Ops 결과 비교 가능 | 6개 프로젝트 snapshot JSON 생성 |
| P0 | `/ops/projects/overview` API 추가 | CEO가 한 번에 전체 상태 확인 | API 200, 프로젝트 6개 반환 |
| P1 | GO100 `ops_integrity`를 AADS adapter로 연결 | 가장 많은 runner/운영 리스크를 중앙화 | GO100 frontend inactive가 AADS 화면에 이슈로 표시 |
| P1 | KIS ops manual을 JSON 진단으로 전환 | 문서 생성 전에도 운영 상태 조회 | DB/서비스/포지션/자금 read-only snapshot |
| P1 | SF/NTV2 cron/docker 상태 표준화 | 영상/커머스 운영 작업 누락 감지 | cron target 존재, 컨테이너 health, 최근 로그 표시 |
| P1 | Skill Library DB와 `SKILL.md` export | 성공 절차 재사용 | 신규 skill 8종 등록, runner instruction 자동 삽입 |
| P2 | Ops 화면에서 승인형 조치 흐름 | 위험 조치 통제 강화 | 조치 전 영향/롤백/승인 로그 저장 |
| P2 | MCP bridge transport watchdog | 도구 transport closed 재발 감소 | bridge disconnect 후 자동 복구와 알림 |

## 8. 데이터 모델 제안

```sql
CREATE TABLE project_ops_connectors (
  project_key text PRIMARY KEY,
  display_name text NOT NULL,
  server_key text NOT NULL,
  repo_path text,
  dashboard_url text,
  health_url text,
  adapter_type text NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE project_ops_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_key text NOT NULL,
  status text NOT NULL,
  health jsonb NOT NULL,
  git_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  runtime_jobs jsonb NOT NULL DEFAULT '{}'::jsonb,
  domain_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  issues jsonb NOT NULL DEFAULT '[]'::jsonb,
  collected_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ops_skill_library (
  slug text PRIMARY KEY,
  title text NOT NULL,
  scope_projects text[] NOT NULL DEFAULT ARRAY['*'],
  trigger_intents text[] NOT NULL DEFAULT ARRAY['status_check'],
  checklist jsonb NOT NULL,
  validation jsonb NOT NULL,
  source_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  enabled boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

## 9. API 제안

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/ops/projects/overview` | 6개 프로젝트 최신 snapshot 요약 |
| GET | `/api/v1/ops/projects/{project}/snapshot` | 프로젝트별 상세 snapshot |
| POST | `/api/v1/ops/projects/{project}/diagnostics` | read-only 진단 실행 |
| GET | `/api/v1/ops/skills` | 공용 Ops skill 목록 |
| POST | `/api/v1/ops/skills/{slug}/preview` | 스킬 적용 시 runner 지시서/검증 체크리스트 미리보기 |
| POST | `/api/v1/ops/actions/request` | 재시작/배포/백필 같은 승인형 조치 요청 생성 |

## 10. 화면 제안

| 화면 | 핵심 UI |
|---|---|
| `/ops/projects` | 프로젝트 카드 6개, 상태, 최근 runner, critical issue, 배포 가능 여부 |
| `/ops/projects/{project}` | 서비스, 컨테이너, DB, cron, queue, runner, git, 문서 링크 |
| `/ops/skills` | 공용 skill, 적용 프로젝트, trigger, 최근 성공률, 검증 기준 |
| Chat Ops Dock | 현재 active project snapshot, dirty/running/approval/health 경고 |

## 11. 작업 지시서 초안

>>>DIRECTIVE_START
TASK_ID: AADS-CROSS-PROJECT-OPS-SDK-001
TITLE: 6개 프로젝트 공용 Ops SDK 및 프로젝트별 Adapter 기반 구축
PRIORITY: P1-HIGH
SIZE: L
DESCRIPTION:
1. AADS에 `ProjectAdapter` 공통 인터페이스와 `ProjectHealthSnapshot`, `GitReleaseSnapshot`, `RuntimeJobSnapshot`, `DomainMetricSnapshot`, `DiagnosticReport`, `ReleasePlan` 모델을 추가한다.
2. AADS/GO100/KIS/SF/NTV2/NAS adapter를 read-only 우선으로 구현한다. 각 adapter는 서비스/컨테이너/DB/cron/runner/git/문서 상태를 표준 JSON으로 반환해야 한다.
3. NAS를 AADS tool connector 프로젝트 enum에 추가해 `read_remote_file`, `list_remote_dir`, `run_remote_command`, `health_check`에서 선택 가능하게 한다.
4. `/api/v1/ops/projects/overview`, `/api/v1/ops/projects/{project}/snapshot`, `/api/v1/ops/projects/{project}/diagnostics` API를 추가한다.
5. `/ops/projects` 및 프로젝트 상세 화면을 추가한다. 6개 프로젝트 상태, 이슈, 최근 runner, 배포 가능 여부, 관련 runbook 링크를 표시한다.
6. 위험 조치(배포, 재시작, 백필, 프로세스 종료)는 실행하지 말고 영향 범위/롤백/승인 요청만 생성한다.
7. 검증은 unit test, adapter mock test, API route import, DB read-only query, dashboard build, `/ops/projects` 화면 캡처, blue-green 배포 전 preflight까지 수행한다.
>>>DIRECTIVE_END

>>>DIRECTIVE_START
TASK_ID: AADS-OPS-SKILL-LIBRARY-001
TITLE: 공용 Ops Skill Library와 성공 절차 재사용 파이프라인 구축
PRIORITY: P1-HIGH
SIZE: M
DESCRIPTION:
1. `ops_skill_library` 테이블과 seed skill 8종(`ops-health-audit`, `ops-release-gate`, `ops-incident-triage`, `ops-runner-recovery`, `ops-data-integrity`, `ops-authenticated-automation`, `ops-media-pipeline`, `ops-doc-sync`)을 추가한다.
2. `pipeline_jobs`, `tool_results_archive`, `ai_observations`, `session_notes`에서 성공 절차 후보를 추출해 skill 후보로 저장하는 read-only 추천기를 만든다.
3. Runner instruction 생성 시 active project와 intent에 맞는 Ops skill checklist를 자동 삽입한다.
4. `/ops/skills` 화면에서 skill 적용 프로젝트, trigger, 검증 기준, 최근 사용/성공 이력을 표시한다.
5. 검증은 seed row count, skill preview API, runner instruction snapshot test, dashboard 화면 렌더링으로 수행한다.
>>>DIRECTIVE_END

## 12. 검증 계획

| 검증 | 명령/도구 | 성공 기준 |
|---|---|---|
| DB migration | `pytest tests/unit` 또는 migration dry-run | 새 테이블 생성, 기존 테이블 영향 없음 |
| adapter unit | 프로젝트별 mock test | 6개 adapter 모두 snapshot schema 통과 |
| API import | FastAPI route dump | 신규 route 3개 이상 등록 |
| dashboard build | `npm run build` | `/ops/projects`, `/ops/skills` 빌드 성공 |
| remote read-only | SSH/docker/systemctl/crontab | 재시작/수정 없이 상태 수집 |
| 브라우저 화면 | Playwright/capture screenshot | `/ops/projects` 프로젝트 6개 표시 |
| 배포 전 | blue-green preflight | dirty/ahead/active runner/rollback plan 확인 |

## 13. 남은 리스크

| 리스크 | 대응 |
|---|---|
| MCP bridge transport closed 재발 | bridge heartbeat/watchdog와 tool timeout 분리 |
| NAS 실제 접근 정보 부족 | NAS connector 등록 후 1차 read-only health만 수행 |
| 프로젝트별 명령 권한 차이 | adapter별 allowlist와 dry-run 모드 적용 |
| 금융 프로젝트 GO100/KIS 오작동 위험 | 장중 직접 조치 금지, read-only snapshot 기본값 |
| NTV2/SF cron 중복/노후화 | cron canonical file 생성 후 drift detection |
| 기존 dirty worktree 다수 | Runner 또는 isolated worktree로 구현, 기존 변경 미포함 |

## 14. 최종 판정

전 프로젝트 공용화는 가능하다. 다만 "프롬프트 공통화"만으로는 부족하고, `ProjectAdapter + Ops Skill Library + Ops Dashboard + 승인형 Action` 4계층으로 나눠야 한다.

가장 빠른 실행 순서는 다음이다.

1. NAS connector와 `ProjectAdapter` 기본 스키마를 먼저 만든다.
2. AADS/GO100/KIS adapter를 1차 연결한다.
3. SF/NTV2/NAS는 read-only health와 cron/docker snapshot부터 붙인다.
4. 성공 절차를 `ops_skill_library`로 승격해 Runner 지시서에 자동 삽입한다.
5. `/ops/projects` 화면에서 CEO가 전체 프로젝트 상태와 필요한 조치를 한 번에 판단하게 만든다.
