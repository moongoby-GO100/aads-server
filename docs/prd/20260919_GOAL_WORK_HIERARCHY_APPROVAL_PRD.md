# PRD — 목표관리 업무계층 및 승인체계

- 문서 버전: 1.1
- 작성일: 2026-09-19 KST
- 상태: 단계별 자동승인 권한 설계 적용 완료 / 구현 미착수
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
6. 자동승인은 전역 스위치가 아니라 주체·단계·행위·위험·횟수·만료가 제한된
   사전 위임 권한으로 발급하고 매 실행마다 원자적으로 소진한다.

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
  "patch_hash": "sha256:...",
  "environment": "dev|staging|production",
  "estimated_files": 2,
  "estimated_rows": 0,
  "estimated_cost_usd": 0,
  "grant_id": "optional-uuid",
  "policy_version": "sha256:..."
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

### 4.4 단계별 자동승인 권한

자동승인은 에이전트가 자기 요청을 승인하는 기능이 아니다. 권한자가 미리 정한
좁은 범위 안에서 정책 엔진이 `AUTO`를 반환하는 **제한된 사전 위임**이다. 다음
차원을 모두 일치시켜야 하며 하나라도 없거나 불일치하면 상위 승인으로
에스컬레이션한다.

| 차원 | 필수 제한 | 예시 |
|---|---|---|
| 주체 | 불변 `session_id` + 활성 `assignment_id` | AADS GoalSystemAdmin 세션 |
| 자원 | tenant/project/goal/stage/target | AADS의 특정 Goal 아래 Story |
| 행위 | allowlist | `create_story`, `retry_task` |
| 위험 | 최대 등급 + 금지요인 | A1 이하, production 제외 |
| 예산 | 횟수·파일·행·비용·시간 상한 | 10회, 3 files, $0 |
| 수명 | `valid_from`, `expires_at`, 정책 버전 | 24시간, policy hash 고정 |
| 위임 | 발급자·부모 grant·깊이 | 기본 0, CEO 명시 시 최대 1 |

단계별 기본 권한은 다음과 같다. `허용 가능`도 grant가 실제 발급된 경우에만
자동 통과하며, grant가 없으면 기존 A0~A3 정책을 따른다.

| 단계 | 자동승인 허용 가능 | 항상 명시 승인/독립 수락 |
|---|---|---|
| Goal | 초안 설명 보강, 읽기, 증거 연결 | 활성화·성공기준·프로젝트 범위·취소 |
| Milestone | 진행률 동기화, 증거 수집, 상태 알림 | 기준선·순서·인수기준·최종 수락 |
| Epic | 승인된 Milestone 안의 동일 프로젝트 초안/비핵심 보정 | 범위·프로젝트 변경, 교차 프로젝트, 최종 수락 |
| Story | 승인된 Epic 범위 안 생성·분해·동일 프로젝트 배정 | 인수기준 확대, 프로젝트 변경, 최종 수락 |
| Task | 안전한 생성·실행·테스트·동일 입력 재시도 | production 배포, DB schema/대량변경, 금융·시크릿, 상위 완료 수락 |

### 4.5 권한 발급·위임·회수 규칙

1. 발급자는 자신이 현재 가진 유효 권한보다 넓은 grant를 만들 수 없다. CEO도
   테넌트·프로젝트 경계 DENY를 우회하지 않는다.
2. 프로젝트 주도는 자기 프로젝트의 Epic/Story/Task만 발급할 수 있다.
   `[CEO] 통합지시`는 교차 프로젝트 조율 grant를 발급할 수 있지만 실제 실행
   주체는 해당 프로젝트의 활성 assignment여야 한다.
3. 자기 자신에게 새 권한을 발급하거나 자기 grant의 범위·횟수·만료를 늘릴 수
   없다. 재위임은 기본 금지하며 CEO가 `delegation_depth=1`을 명시한 경우만
   원 grant 범위의 교집합 안에서 한 단계 허용한다.
   grant 요청자·대상 주체는 승인자가 될 수 없고, A2 grant와 위임 허용 변경은
   발급 권한자와 독립 승인자의 결정을 모두 기록한다.
4. 역할 이름이 아니라 재사용되지 않는 session/assignment UUID에 결합한다.
   담당 교체·세션 비활성·Goal 종료·정책 버전 변경 시 grant를 즉시 `revoked` 또는
   `stale` 처리한다.
5. 회수는 즉시 적용한다. 큐에 있거나 이미 승인된 실행도 실행 직전 grant 상태를
   재검증한다. 실행 중 작업은 행위별 `cancel_now|finish_current|compensate` 정책을
   grant에 고정하며, 안전 중단·보상 결과를 감사로그에 남긴다.
6. 긴급 `kill switch`는 tenant/project/goal별로 제공하고 모든 permit보다
   우선한다. 해제는 원래 grant를 자동 복구하지 않고 재검토를 요구한다.

### 4.6 판정·소진 순서

```text
1. tenant/project/identity/kill-switch DENY
2. A3 또는 mandatory-human action DENY_AUTO
3. 활성 assignment + grant scope + policy version 검증
4. risk/context/budget/expiry/delegation 교집합 검증
5. DB 시각으로 만료를 확인하고 grant row 잠금 후 사용량을 원자적으로 예약
6. decision_id + matched policy/grant + reason code 기록
7. 실행 직전 version/hash/revocation 재검증 후 outbox 생성
```

기본값은 `DENY_AUTO`다. 명시적 DENY는 어떤 permit보다 우선한다. 복수 grant가
일치해도 범위를 합쳐 더 넓은 권한을 만들지 않고, 요청 전체를 만족하는 단일
grant만 사용한다. 가장 좁고 가장 빨리 만료되는 grant를 우선 선택한다.
동일 `execution_key` 재시도는 기존 예약/소비 건을 재사용하며 횟수를 다시 차감하지
않는다. 새 key만 새 사용으로 계산하고, 외부 부작용 여부가 불명확한 실패는 사용량을
자동 환급하지 않고 `manual_reconciliation`으로 보낸다.

### 4.7 자동승인 불가선

- Goal 활성화/취소와 성공기준 변경, Milestone 기준선/최종 수락
- 교차 프로젝트 실행 담당 치환, 자기 승인·자기 독립 검수
- production 배포·라우팅 전환, DB schema/광범위 데이터 변경
- 금융 주문·자금·결제, 시크릿·권한·보안정책 변경, 파괴적 작업
- 승인 당시와 다른 `patch_hash`, target version, project, environment

이 항목은 grant에 잘못 포함돼도 `mandatory-human` deny 정책이 우선한다.

### 4.8 정책 운영 안전장치

- 정책은 버전·hash로 고정하고 변경 전 `simulate`와 shadow mode로 최근 결정에
  재생한다. 예상보다 권한이 넓어지는 diff가 1건이라도 있으면 활성화하지 않는다.
- 신규 정책은 `audit_only → project canary → enabled` 순으로 승격하고 단계별
  판정 차이와 수동 override를 검수한다.
- 정책 평가기와 실행기를 분리한다. 실행기는 서명된 `decision_id`, grant version,
  input hash를 검증하고 임의 `AUTO` 문자열을 신뢰하지 않는다.
- 자동승인율이 아니라 `manual override`, `scope miss`, `stale grant`, `revoke 후
  차단`, `자기승인 차단`을 운영 지표로 본다.
- decision log는 입력·결과·정책 revision을 남기되 시크릿과 개인정보 경로를
  저장 전에 마스킹한다.
- 30일마다 활성 grant를 재인증하고, 7일 이상 미사용 grant는 자동 만료 후보로
  표시한다. 실제 기간은 tenant 정책으로 더 짧게 설정할 수 있다.
- 정책 캐시는 event 기반 무효화와 짧은 TTL을 함께 사용한다. 무효화나 정책 저장소
  조회가 실패하면 캐시된 permit으로 진행하지 않고 수동 승인으로 fail closed한다.

### 4.9 정책 근거

- Cedar는 principal/action/resource/context를 함께 평가하고 기본 거부 및 명시적
  거부 우선 모델을 제공한다: https://docs.cedarpolicy.com/policies/syntax-policy.html
  (공식 문서, 2026-09-19 KST 확인)
- OPA는 정책 배포와 실행 지점의 분리, decision id·policy revision이 포함된
  결정 로그 및 민감 필드 마스킹을 설명한다:
  https://www.openpolicyagent.org/docs/management-decision-logs
  (공식 문서, 2026-09-19 KST 확인)
- NIST SP 800-207은 자원 단위·동적 정책·지속 검증을 요구한다:
  https://csrc.nist.gov/pubs/sp/800/207/final
  (공식 문서, 2026-09-19 KST 확인)

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

### FR-010 자동승인 grant

- grant 생성 시 서버가 발급자의 유효 범위와 요청 범위의 교집합을 계산한다.
- `A3`, `mandatory_human`, 다른 tenant/project 실행은 저장 단계부터 거절한다.
- grant 사용량은 `SELECT ... FOR UPDATE` 또는 조건부 UPDATE로 원자적 소진한다.
- 횟수 초과·만료·정책 version 불일치는 `AUTO`가 아닌 승인 요청으로 전환한다.
- 소비 예약은 `execution_key`로 멱등 처리한다. 동일 key 재시도는 한 번만 차감하고,
  다른 key의 동시 요청은 예산·병렬 상한 안에서만 예약한다.
- 다음 단계 전환은 선행 단계의 필수 evidence와 독립 검수 결과가 확인된 경우만
  허용하며, 미충족 시 grant를 소비하지 않는다.

### FR-011 회수·비상차단

- 개별 grant, session, assignment, goal, project, tenant 단위 회수를 제공한다.
- 회수 이벤트와 결정 시각 사이의 경합에서도 실행 직전 재검증으로 fail closed한다.
- kill switch 상태에서는 읽기·감사 조회 외 모든 자동승인을 차단한다.
- 실행 중 작업은 grant에 고정된 회수 전략에 따라 중단·현재 단계 완료·보상하고,
  상위 grant 회수는 하위 grant 신규 실행을 함께 차단한다.

### FR-012 정책 설명·시뮬레이션

- 모든 판정은 `decision_id`, `policy_version`, `matched_grant_id`, `reason_codes`,
  `remaining_uses`를 반환한다.
- 정책 후보를 과거 결정 입력에 재생하는 dry-run API를 제공하며 실제 grant를
  소진하거나 실행 이벤트를 만들지 않는다.
- shadow 결과와 실제 결과의 차이를 저장하고 활성화 전 검수한다.
- 정책을 `audit_only`, project canary, 전체 활성 순으로 승격하며 각 단계의 승인자와
  rollback 정책 버전을 기록한다.

### FR-013 권한 재인증

- 담당 교체, 세션 종료, Goal/Milestone 종료, 정책 변경 시 관련 grant를 stale로
  만든다.
- 만료 임박·장기 미사용·범위 초과 시 소유자와 발급자에게 알리고 자동 연장하지
  않는다.

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

### 6.5 `goal_auto_approval_grants`

```text
id, tenant_id, project, principal_session_id, assignment_id,
goal_id, milestone_id, epic_id, story_id, actions, tool_groups,
max_risk_tier, environments, conditions, max_executions, used_executions,
max_files, max_rows, max_cost_usd, max_parallel, max_duration_seconds,
valid_from, expires_at, idle_timeout_seconds, delegation_depth, parent_grant_id,
policy_version, grant_version, scope_hash, revocation_strategy,
status, requested_by, issued_by, approved_by, issued_at,
revoked_by, revoked_at, revoke_reason
```

- 활성 grant는 immutable이며 확대·연장은 기존 행 수정이 아니라 새 revision으로
  발급한다.
- `(tenant_id, id)`와 `(tenant_id, principal_session_id, status)` 인덱스를 두고,
  `used_executions <= max_executions`, 만료, non-negative budget을 CHECK한다.
- `goal_auto_approval_uses`에는 `decision_id`, `execution_key`, grant/version,
  input hash, target/version, patch hash, action, budget delta, 예약·실행·완료 시각,
  상태, 결과, correlation id를 append-only로 기록한다.
- `(tenant_id, execution_key)` UNIQUE로 재전송을 멱등 처리한다. 예약 뒤 외부
  부작용이 없다고 증명된 경우만 사용량을 반환하고, 그 외에는 수동 대조한다.

### 6.6 `goal_approval_policy_versions`

정책 원문과 정규화 hash, 작성자·승인자, 적용 범위, 모드
(`audit_only|canary|enabled|retired`), 적용 시각, 이전 버전, simulation 결과를
불변으로 보존한다. 정책 변경은 기존 grant를 자동 확대하지 않으며 새 deny는 즉시
적용한다.

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
- `GET /api/v1/goals/{goal_id}/auto-approval-grants`
- `POST /api/v1/goals/{goal_id}/auto-approval-grants/preview`
- `POST /api/v1/goals/{goal_id}/auto-approval-grants`
- `POST /api/v1/auto-approval-grants/{id}/revoke`
- `GET /api/v1/auto-approval-grants/{id}/usage`
- `POST /api/v1/goal-policy/simulate`

모든 응답은 `version`, `project`, `tenant_id`(권한상 노출 가능 시),
`last_error`, `pending_approval_count`를 일관되게 제공한다.

grant API는 유효 범위·잔여 횟수·만료·발급자·회수 상태를 반환한다. `preview`와
`simulate`는 저장/소진하지 않으며, 요청 범위 중 자동승인 불가 항목과 축소된
교집합을 명시한다.

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

### 8.4 자동승인 권한 화면

- Goal 상세에 `활성 자동승인 권한` 패널을 두고 단계·행위·주체·만료·잔여 횟수·
  마지막 사용·발급자를 한눈에 표시한다.
- `권한 회수`는 즉시 실행하고 확인 결과의 decision id를 보여준다.
- 권한 생성은 기본 최소 범위를 제안하며 A3/production/금융/시크릿을 선택할 수
  없게 한다. 범위가 넓어질수록 경고만 띄우지 말고 서버 preview가 거부한다.
- 일반 실행 화면에는 현재 작업에 적용된 grant와 남은 범위만 간단히 표시하고
  정책 편집은 Admin/Settings 보조 경로에 둔다.
- 실행 타임라인은 적용 grant/policy version, 생성된 evidence, 다음 수동 게이트를
  연결하고, 회수 후 대기·실행 중·보상 상태를 서로 구분한다.

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
| 권한 안전 | 명시 deny 우선, 단일 grant 완전 일치, 정책·대상·grant 실행 직전 재검증 |
| 시간 | 만료·idle timeout은 PostgreSQL DB 시각 기준, 클라이언트 시각 불신 |
| 비밀 보호 | grant token 원문·민감 context 로깅 금지, hash/식별자만 저장 |

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
| T16 | grant 없이 A1 자동승인 시도 | 기존 승인 경로로 에스컬레이션 |
| T17 | Story grant로 Epic 변경 시도 | scope_mismatch 차단 |
| T18 | A3 행위를 grant에 포함해 발급 | 저장 단계 422 mandatory_human |
| T19 | 자기 자신에게 grant 발급 | 403 self_grant_denied |
| T20 | max 1회 grant 동시 소비 2건 | 정확히 1건만 AUTO |
| T21 | 회수와 실행 큐 소비 경합 | 실행 0건, revoked reason 기록 |
| T22 | 담당 session 교체 후 이전 grant 사용 | stale_assignment 차단 |
| T23 | policy version 변경 후 grant 사용 | stale_policy 차단 |
| T24 | 두 grant 범위 조합으로 요청 충족 시도 | 합성 금지, 승인 요청 전환 |
| T25 | CEO 통합지시가 로컬 담당 없이 실행 | 403 local_assignment_required |
| T26 | kill switch 활성 중 자동 실행 | 읽기 외 전부 차단 |
| T27 | simulate/shadow 실행 | 사용량·데이터 변경 0건 |
| T28 | decision log에 시크릿 입력 | 지정 경로 마스킹 확인 |
| T29 | grant 만료 직전 큐잉 후 만료 뒤 실행 | 실행 직전 차단 |
| T30 | 동일 execution_key 3회 재시도 | 사용량 1회, 외부 실행 최대 1회 |
| T31 | 다음 단계 grant와 evidence 누락 | grant 미소비, 단계 전진 0건 |
| T32 | 상위 grant 회수 후 하위 grant 사용 | 신규 실행 0건 |
| T33 | 비용·행·병렬·시간 상한 초과 | 원자 차단 + 사유 기록 |
| T34 | 정책 캐시 무효화/저장소 장애 | 자동승인 0건, 수동 경로 전환 |
| T35 | 회수 중 in-flight 실행 | 고정 회수 전략대로 중단/완료/보상 |

### 10.2 완료 판정

- 단위·통합·테넌트 격리·동시성 시험 통과.
- 390/768/1440px에서 Goal 트리와 승인/복구 화면 캡처.
- 미인증 API 401, 타 프로젝트 403, health 200 확인.
- `deploy.sh bluegreen`으로 한 release SHA당 이미지 1개 빌드.
- candidate direct health → cutover → routed health → same-digest standby.
- P0/P1 신규 오류 없이 5분 관찰 후에만 릴리스 완료.
- T16~T35 통과와 함께 자동승인 결정 100%가 decision id·policy/grant version으로
  역추적되어야 한다.
- 이중 소비·A3 자동승인·회수 뒤 신규 실행·증거 없는 단계 전진은 각각 0건이다.
- kill switch와 상위 grant 회수 전파는 p95 5초 이내다.

## 11. 단계별 출시와 롤백

| 단계 | 기능 플래그 | 롤백 |
|---|---|---|
| M12 스키마 | `GOAL_WORK_HIERARCHY_ENABLED=false` | 읽기 중지, additive schema 유지 |
| M13 API | `GOAL_WORK_ITEMS_API_ENABLED=false` | 기존 goals/milestones API 유지 |
| M14 승인 | `GOAL_WORKFLOW_APPROVAL_ENABLED=false`, `GOAL_AUTO_APPROVAL_MODE=off\|audit\|canary\|on` | 신규 grant 자동실행 차단, 수동 승인·기존 실행 게이트 유지 |
| M15 UI | dashboard feature flag | 기존 GoalPanel로 복귀 |
| M16 운영 | blue/green route | 이전 동일 이미지 슬롯으로 즉시 롤백 |

데이터 롤백은 생성된 work item을 삭제하지 않고 `cancelled`/`superseded`로
종료한다. 승인·감사 이벤트는 보존한다.

## 12. 구현 순서와 의존성

| 순위 | 구현 | 의존 | 완료 기준 |
|---|---|---|---|
| P0 | M12 스키마·고유성·버전 제약 | 선행 목표 무결성 커밋 반영 | migration up/down·경쟁 insert 시험 |
| P0 | M13 정책 입력·프로젝트 경계 API | M12 | T01~T05 통과 |
| P0 | M14a change set·승인·outbox | M13 | T06~T14 통과 |
| P0 | M14b 단계별 grant·원자 소진·회수 | M14a | T16~T26, T29~T35 통과 |
| P0 | M14c 정책 simulate·shadow·결정로그 마스킹 | M14b | T27~T28 통과 |
| P1 | M15 GoalPanel·승인 diff·복구 UI | M14c | T15 + 3 viewport E2E |
| P1 | M16 데이터 정리·블루그린 릴리스 | M15 | 동일 digest + 5분 관찰 |

## 13. 현재 구현 상태

| 항목 | 상태 | 근거/차단 |
|---|---|---|
| 기획 설계 | 완료 | 본 문서의 상위 기획 |
| PRD | 완료 | 본 문서 |
| 단계별 자동승인 권한 설계 | 완료 | 4.4~4.9, FR-010~013, T16~T35 |
| M12 코드 | 미반영 | `runner-e56868f1` approval commit gate 실패 |
| 선행 목표 무결성 코드 | 검수 보류 | `runner-cd3ff19a` review infrastructure failure |
| 운영 DB 마이그레이션 | 미실행 | 선행 커밋·검수 전 실행 금지 |
| 대시보드·배포·E2E | 미실행 | M12~M14 선행 필요 |
