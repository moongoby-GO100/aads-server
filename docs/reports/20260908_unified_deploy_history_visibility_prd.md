# 통합 배포 이력·상태 카드 PRD v1.0

- Task: `AADS-UNIFIED-DEPLOY-HISTORY-CARD-P0-20260908`
- 작성 시각: 2026-09-08 19:02 KST
- 대상: AADS, FOOD(열정국밥 매장비서), GO100, KIS, SF, NTV2, NAS
- 기존 상위 문서: `docs/reports/20260908_unified_deployment_management_prd.md`

## 1. 목표

CEO가 채팅의 배포 카드 하나에서 각 프로젝트의 대기·진행·지연·완료·실패 여부와 실제 반영된 릴리스를 배포 ID로 추적한다. Pipeline Runner 작업 완료와 운영 배포 완료를 분리해 표시하고, 증거가 없는 과거 작업은 배포로 소급 확정하지 않는다.

## 2. 현재 실측과 문제

| 항목 | 실측 | 판정 |
|---|---:|---|
| `deploy_runs` | 196건, AADS 100% | 타 프로젝트 중앙 배포 원장 미수집 |
| `pipeline_jobs.deployed_at` | AADS/GO100/KIS/SF/NTV2 모두 0건 | runner 이력만으로 실제 배포를 증명할 수 없음 |
| 배포 #196 | `failed / build_candidate_image` | 기존 성공 목록에서 보이지 않아 상태 추적 어려움 |
| 프로젝트 개요 | runner 최신 상태 병합 가능 | 작업 상태이며 배포 완료 증거는 아님 |

## 3. 사용자 흐름

1. 첫 화면: `현재 배포 #ID · 한글 상태`, phase, 경과시간, 대기건을 본다.
2. 프로젝트 선택: 7개 프로젝트 카드에서 최신 상태와 원장 존재 여부를 확인한다.
3. 이력 확인: 최근 배포 이력에서 성공·실패를 함께 보고 배포 ID, SHA, 시작/종료, 소요시간, 변경 요약을 확인한다.
4. 실패 복구: 실패/지연 행에서 phase와 오류 요약을 보고 재조정 또는 재시도 API로 이동한다.

## 4. 설계와 기술 스택

| 계층 | 기술 | 책임 |
|---|---|---|
| UI | Next.js 16, React, TypeScript | 상태 카드, 프로젝트 필터, ID·상태 배지 |
| API | FastAPI 0.115, asyncpg | `/api/v1/ops/deploy/status` 집계·필터 |
| 원장 | PostgreSQL 15 | `deploy_runs`, `deploy_components`, `deploy_phase_events` |
| 실행 | 중앙 Coordinator + 프로젝트 Adapter | 동일 `deploy_run_id`의 상태 전이·heartbeat |
| 관제 | health check, blue/green digest gate | 후보/라우팅/standby/rollback 증거 기록 |

상태 모델은 `queued → running → verifying → syncing_standby → success`를 정상 흐름으로 사용한다. `stalled`는 heartbeat가 300초 이상 없을 때 계산되는 표시 상태이며 원본 상태를 덮어쓰지 않는다. terminal 상태는 `success/failed/blocked/cancelled/superseded`로 구분한다.

## 5. 통합 수집 계약

모든 프로젝트 배포는 실행 전에 중앙 `deploy_runs`를 1건 생성하고 반환된 ID를 끝까지 사용한다. Adapter는 phase 시작/종료, heartbeat, release SHA, target environment, component, 검증 결과, error summary, rollback run ID를 같은 원장에 기록한다.

- FOOD → `store-assistant` 컨테이너 교체
- NTV2 → PHP/app 배포 및 optimize/reload
- SF → API/worker/frontend 배포
- NAS → cafe24_114 경유 rsync-over-SSH 배포
- GO100/KIS → backend/frontend/worker 단위 배포
- DB/config/prompt → 별도 component와 승인·rollback metadata 기록

`pipeline_jobs`는 작업 원장으로 유지한다. `runner_job_id`를 `deploy_runs`에 연결하되 `deployed_at` 또는 성공한 `deploy_runs`가 없으면 UI에 “작업 완료”로만 표시하고 “배포 완료”로 표시하지 않는다.

## 6. API/UI 계약

- `recent_deployments`: 성공과 실패를 포함한 최근 terminal 배포 20건
- `recent_completed_deployments`: 실제 성공 반영 목록, 하위 호환 유지
- `project_deployments`: 7개 프로젝트 최신 상태, `id/source/has_deploy_run/has_pipeline_job` 포함
- 카드 표시: `배포 #196 · 실패`, `phase=build_candidate_image`, 시작/종료/소요시간
- 데이터 없음: “이력 없음”과 “수집 미연결”을 구분하고 0건을 정상 성공처럼 보이지 않게 한다.

## 7. 구현 단계

| 우선순위 | 범위 | 완료 기준 |
|---|---|---|
| P0 | 배포 ID·한글 상태·실패 포함 최근 이력 | #196 실패가 카드에서 재현됨 |
| P0 | 7개 프로젝트 고정 카드 | 모든 프로젝트가 이력 없음 포함 노출됨 |
| P0 | 원격 Adapter의 중앙 run 생성/상태 전이 | 프로젝트별 실제 dry-run 뒤 run ID 생성 |
| P1 | 기존 배포 로그 검증형 backfill | provenance가 있는 성공 건만 import |
| P1 | 프로젝트/기간/상태 필터와 상세 drawer | 30/90일 검색 및 run 상세 확인 |
| P2 | SLO·실패율·평균 배포시간 | 프로젝트별 표본수와 출처 함께 표시 |

## 8. 검증·완료 기준

1. 단위 테스트에서 실패 run ID와 terminal 상태가 API에 포함된다.
2. Dashboard build와 대상 lint가 통과한다.
3. 운영 API에서 #196/#197의 실제 상태가 UI와 일치한다.
4. 브라우저 캡처에서 7개 프로젝트, 배포 ID, 한글 상태가 확인된다.
5. 원격 프로젝트마다 최소 1건의 검증 배포가 `deploy_runs`에 생성되고 phase event·health·release SHA가 남는다.
6. 타 프로젝트 실제 이력이 없는 동안에는 AADS만 성공 반영 목록에 보이는 것을 정상 제한으로 표시한다.

## 9. 롤백

API 신규 필드는 additive이므로 문제 시 Dashboard가 기존 `recent_completed_deployments`로 폴백한다. UI 변경은 단일 카드 파일 커밋을 revert하고 Dashboard 직전 슬롯으로 라우팅한다. 원장 수집기는 프로젝트별 feature flag로 끄며 기존 `deploy_runs` 행은 감사 증거로 보존한다.
