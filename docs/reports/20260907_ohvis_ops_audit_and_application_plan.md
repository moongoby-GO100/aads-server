# OHVIS Ops 반영 전수 검수 및 활용 기획 보고서

- 작성 시각: 2026-09-07 08:56 KST
- 대상: AADS/OHVIS, Ops 역할, 운영 API, 대시보드, 작업/루프 원장
- 검수 범위: 코드, DB, 운영 컨테이너, 공개 URL 인증 게이트, 기존 문서
- 결론: Ops는 OHVIS/AADS에 이미 반영되어 있으나, "역할/운영 API/화면" 중심의 부분 반영이다. OHVIS의 작업 루프와 Ops의 자동 운영 통제가 완전히 닫힌 구조는 아니다.

## 1. 실측 요약

| 항목 | 결과 | 출처 |
|---|---:|---|
| Ops L3 prompt asset | 7건 활성 | DB `prompt_assets` |
| Ops role profile | `role=Ops`, `project_scope={AADS,KIS,GO100,SF,NTV2,NAS}`, `budget_usd=90.00` | DB `role_profiles` |
| Ops 세션 수 | 59건 | DB `chat_sessions` |
| compiled prompt 내 `role-ops-monitor` 적용 이력 | 47건 | DB `compiled_prompt_provenance` |
| compiled prompt 내 `project-role-aads-ops` 적용 이력 | 0건 | DB `compiled_prompt_provenance` |
| OHVIS task 원장 | 총 262건, done 241건, error 1건, running 17건, stale_cleaned 3건 | DB `ohvis_tasks` |
| OHVIS loop 원장 | 총 18건, active 4건, paused 2건, completed 7건, cancelled 5건 | DB `ohvis_loops` |
| API route 등록 | `/ops`, `/ohvis/tasks`, `/loops` 포함 80개 route | 컨테이너 `aads-server` app route dump |
| 공개 `/ops` 접근 | HTTP 307, `/login?redirect=%2Fops` | `curl https://aads.newtalk.kr/ops` |
| 비인증 OHVIS queue API 접근 | HTTP 401 | `curl https://aads.newtalk.kr/api/v1/ohvis/tasks/queue` |

## 2. 반영된 구조

### 2.1 역할/프롬프트

`migrations/073_refine_ops_developer_qa_judge_roles.sql`에서 Ops를 "배포·운영엔지니어"로 정의하고, SRE와 책임을 분리했다.

Ops의 책임은 다음으로 정의되어 있다.

| 책임 | 반영 상태 | 근거 |
|---|---|---|
| 릴리즈 실행 | 반영됨 | `role-ops-monitor`, `project-role-aads-ops` |
| runbook 준수 | 반영됨 | `role-ops-monitor` |
| 작업 잠금/배포 전후 확인 | 반영됨 | `/api/v1/ops/locks/*`, `/api/v1/ops/active-work/{project}` |
| 롤백 준비 | 반영됨 | `requires_rollback_plan=true` |
| 운영 보고 품질 | 반영됨 | `requires_verification_before_done=true` |
| 6개 프로젝트별 오버레이 | 반영됨 | AADS/KIS/GO100/SF/NTV2/NAS prompt asset 6건 |

### 2.2 API/운영 도구

`app/api/ops.py`는 운영 관측/조치 API를 갖고 있다.

| 영역 | 대표 API | 활용 |
|---|---|---|
| 배포/버전 | `/api/v1/ops/version`, `/api/v1/ops/deploy/status` | 현재 release SHA, 배포 큐, standby sync 확인 |
| 잠금/충돌 | `/api/v1/ops/locks/*`, `/api/v1/ops/active-work/{project}` | 동시 수정/배포 충돌 방지 |
| 러너/파이프라인 | `/api/v1/ops/pipeline-status`, `/api/v1/ops/directive-lifecycle` | 지시서 생명주기와 러너 상태 확인 |
| 헬스/인프라 | `/api/v1/ops/health-check`, `/api/v1/ops/full-health`, `/api/v1/ops/server-health/{server_id}` | 서버/컨테이너/서비스 상태 확인 |
| 복구 | `/api/v1/ops/stalled`, `/api/v1/ops/auto-recover`, `/api/v1/ops/recovery-logs` | 스톨 감지, 자동 복구, 회로 차단기 |
| 비용/사용량 | `/api/v1/ops/cost/summary`, `/api/v1/ops/codex-usage`, `/api/v1/ops/account-usage` | 비용 및 모델 사용량 감시 |
| 작업 변경 원장 | `/api/v1/ops/workspace-changes`, `/api/v1/ops/workspace-changes/finalize` | 채팅 세션 변경 파일 commit/finalize 보조 |

### 2.3 OHVIS 작업/루프

OHVIS는 작업 카드와 루프 실행 구조를 갖고 있다.

| 구성 | 반영 상태 | 근거 |
|---|---|---|
| 작업 원장 | 반영됨 | `ohvis_tasks` 테이블, `app/api/ohvis_tasks.py` |
| 작업 카드 | 반영됨 | `chat_artifacts(type='task_card')` upsert |
| 작업 SSE | 반영됨 | Redis pub/sub `ohvis:task:{session_id}` |
| 루프 API | 반영됨 | `app/api/loops.py` |
| 루프 DB | 반영됨 | `ohvis_loops`, `ohvis_loop_iterations`, `ohvis_loop_definitions` |
| 스톨 정리 | 부분 반영 | `mark_stale_running_tasks(stale_hours=24)` |

### 2.4 대시보드

대시보드는 Ops 화면을 여러 개 갖고 있다.

| 화면 | 경로 | 기능 |
|---|---|---|
| 운영 현황 | `/ops` | pipeline health, cost, bridge log, QA, deploy 상태 |
| 서버 상태 | `/ops/servers` | 3개 서버 헬스, 메모리/디스크/load, PowerShell 접속 |
| 복구 | `/ops/recovery` | recovery logs, circuit breaker |
| 메모리 | `/ops/memory` | memory stats, entries, deduplicate |
| PC Agent | `/ops/pc-agents` | PC Agent 운영 |
| Mobile Agent | `/ops/mobile-agent` | 오비스 앱 설치, 페어링, WebView, voice wake |
| Chat Ops Dock | 채팅 하단 | workspace dirty/commit/finalize 상태 |

## 3. 미흡한 점

| 문제 | 현재 근거 | 영향 |
|---|---|---|
| AADS 프로젝트 Ops 오버레이 적용 이력 0건 | `compiled_prompt_provenance`에서 `project-role-aads-ops=0` | AADS Ops 세션에서 프로젝트 특화 지시가 누락될 수 있음 |
| OHVIS task `running` 잔존 17건 | DB `ohvis_tasks` | 실제 죽은 runner가 작업 중으로 보일 수 있음 |
| OHVIS loop active 4건 장기 잔존 | DB `ohvis_loops` | 반복 작업이 실제 실행 중인지 상태 혼선 |
| 스톨 정리 기준이 24시간 | `mark_stale_running_tasks(stale_hours=24)` | 1~2시간 내 실패/취소 runner가 UI에 running으로 남음 |
| Ops 화면과 OHVIS task/loop 화면 분리 | `/ops`, `/admin/loops`, ChatOpsDock가 독립 동작 | CEO가 한 화면에서 원인, 조치, 검증, 롤백을 보기 어려움 |
| Ops Agent가 구형 LLM 배포 JSON 생성 수준 | `app/agents/devops_agent.py` | 실제 AADS blue-green 계약과 직접 연결이 약함 |
| 문서의 OHVIS release-control은 설계서 중심 | `docs/plans/20260904_OHVIS_GLOBAL_RELEASE_CONTROL_APPLICATION_PLAN.md` | 구현 상태와 설계 상태가 화면에서 구분되지 않음 |

## 4. 판정

Ops는 "반영됨"으로 판정한다. 근거는 DB role profile, prompt asset, API route, 대시보드 화면, OHVIS task/loop DB가 모두 존재하기 때문이다.

다만 완성도는 "부분 반영"이다. 현재 Ops는 운영 관측과 배포 안전장치로는 강하지만, OHVIS의 목표 달성 루프, runner 오류, task card, release certification을 하나의 자동 운영 콘솔로 묶는 단계는 아직 부족하다.

## 5. 활용 방식

### 5.1 CEO가 바로 쓰는 방식

| 사용 목적 | 지시 예시 | 내부 활용 경로 |
|---|---|---|
| 배포 확인 | "Ops로 AADS 배포 상태 확인하고 SHA/슬롯/헬스 보고해" | `/ops/deploy/status`, docker, health |
| 러너 정리 | "Ops로 running 잔존 러너와 OHVIS task 불일치 정리해" | `pipeline_jobs`, `ohvis_tasks`, stale cleanup |
| 장애 복구 | "Ops로 스톨 감지 후 자동복구 가능/불가 보고해" | `/ops/stalled`, `/ops/auto-recover`, recovery logs |
| 프로젝트별 운영 | "GO100 Ops 기준으로 장중 배포 가능 여부 판단해" | project-role-go100-ops, KIS/GO100 runbook |
| 비용 감시 | "Ops로 오늘 모델 비용과 Codex 사용량 보고해" | `/ops/cost/summary`, `/ops/codex-usage` |
| 작업 완료 검수 | "Ops로 커밋/푸시/배포/문서기록 완료 여부 검수해" | workspace changes, git, deploy status |

### 5.2 시스템이 자동 활용하는 방식

1. `role_key=Ops` 세션이면 L3 `role-ops-monitor`를 붙인다.
2. workspace/project가 AADS/KIS/GO100/SF/NTV2/NAS 중 하나면 프로젝트별 Ops 오버레이를 붙인다.
3. 배포/상태/장애/러너/헬스 요청은 Ops API를 우선 호출한다.
4. 복잡한 작업은 `ohvis_tasks`에 task card를 만들고 Redis SSE로 상태를 갱신한다.
5. 완료 보고 전 `health/API/git/DB/UI` 검증 근거를 요구한다.

## 6. 보강 기획

| 우선순위 | 보강안 | 기대효과 | 완료 기준 |
|---|---|---|---|
| P0 | `compiled_prompt_provenance` 기준 AADS Ops 오버레이 미적용 원인 수정 | AADS Ops 세션에서도 프로젝트 특화 운영 지침 적용 | 신규 AADS Ops 세션 provenance에 `project-role-aads-ops` 확인 |
| P0 | runner 상태와 `ohvis_tasks` 상태 동기화 워커 추가 | error/cancelled runner가 running task로 남는 문제 감소 | runner terminal 상태가 5분 내 task done/error/stale로 반영 |
| P0 | OHVIS active loop 감사 API 추가 | 오래된 active loop를 실제 실행/중단/고아 상태로 구분 | `/api/v1/ohvis/ops/audit`에서 orphan/active/paused 판정 |
| P1 | `/ops`에 OHVIS task/loop 섹션 추가 | CEO가 운영 현황과 OHVIS 자율작업을 한 화면에서 확인 | `/ops`에서 active tasks, active loops, stale candidates 표시 |
| P1 | Ops Agent를 blue-green release contract 실행 계획 생성기로 교체 | 구형 배포 JSON이 실제 deploy 규칙과 어긋나는 문제 해결 | DevOps Agent 결과에 clean SHA, no-build, same digest, rollback, 5분 monitoring 포함 |
| P1 | Chat Ops Dock에 OHVIS task queue 추가 | 채팅창에서 작업 상태, pending, stale, unreported 확인 | 채팅 하단에서 session별 OHVIS task active/unreported 확인 |
| P2 | 프로젝트별 Ops runbook 문서 자동 링크 | AADS/KIS/GO100/SF/NTV2/NAS별 운영 기준 접근성 향상 | `/ops`에서 project 선택 시 runbook/docs 링크 표시 |

## 7. 구현 지시서 초안

>>>DIRECTIVE_START
TASK_ID: AADS-OHVIS-OPS-INTEGRATION-001
TITLE: OHVIS와 Ops 운영 통제 통합 및 task/loop 상태 불일치 보강
PRIORITY: P1-HIGH
SIZE: M
DESCRIPTION:
1. AADS Ops 세션에서 `project-role-aads-ops`가 compiled_prompt_provenance에 붙지 않는 원인을 분석하고 수정한다.
2. `pipeline_jobs` terminal 상태와 `ohvis_tasks` running 상태를 5분 내 동기화하는 안전 워커 또는 API 보강을 구현한다. 실제 실행 중인 작업은 건드리지 않고, runner error/cancelled/done이 확인된 task만 terminal 처리한다.
3. OHVIS loop/task 운영 감사 API를 추가해 active loop, running task, stale candidate, unreported task를 한 응답으로 반환한다.
4. `/ops` 대시보드에 OHVIS 운영 섹션을 추가한다. 표시 항목은 active task, stale candidate, active loop, paused loop, unreported terminal task, 최근 runner 연결 상태다.
5. ChatOpsDock에 현재 세션 OHVIS task queue 요약을 추가한다.
6. 검증: unit test, route import, DB read-only audit query, `/ops` 인증 게이트, blue-green 배포 후 5분 P0/P1 모니터링을 수행한다.
>>>DIRECTIVE_END

## 8. 완료 기준

| 기준 | 성공 조건 |
|---|---|
| 프롬프트 적용 | 신규 AADS Ops 세션 provenance에 `role-ops-monitor`와 `project-role-aads-ops` 동시 포함 |
| task 동기화 | runner terminal 상태와 `ohvis_tasks.status` 불일치 0건 |
| loop 감사 | active loop가 실제 next_run/worker 상태와 함께 표시 |
| 화면 | `/ops`에서 OHVIS 운영 섹션 렌더링 확인 |
| 배포 | API/Dashboard blue-green, same digest, external health, 5분 P0/P1 모니터링 통과 |

