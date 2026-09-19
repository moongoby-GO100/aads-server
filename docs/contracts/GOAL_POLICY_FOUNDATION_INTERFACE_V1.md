# Goal Policy Foundation Interface v1

- 상태: **승인 (B-04, 2026-09-19 17:09 KST)**
- 대상: W-14F evaluator / executor
- PRD: `docs/prd/20260919_GOAL_WORK_HIERARCHY_APPROVAL_PRD.md` v1.2
- 원칙: 본 계약 승인 전 W-14F 구현 및 추가 운영 migration 금지

## 1. 신뢰 경계

| 경계 | Evaluator | Executor |
|---|---|---|
| service identity | 정책 평가 전용 | 실행 전용 |
| DB role | 정책·ledger 기록 최소권한 | lease·reservation·outbox 소비 최소권한 |
| 배포 권한 | 없음 | 승인된 artifact 실행만 |
| 서명키 | decision 서명 전용 접근 | 공개키/검증키만 접근 |
| 네트워크 | policy store·primary DB | 승인된 tool/executor allowlist |
| 감사 책임 | 모든 평가 결과 동기 기록 | 실행 전 재검증·실행 결과 기록 |

서비스 identity, DB role, 배포 권한, 서명키 접근을 공유하지 않는다. executor는
임의 문자열 `AUTO`를 신뢰하지 않고 아래 envelope의 서명, hash, version, epoch를
검증해야 한다.

## 2. 정책 결정 envelope

```json
{
  "decision_id": "uuid",
  "boundary_decision": "ALLOW|DENY",
  "approval_route": "NONE|NOTIFY|PROJECT_APPROVAL|CEO_APPROVAL|INDEPENDENT_REVIEW|PROJECT_AND_INDEPENDENT|CEO_AND_INDEPENDENT",
  "automation_eligibility": "BASELINE_AUTO|GRANT_REQUIRED|MANDATORY_HUMAN|NOT_EXECUTABLE",
  "risk_tier": "A0|A1|A2|A3",
  "result": "AUTO|APPROVAL_REQUIRED|DENY|NOT_EXECUTABLE",
  "reason_codes": [],
  "policy_version": 1,
  "matched_grant_id": null,
  "grant_version": null,
  "remaining_uses": null,
  "decision_input_hash": "sha256:...",
  "precondition_snapshot_hash": "sha256:...",
  "original_engine_result": "Allow|Deny",
  "effective_application_result": "AUTO|APPROVAL_REQUIRED|DENY|NOT_EXECUTABLE",
  "error_policy_ids": [],
  "error_kinds": [],
  "entity_resolution_status": "resolved|partial|failed",
  "fallback_route": null,
  "erased": [],
  "masked": [],
  "masking_policy_version": 1,
  "trace_id": "...",
  "span_id": "...",
  "correlation_id": "...",
  "decided_at": "timestamptz",
  "canonicalization_version": "RFC8785",
  "hash_algorithm": "SHA-256",
  "signature_algorithm": "implementation-approved",
  "signature": "base64url"
}
```

## 3. Canonical mapping

| 위험 | 경계 | 자동화 | 기본 경로 | grant |
|---|---|---|---|---|
| A0 | ALLOW | BASELINE_AUTO | NONE 또는 NOTIFY | 불필요 |
| A1 | ALLOW | GRANT_REQUIRED | 기존 승인 경로 | 유효한 단일 grant만 허용 |
| A2 | ALLOW | MANDATORY_HUMAN 또는 좁은 GRANT_REQUIRED | PROJECT_APPROVAL 이상 | 명시된 좁은 사전 위임만 |
| A3 | ALLOW | MANDATORY_HUMAN | CEO_APPROVAL | 발급·자동승인 금지 |
| 경계 위반 | DENY | NOT_EXECUTABLE | 없음 | 우회 불가 |

복수 grant 합성은 금지한다. `DENY_AUTO`는 결과값으로 저장하지 않고
`MANDATORY_HUMAN`과 reason code로 표현한다.

## 4. Evaluator 요청

클라이언트는 `expected_parent_version` 같은 낙관적 잠금값만 보낼 수 있다.
evaluator가 primary DB에서 `preconditions`를 계산한다.

```json
{
  "tenant_id": "uuid",
  "project": "AADS",
  "workspace_kind": "project|ceo",
  "principal_session_id": "uuid",
  "assignment_id": "uuid",
  "target_type": "goal|milestone|epic|story|task|change_set",
  "target_id": "uuid",
  "action": "string",
  "base_version": 7,
  "patch": [],
  "environment": "dev|staging|production",
  "risk_factors": [],
  "expected_parent_version": 7
}
```

서버 계산 snapshot은 `parent_state`, `parent_version`, `evidence_complete`,
`evidence_snapshot_hash`, `review_verdict`, `blocker_count`,
`dependency_blocker_count`를 포함한다. 실행 직전 다시 계산하며 변경 시 기존
결정을 폐기한다. `precondition_unmet`에서는 grant를 예약하거나 소비하지 않는다.

## 5. Hash와 서명 입력

1. patch는 RFC 6902 operation 배열 순서를 보존한다.
2. 배열 안 JSON 객체는 RFC 8785 JCS로 정규화한다.
3. duplicate property, NaN, Infinity, 비정상 숫자는 422 `invalid_patch`다.
4. UTF-8 bytes에 SHA-256을 적용해 `patch_hash`를 만든다.
5. 아래 필드를 동일 방식으로 정규화해 `decision_input_hash`를 만든다.

```text
tenant_id, project, workspace_kind, principal_session_id, assignment_id,
target_type, target_id, action, base_version, patch_hash, environment,
risk_factors, precondition_snapshot_hash, policy_version, grant_id, grant_version
```

서명/MAC은 `decision_id`, `decision_input_hash`, `effective_application_result`,
`policy_version`, `decided_at`을 포함한다. executor는 서명 불일치나 미지원
canonicalization version을 `NOT_EXECUTABLE`로 처리한다.

## 6. Diagnostics와 감사 원자성

- Cedar diagnostics error가 1건이라도 있으면 원 engine 결과가 Allow여도 AUTO를 폐기한다.
- 경계 entity resolution 실패는 승인 요청 없이 `NOT_EXECUTABLE`이다.
- mandatory-human 평가 오류는 AUTO를 금지하고 명시 승인으로 보낸다.
- simulate/shadow 오류는 실행하지 않고 diff와 오류만 저장한다.
- `goal_policy_decisions` 기록 실패 시 AUTO를 반환하지 않는다.
- AUTO와 usage reservation이 필요한 경우 decision, reservation, usage event,
  outbox를 한 DB transaction에서 기록한다.

민감값은 원문을 저장하지 않는다. 감사에는 `erased`와 `masked` JSON Pointer 및
`masking_policy_version`만 저장한다.

## 7. Executor 검증 계약

executor는 실행 직전 primary DB에서 다음을 재검증한다.

```text
target_version
kill_switch_epoch
deny_policy_epoch
assignment_epoch
grant_revocation_epoch
ancestor_revocation_epoch
policy_version
precondition_snapshot_hash
decision_input_hash
decision signature
```

permit cache는 이 값들에 적용하지 않는다. 일반 permit cache TTL은 최대 2초이며,
cache/policy store 조회 실패 때 cached permit으로 실행하지 않는다.

## 8. Grant reservation과 위임

동일 `execution_key`는 기존 reservation을 반환하며 다시 차감하지 않는다. grant
소진은 blocking `FOR UPDATE` 또는 조건부 `UPDATE ... RETURNING`만 사용한다.
`SKIP LOCKED`는 outbox/job queue claim에만 허용한다.

모든 ancestor의 active state, parent/current version, revocation epoch, scope subset,
delegation depth, issuer/principal 분리, assignment 활성, tenant/project/goal 경계를
검증한다. `scope_hash`만으로 회수 전파를 판정하지 않는다.

## 9. Outbox와 외부 실행

업무 변경과 outbox 생성은 동일 transaction의 논리적 1회다. relay는 최소 1회,
내부 consumer는 `execution_key` 기준 1회다. 외부 executor가 멱등키를 지원할 때만
effectively-once를 보장한다. 외부 결과가 불명확하면 자동 환급·재실행 없이
`reconciliation_required`로 전환한다.

## 10. 검수·회수 상태

검수자 0명은 `review_pending_assignment`로 전환하며 자동 수락하지 않는다.
수행자·요청자·담당자·evidence 제출자·execution actor·직접 기여 session은 해당
version의 독립 검수자가 될 수 없다. CEO override는 사유·원 요구 role·요구사항 ID·
risk acceptance를 기록한다.

실행 회수는 grant revoke와 분리해 `cancellation_requested`,
`finish_current_requested`, `compensating`, `compensated`, `compensation_failed`,
`reconciliation_required`로 기록한다.

## 11. Idempotency와 오류 계약

- 같은 idempotency key와 같은 canonical body: 기존 결과 반환
- 같은 key와 다른 body: 409 `idempotency_key_reused`
- version/hash/environment/policy/grant mismatch: 실행 차단
- 다른 tenant의 존재 노출 가능 요청: 외부 404, 내부 감사 `tenant_scope_denied`
- 표준 오류코드는 PRD v1.2 14.12를 따른다.

## 12. 승인 시험

B-04 승인에는 T01 수정본, T08 강화본, T36~T58 명세가 모두 연결돼야 한다.
특히 T36, T38, T45~T46, T48~T58은 executor가 fail-closed임을 입증해야 한다.
shadow 권한 확대, A3 AUTO, ledger 누락, 이중 소비, revoke/kill switch 이후 신규
실행, tenant 누출이 각 1건이라도 있으면 승인을 중단한다.

## 13. B-04 승인 기록

승인 이벤트에는 문서 commit SHA, 승인자 session UUID, 승인 시각, 승인한 contract
version, 관련 ADR 목록을 기록한다. 승인 전 상태는 `pending`이며 이 문서만으로
승인을 추정하지 않는다.

- 승인 범위 판정: `propose_next_steps` auto grant `82320385`
- 승인 세션: `1fa84036-b12d-4497-97f5-076a32645a20`
- 승인 시각: 2026-09-19 17:09 KST
- 계약 버전: v1
- 후속 차단: B-02/B-02R 독립 ACCEPT 전 W-14F 구현·운영 migration 금지
