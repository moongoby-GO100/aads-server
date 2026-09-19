# PRD — 목표관리 업무계층 및 승인체계

- 문서 버전: 1.0
- 작성일: 2026-09-19 KST
- 상태: 설계 적용 완료 / 구현 미착수
- 대상: AADS Goal Management, GoalPanel, Approval Gate, Pipeline Runner
- 관련 목표: `cf1ec2f6-0072-4f85-aa5e-b08760cd6613`
- 관련 마일스톤: M12~M16
- 상위 기획: [`../plans/20260919_GOAL_EPIC_STORY_TASK_기획서.md`](../plans/20260919_GOAL_EPIC_STORY_TASK_기획서.md)

## 1. 제품 목표

각 세션이 목표를 같은 의미로 분해하고, 자기 프로젝트 경계 안에서 유일한 담당과
협업하며, 필요한 결정만 적절한 권한자에게 승인받고, 실행과 완료를 증거로 검증하게
한다.

### 1.1 핵심 성과

1. Goal → Milestone → Epic → Story → Task를 단일 트리에서 추적한다.
2. 프로젝트별 활성 담당 역할과 세션의 중복을 DB에서 차단한다.
3. `[CEO] 통합지시`만 교차 프로젝트를 조율하되 실행 담당은 각 프로젝트에 둔다.
4. 승인된 내용·버전과 실제 실행 내용을 일치시키고 중복 실행을 막는다.
5. 승인, 실행 성공, 완료 수락을 별도 상태로 관리한다.

### 1.2 비목표

- 기존 `agent_permission_requests`를 대체하는 새 승인 UI 구축
- 모든 Story/Task에 CEO 승인을 요구하는 수동 워크플로
- 배포·금융·시크릿 작업의 안전 게이트 완화
- 현재 운영 데이터의 무검증 자동 정리

## 2. 사용자와 사용자 여정

| 사용자 | 핵심 업무 | 첫 진입 | 반복 사용 | 실패 복구 |
|---|---|---|---|---|
| CEO | 목표·성과기준·교차 프로젝트 결정 | 승인 필요/막힘 요약 | A3 diff 검토·결정 | 거절·회수·롤백 |
| 목표 주도 | Milestone 기준선·프로젝트 Epic 조율 | 목표 트리 | 진척·위험·승인 대기 관리 | change set 보완 |
| 프로젝트 담당 | Story/Task 실행 | 내 담당 필터 | Task 실행·증거 제출 | 재시도·차단 해제 요청 |
| 독립 검수자 | 인수기준 검증 | 검수 대기함 | 증거 검토·수락/보완 | 기준 불명확 시 에스컬레이션 |
| 운영자 | 배포·관찰·롤백 | 릴리스 후보 | health·동일 digest 확인 | 즉시 라우팅 롤백 |

## 3. 상태 모델

### 3.1 Work item 상태

```text
draft → ready → in_progress → in_review → completed
           └──────────────→ blocked ──→ in_progress
draft/ready/in_progress → cancelled
in_review → changes_requested → in_progress
```

- `completed`는 승인 상태가 아니라 검수 결과다.
- blocker가 있거나 필수 evidence가 없으면 `in_review` 진입을 거절한다.
- 부모 롤업은 자식 상태를 계산하되 상위 완료 수락을 대신하지 않는다.

### 3.2 Change set/승인 상태

```text
draft → pending → approved → executing → executed
          ├─ rejected → revised → pending
          ├─ expired
          └─ superseded
approved → revoked
executing → failed → retry_pending | superseded
```

- `base_version`과 현재 대상 버전이 다르면 `approved`여도 실행하지 않고
  `superseded` 처리한다.
- 승인된 JSON Patch의 정규화 SHA-256과 실행 직전 SHA-256이 같아야 한다.
- 실행 소비자는 `owner_instance + owner_epoch` lease와 `execution_key`를 가진다.

## 4. 권한과 승인 정책

### 4.1 역할

| 역할 | 권한 |
|---|---|
| CEO | Goal/Milestone 기준선, 교차 프로젝트, A3 승인·회수 |
| 목표 주도 | 목표 내 Epic 조율, A2 요청·프로젝트 간 의존성 제안 |
| 프로젝트 주도 | 자기 프로젝트 Epic/Story A2 승인 |
| 담당 세션 | 배정된 Story/Task 실행·증거 제출 |
| 독립 검수자 | Story/Epic/Milestone 수락·보완요청 |

수행자와 검수자는 달라야 한다. CEO override는 허용하되 사유와 원래 요구된 검수
역할을 감사로그에 남긴다.

### 4.2 정책 판정 입력

```json
{
  "tenant_id": "...",
  "project": "AADS",
  "workspace_kind": "project|ceo_integrated",
  "actor_session_id": "...",
  "actor_role_key": "...",
  "target_type": "goal|milestone|epic|story|task",
  "target_id": "...",
  "action": "create|update|assign|cancel|execute|accept",
  "base_version": 7,
  "risk_factors": ["cross_project", "production_deploy"],
  "patch_hash": "sha256:..."
}
```

판정 결과는 `AUTO`, `NOTIFY`, `PROJECT_APPROVAL`, `CEO_APPROVAL`, `DENY` 중
하나다. `DENY`는 프로젝트/테넌트 경계 위반이며 승인으로 우회할 수 없다.

### 4.3 정책표

| 대상/행위 | 프로젝트 내부 | 교차 프로젝트/고위험 | 완료 판정 |
|---|---|---|---|
| Goal 초안 | NOTIFY | CEO_APPROVAL | CEO/독립 검수 |
| Goal 활성화·성공기준·취소 | CEO_APPROVAL | CEO_APPROVAL | CEO |
| Milestone 기준선·순서·기준 | CEO_APPROVAL | CEO_APPROVAL | 독립 검수 + CEO 수락 |
| Epic 생성·기준 변경 | PROJECT_APPROVAL | CEO_APPROVAL | 프로젝트 독립 검수 |
| Story 생성·기준 변경 | 승인된 Epic 안 AUTO, 밖 PROJECT_APPROVAL | CEO_APPROVAL | 독립 검수 |
| Task 생성·안전 재시도 | AUTO | 실행 위험 게이트 재분류 | 담당 외 검수 또는 자동 테스트 |
| 담당 생성·교체 | PROJECT_APPROVAL | CEO_APPROVAL | 고유성 제약 통과 |
| 운영 배포·DB 스키마·금융·시크릿 | 해당 없음 | CEO_APPROVAL | 운영 검증 별도 |

## 5. 기능 요구사항

### FR-001 계층 무결성

- Epic은 Milestone 아래 최상위 work item이다.
- Story 부모는 Epic, Task 부모는 Story만 허용한다.
- parent 관계와 dependency 관계를 별도 저장한다.
- 순환 참조, 다른 tenant 부모, 다른 project 부모를 거절한다.

### FR-002 프로젝트 담당 고유성

- `(tenant_id, project, role_key)` 활성 담당은 최대 1개다.
- `(tenant_id, project, session_id)` 활성 담당은 최대 1개다.
- 부족한 담당은 동일 project workspace 안에서만 생성한다.
- `[CEO] 통합지시`는 로컬 담당을 선택·조율할 수 있지만 로컬 담당 정본을
  자기 세션으로 치환하지 않는다.

### FR-003 불변 change set

- 승인 대상 변경은 원본 객체를 바로 수정하지 않고 `work_item_change_sets`에
  JSON Patch로 저장한다.
- change set은 대상 버전, diff, 이유, 예상 효과, 롤백, 위험등급, idempotency
  key를 가진다.
- 제출 후 수정은 새 revision을 만들고 이전 승인을 `superseded` 처리한다.

### FR-004 승인 라우팅

- 정책 엔진이 A0~A3에 해당하는 판정을 반환한다.
- A2/A3는 `agent_permission_requests`에
  `gate_source='goal_workflow'`로 생성한다.
- `approval_scope`에 change_set_id, target, base_version, patch_hash, project,
  required_role을 저장한다.
- A3는 bulk approval과 session/project 자동승인 범위를 허용하지 않는다.

### FR-005 승인 실행

- 승인 결정 후 outbox 이벤트를 한 번만 만든다.
- 실행 전 tenant/project/actor/target version/patch hash/만료/회수를 재검증한다.
- 실패가 재시도 가능하고 change set이 같으면 승인 재사용, 내용이 달라지면 재승인한다.
- 실행 결과와 affected rows/files/job SHA를 이벤트에 기록한다.

### FR-006 완료 증거와 독립 검수

- Task evidence: 명령 결과, 테스트, 파일/commit/job 식별자 중 정책에 필요한 항목.
- Story evidence: 인수기준별 pass/fail과 연결 Task 증거.
- Epic/Milestone evidence: 하위 롤업 + 사용자/API/E2E 검증.
- 수행자는 자기 Story/Epic/Milestone을 수락할 수 없다.
- 승인됐지만 검수 실패하면 `changes_requested`; 승인을 취소하거나 완료로 위조하지 않는다.

### FR-007 자동 롤업

- 진행률은 가중치가 없으면 자식 완료 수 기반으로 계산한다.
- 진행률과 상태는 구분한다. 100% 계산이어도 수락 전에는 `in_review`다.
- 선택 Task는 분모에서 제외하고 UI에 별도 표시한다.

### FR-008 실패 복구

- 화면은 마지막 실패 단계, 오류 요약, 승인 유효 여부, 재시도 가능 여부를 표시한다.
- 네트워크 재전송은 idempotency key로 같은 요청을 반환한다.
- 승인 대기 중 기준 버전 변경은 “계획 변경으로 기존 승인 만료”를 표시한다.
- owner lease 상실 시 새 소비자가 동일 execution_key 결과를 조회하고 이어받는다.

### FR-009 감사·알림

- 제안·승인·거절·만료·회수·실행·검수·롤백을 append-only 이벤트로 남긴다.
- 자동 통과도 정책 id, 등급, 사용 승인 범위, 잔여 횟수를 기록한다.
- 승인 알림은 프로젝트명, 목표명, 대상, 변경 요약, 결정권자, 만료를 포함한다.

## 6. 데이터 요구사항

### 6.1 `work_items`

필수 컬럼:

```text
id, tenant_id, project, goal_id, milestone_id, parent_id,
type(epic|story|task), title, description, acceptance_criteria,
status, priority, assignment_id, progress, version,
idempotency_key, created_by, created_at, updated_at, completed_at
```

고유키: `(tenant_id, project, parent_id, idempotency_key)`.

### 6.2 `project_role_assignments`

```text
id, tenant_id, project, role_key, session_id, active,
assigned_by, assigned_at, ended_at, end_reason
```

활성 role과 session 각각 partial UNIQUE를 둔다.

### 6.3 `work_item_change_sets`

```text
id, tenant_id, project, target_type, target_id, action,
base_version, patch(jsonb), patch_hash, rationale, expected_effect,
rollback_plan, risk_tier, state, approval_request_id,
idempotency_key, requested_by, decided_by, decided_at,
execution_key, executed_at, created_at, updated_at
```

고유키: `(tenant_id, idempotency_key)`와 실행 시 `execution_key` UNIQUE.

### 6.4 `work_item_events`

```text
id, tenant_id, project, aggregate_type, aggregate_id,
event_type, actor_session_id, actor_role_key, payload,
correlation_id, causation_id, created_at
```

UPDATE/DELETE를 금지하고 보존기간 정책에 따라 파티셔닝한다.

## 7. API 계약

### 7.1 생성·변경

`POST /api/v1/goals/{goal_id}/work-items`

- 입력: type, parent_id, title, acceptance_criteria, assignment_id,
  idempotency_key
- 응답: work_item, policy_decision, change_set_id/approval_request_id(필요 시)
- 오류: 403 project_scope_denied, 409 duplicate_assignment/version_conflict,
  422 invalid_parent

`POST /api/v1/work-items/{id}/change-sets`

- 입력: base_version, patch, rationale, expected_effect, rollback_plan,
  idempotency_key
- 서버가 risk tier와 required approver를 판정한다. 클라이언트 값을 신뢰하지 않는다.

### 7.2 조회·검수

- `GET /api/v1/goals/{goal_id}/tree?include=evidence,approvals,dependencies`
- `GET /api/v1/work-items/{id}/approval-preview`
- `POST /api/v1/work-items/{id}/submit-review`
- `POST /api/v1/work-items/{id}/accept`
- `POST /api/v1/work-items/{id}/request-changes`
- `GET /api/v1/goals/{goal_id}/governance`

모든 응답은 `version`, `project`, `tenant_id`(권한상 노출 가능 시),
`last_error`, `pending_approval_count`를 일관되게 제공한다.

## 8. UX 요구사항

### 8.1 Goal 상세

- 상단: 목표 상태, 진행률, 현재 Milestone, 승인 필요, 막힘, 마지막 오류.
- 본문: Milestone → Epic → Story → Task 트리.
- 기본 액션: 내 작업 보기, 승인 보기, 검수 보기, 실패 재시도.
- 설정·담당 관리·정책 편집은 보조 패널로 분리한다.

### 8.2 승인 카드

필수 표시:

1. 누가 무엇을 왜 바꾸는가.
2. 변경 전/후 diff와 영향을 받는 프로젝트.
3. 권장안과 비권장 사유.
4. 실행될 API/도구 범위와 횟수·만료.
5. 롤백 방법과 승인하지 않을 때 유지되는 상태.

버튼: `승인`, `보완 요청`, `거절`. A3에는 `일괄 승인`과 넓은 범위 승인 버튼을
노출하지 않는다.

### 8.3 모바일·복구

- 390px에서 주요 버튼 44px 이상, 가로 스크롤 없이 핵심 요약 노출.
- 세션 재로그인 후 동일 route와 펼친 대상 복구.
- 중복 클릭은 버튼 잠금이 아니라 서버 idempotency 결과로 안전하게 처리.
- 실패 시 막다른 화면 대신 `재시도`, `보완 요청`, `담당 열기`를 제공한다.

## 9. 비기능 요구사항

| 영역 | 요구사항 |
|---|---|
| 보안 | tenant/project deny는 fail closed, 승인으로 우회 불가 |
| 일관성 | change set 적용과 outbox 기록은 한 트랜잭션 |
| 중복 방지 | create/decide/execute 전 경로 idempotent |
| 관측성 | correlation_id로 제안→승인→실행→검수 추적 |
| 성능 | Goal tree p95 500ms 이내(1,000 work items 기준) |
| 접근성 | 키보드 승인 흐름, focus 유지, 상태를 색만으로 구분하지 않음 |
| 배포 | immutable image, candidate health, 짧은 nginx lock, 동일 digest standby |

## 10. 검증 계획

### 10.1 필수 회귀시험

| ID | 시나리오 | 기대 결과 |
|---|---|---|
| T01 | Task 부모로 Story 지정 | 422 invalid_parent |
| T02 | 다른 tenant 부모 연결 | 403/404, 존재 노출 없음 |
| T03 | 프로젝트에 같은 role 담당 재생성 | 409 duplicate_assignment |
| T04 | 일반 AADS 세션이 GO100 담당 지정 | 403 project_scope_denied |
| T05 | CEO 통합지시가 GO100 로컬 담당 지정 | 성공, 담당은 GO100 assignment |
| T06 | 승인 대기 중 대상 version 변경 | 기존 승인 superseded |
| T07 | 승인 후 patch 변조 | 실행 차단 + 감사 이벤트 |
| T08 | 승인 콜백 중복 2회 | 실행 1회 |
| T09 | A3 일괄승인 시도 | 400 bulk_not_allowed |
| T10 | 수행자가 자기 Story 수락 | 403 separation_of_duties |
| T11 | evidence 없는 완료 | 422 evidence_required |
| T12 | 네트워크 재전송 동일 key | 같은 resource 반환 |
| T13 | lease 중간 상실 후 재개 | 중복 실행 없이 완료/실패 1건 |
| T14 | 승인 회수 후 실행 큐 소비 | 실행 차단 |
| T15 | 모바일 세션 만료 후 로그인 | 같은 Goal/승인 카드 복구 |

### 10.2 완료 판정

- 단위·통합·테넌트 격리·동시성 시험 통과.
- 390/768/1440px에서 Goal 트리와 승인/복구 화면 캡처.
- 미인증 API 401, 타 프로젝트 403, health 200 확인.
- `deploy.sh bluegreen`으로 한 release SHA당 이미지 1개 빌드.
- candidate direct health → cutover → routed health → same-digest standby.
- P0/P1 신규 오류 없이 5분 관찰 후에만 릴리스 완료.

## 11. 단계별 출시와 롤백

| 단계 | 기능 플래그 | 롤백 |
|---|---|---|
| M12 스키마 | `GOAL_WORK_HIERARCHY_ENABLED=false` | 읽기 중지, additive schema 유지 |
| M13 API | `GOAL_WORK_ITEMS_API_ENABLED=false` | 기존 goals/milestones API 유지 |
| M14 승인 | `GOAL_WORKFLOW_APPROVAL_ENABLED=false` | 신규 change set 실행 차단, 기존 실행 게이트 유지 |
| M15 UI | dashboard feature flag | 기존 GoalPanel로 복귀 |
| M16 운영 | blue/green route | 이전 동일 이미지 슬롯으로 즉시 롤백 |

데이터 롤백은 생성된 work item을 삭제하지 않고 `cancelled`/`superseded`로
종료한다. 승인·감사 이벤트는 보존한다.

## 12. 구현 순서와 의존성

| 순위 | 구현 | 의존 | 완료 기준 |
|---|---|---|---|
| P0 | M12 스키마·고유성·버전 제약 | 선행 목표 무결성 커밋 반영 | migration up/down·경쟁 insert 시험 |
| P0 | M13 정책 입력·프로젝트 경계 API | M12 | T01~T05 통과 |
| P0 | M14 change set·승인·outbox | M13 | T06~T14 통과 |
| P1 | M15 GoalPanel·승인 diff·복구 UI | M14 | T15 + 3 viewport E2E |
| P1 | M16 데이터 정리·블루그린 릴리스 | M15 | 동일 digest + 5분 관찰 |

## 13. 현재 구현 상태

| 항목 | 상태 | 근거/차단 |
|---|---|---|
| 기획 설계 | 완료 | 본 문서의 상위 기획 |
| PRD | 완료 | 본 문서 |
| M12 코드 | 미반영 | `runner-e56868f1` approval commit gate 실패 |
| 선행 목표 무결성 코드 | 검수 보류 | `runner-cd3ff19a` review infrastructure failure |
| 운영 DB 마이그레이션 | 미실행 | 선행 커밋·검수 전 실행 금지 |
| 대시보드·배포·E2E | 미실행 | M12~M14 선행 필요 |
