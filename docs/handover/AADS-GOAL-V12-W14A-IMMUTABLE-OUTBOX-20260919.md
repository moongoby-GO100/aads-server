# AADS W-14a immutable change-set / approval / outbox handover

## STEP 0 기존 구현 조사

| 접점 | 분류 | 처리 |
|---|---|---|
| `create_change_set` / RFC6902 patch 및 idempotency | 수정 | W-14F validator/JCS patch hash 재사용, 전체 semantic body hash와 target version 추가 |
| `route_change_set`, `decide_change_set` | 수정 | legacy request 호환 필드는 유지하고 독립 route/append-only decision을 추가; reject wins 및 self-approval 금지 유지 |
| `execute_change_set` | 수정 | 저장된 W-14F decision/signature/fence를 실행 직전 재검증하고 business mutation/effect/outbox를 한 transaction에 결합 |
| `goal_workflow_outbox` | 수정 | claim에만 `SKIP LOCKED` 사용, owner instance/epoch acknowledgement fence 및 reconciliation 상태 추가 |
| `claim_outbox`, `complete_outbox_delivery` | 신규 | at-least-once delivery claim과 fenced 완료/unknown outcome 처리 |
| `goal_workflow_effects` | 신규 | execution key당 내부 effect를 한 번만 기록 |
| completion acceptance (`review_item`) | 유지 | execution과 별도 수동 acceptance 경계 유지 |
| grant/review/rollup API | 유지 | W-14a 외 동작을 대체하지 않음 |
| 삭제 | 없음 | 기존 호출처와 rollback 호환을 위해 제거한 API/테이블 없음 |

지시서에 직접 열거되지 않은 migration/test/handover 변경은 새 DB 불변식, T06-T14
회귀 계약, 인수 증거를 각각 남기기 위해 필요했다.

## 구현 증거

- 동일 tenant/idempotency key는 target과 patch뿐 아니라 rationale, expected effect,
  rollback plan, action, environment, version을 포함한 semantic body hash가 같아야 재사용된다.
- A3 route는 CEO와 independent reviewer가 서로 다른 actor로 각각 승인해야 한다.
  어느 route든 거절하면 aggregate change-set은 즉시 rejected가 된다.
- 실행은 저장된 W-14F decision을 로드하고 canonicalization/hash/signature, target/policy,
  precondition, kill switch, assignment, grant 및 revocation epoch fence를 재검증한다.
- 내부 work-item mutation, execution-key effect 원장, change-set executed 전환과 outbox
  insert는 동일 caller transaction에서 수행된다. acceptance는 기존 review API에 남는다.
- outbox claim만 `FOR UPDATE SKIP LOCKED`를 사용한다. owner/epoch가 달라진 worker는
  delivery 결과를 기록할 수 없다.
- 외부 결과 `unknown`은 effect와 outbox를 `reconciliation_required`로 전환하며 자동
  refund 또는 blind replay 경로가 없다.

## 검증 결과

- `python -m py_compile app/services/goal_workflow_approval.py app/services/goal_policy_foundation.py`: PASS
- `ruff check` (변경 Python 4개): PASS
- W-14a/W-14F 집중 단위시험: `69 passed`
- 전체 goal 단위 회귀: `346 passed`
- 일회용 PostgreSQL 통합시험: `1 passed`; migration 2회, 복수 승인,
  transaction rollback, execution-key 단일효과를 검증했다.
- AAG baseline: 결함 증가 없음
- `git diff --check`: PASS
- 운영 migration/build/deploy: 미실행

## 남은 검증

- 운영 적용 전 migration rehearsal과 blue/green release gate 수행
- migration 적용 순서: W-14F stores 이후 `20260919_goal_workflow_w14a.sql`
