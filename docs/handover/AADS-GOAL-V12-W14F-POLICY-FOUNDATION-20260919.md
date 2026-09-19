# AADS-GOAL-V12-W14F-POLICY-FOUNDATION-20260919

## STEP 0 기존 구현 조사

| 접점 | 분류 | 처리 |
|---|---|---|
| `compute_preconditions`, `create_policy_input`, `require_current_preconditions` | 유지 | W-13 서버 계산 snapshot과 실행 직전 stale guard를 정본으로 유지 |
| `goal_policy_decisions` append-only/RLS 원장 | 수정 | 서명 key id/version 및 ancestor revocation epoch를 additive migration으로 보강 |
| `goal_kill_switches`, `project_role_assignments`, `goal_auto_approval_grants` | 수정 | executor가 비교할 assignment/grant revocation epoch를 additive하게 추가 |
| 기존 work-item/router/API | 유지 | W-14F가 기존 endpoint를 대체하지 않음 |
| RFC 6902/JCS/hash 유틸리티 | 신규 | op 순서 보존, 객체 키 정렬, duplicate/비정상 수 거절, SHA-256 |
| 4축 evaluator와 동기 ledger append | 신규 | diagnostics/entity 오류 및 A3에서 AUTO 폐기, ledger 실패 예외 전파 |
| HMAC envelope와 executor verifier | 신규 | key id/version, input/precondition/signature 및 mutable epoch 즉시 재검증 |
| 단일 grant reservation/budget overrun guard | 신규 | grant 합성·`SKIP LOCKED` 없이 조건부 원자 차감, overrun stale |
| W-14F 집중 unit 및 migration integration | 신규/수정 | T36, T38, T45, T46, T48~T58과 migration 반복 적용 근거 |
| 삭제 | 없음 | 기존 호출 계약과 rollback 대상 삭제 없음 |

지시서에 직접 파일명이 열거되지 않은 service/migration/test/handover를 추가한 이유는
W-14F의 evaluator/executor 신뢰 경계, DB fence, 독립 검증 증거를 각각 분리해 구현하기
위해서다.

## 구현 결과

- 결정 envelope는 `boundary_decision`, `approval_route`,
  `automation_eligibility`, `risk_tier`와 파생 `result`를 함께 기록한다.
- RFC 6902 배열 순서는 hash에 반영하고 객체 property 순서는 JCS 정렬한다.
  duplicate property, NaN/Infinity, malformed JSON number와 미지원 canonicalization
  version은 `422` 계약 오류다.
- Cedar diagnostics 또는 entity resolution 오류에서 AUTO를 반환하지 않는다. A3 및
  mandatory-human action은 grant가 있어도 수동 승인으로 고정한다.
- ledger INSERT가 완료되기 전 envelope를 반환하지 않으며 실패는 호출 transaction에
  전파한다.
- HMAC-SHA-256 서명 범위에 identity, input/patch/precondition hash, target version,
  policy version, mutable epoch, effective result, 시각과 key id/version을 포함한다.
  executor는 DB 함수로 현재 target/policy/kill/deny/assignment/grant/ancestor/
  precondition fence를 다시 읽고 서명된 값과 비교한다.
- patch 원문은 decision ledger diagnostics에 남기지 않는다. 민감 키 값은 지우고
  JSON Pointer만 `erased`에 남긴다.
- 단일 grant만 조건부 `UPDATE ... RETURNING`으로 예약하며 grant row에는
  `SKIP LOCKED`를 사용하지 않는다.

## 독립 검증 상태

- 집중 테스트 파일에 T36, T38, T45, T46, T48~T58 케이스를 추가했다.
- 기존 W-13 T56/T57 및 precondition 회귀 파일은 삭제·대체하지 않았다.
- disposable PostgreSQL 회귀는 W14F migration을 두 번 적용하고 새 컬럼·fence 함수를
  검증하도록 확장했다.
- `git diff --check`: PASS.
- `pytest -q tests/unit/test_goal_*.py`: **340 passed**.
- W-14F/W-13/W-12 집중 unit 4개 파일: **41 passed**.
- disposable PostgreSQL `goal_w14f_r5b_test`에서 migration 반복 적용·RLS·trigger·
  fence 함수 회귀: **1 passed**.
- 러너 후보의 `.runner_full_diff.patch`는 최종 변경에서 제외했다.
- npm/next/docker build: 승인 후 Runner 빌드 검증 대상.
- production DB 조회·기록을 하지 않았으므로 B-04 승인 완료를 주장하지 않는다.
- commit/push는 최종 통합 단계에서 기록하며 deploy는 수행하지 않았다.
