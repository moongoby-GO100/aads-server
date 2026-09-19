# 목표관리 업무계층·승인체계 통합 기획서

- 작성: 2026-09-19 KST
- 개정: 2026-09-19 KST — 단계별 자동승인 권한, 회수·비상차단·정책 시뮬레이션 반영
- 관련 목표: `cf1ec2f6-0072-4f85-aa5e-b08760cd6613` 목표·마일스톤 시스템 무결성 확보
- 구현 PRD: [`../prd/20260919_GOAL_WORK_HIERARCHY_APPROVAL_PRD.md`](../prd/20260919_GOAL_WORK_HIERARCHY_APPROVAL_PRD.md)
- 이어받는 문서: `20260914_GOAL_ORCHESTRATION_기획서.md`, `20260915_APPROVAL_GATE_PRD.md`

## 1. 결론

제안한 Epic → Story → Task 구분은 도입한다. 다만 마일스톤을 작업 종류로
대체하지 않고 아래처럼 **성과 게이트와 실행 계층을 분리**한다.

```text
Goal (달성할 사업·운영 결과)
└─ Milestone (성과 게이트·완료 판정 시점)
   └─ Epic (큰 기능·업무 덩어리)
      └─ Story (사용자 가치가 검증 가능한 요구사항)
         └─ Task (코드·조사·배포 등 구체 실행)
```

승인은 계층마다 반복하지 않는다. **방향을 바꾸는 결정**, **되돌리기 어렵거나
위험한 실행**, **완료 수락**을 분리한다. 승인된 Epic 범위 안의 Story/Task
분해와 안전한 재시도는 자동 진행하고, 목표 성공기준·마일스톤 기준선·교차
프로젝트·배포/금융/보안 변경은 명시 승인을 받는다.

## 2. 현황과 설계 판단

### 2.1 현재 구조의 결함

| 문제 | 현재 근거 | 사용자 영향 | 설계 조치 |
|---|---|---|---|
| 마일스톤이 성과 게이트와 실행 작업을 겸함 | `goals`, `milestones`, `goal_task_links` 구조 | 완료 의미와 담당 책임이 모호함 | `work_items`로 Epic/Story/Task 분리 |
| 완료 증거와 승인 결과가 섞임 | 목표 API와 승인 게이트가 별도 경로 | 승인만으로 완료처럼 보일 수 있음 | 승인·실행·검수 상태 분리 |
| 프로젝트 담당 경계가 경고 수준 | `add_goal_session`은 프로젝트가 달라도 차단하지 않음 | 다른 프로젝트 맥락·권한 혼입 | DB 제약 + API 권한검사 |
| 기존 승인 게이트가 실행 도구 중심 | `live_trading_guard`, `next_step_proposals` | “무엇을 만들지”의 기준선 변경 추적 부족 | 버전 고정 change set 도입 |
| 범용 고유키만으로 중복 방지 | 일부 멱등키가 전역 또는 없음 | 재시도 시 중복 Epic/Story/Task 가능 | 테넌트·프로젝트·부모 범위 멱등키 |

### 2.2 최신 제품 설계에서 취할 원칙

| 근거 | 확인된 원칙 | AADS 적용 |
|---|---|---|
| Jira Cloud | Epic은 큰 업무, Story/Task는 표준 실행 항목이며 계층 변경 시 영향 확인이 필요 | 계층 마이그레이션을 버전·검증·롤백 가능한 변경으로 처리 |
| Linear | Initiative/Project는 목적과 결과, Issue는 일상 실행이며 Milestone은 의미 있는 완료 단계 | Milestone을 Task 종류가 아닌 성과 게이트로 유지 |
| GitHub Issues | sub-issue 계층과 dependency는 서로 다른 관계 | 부모/자식과 `blocks` 의존성을 별도 모델링 |
| NIST AI RMF Playbook | 사람 감독, 예외·에스컬레이션, go/no-go 결정과 감사로그를 문서화 | 승인 근거·결정자·버전·실행 결과를 감사 이벤트로 보존 |

공식 참고:

- Jira work type hierarchy: https://support.atlassian.com/jira-cloud-administration/docs/what-are-issue-types/
- Linear conceptual model: https://linear.app/docs/conceptual-model
- GitHub issues/sub-issues/dependencies: https://docs.github.com/en/issues/tracking-your-work-with-issues/learning-about-issues/about-issues
- NIST AI RMF Playbook: https://airc.nist.gov/airmf-resources/playbook/

## 3. 업무 계층과 책임

| 계층 | 답해야 하는 질문 | 단일 책임자 | 완료 조건 |
|---|---|---|---|
| Goal | 어떤 결과를 달성하는가 | CEO 또는 위임된 목표 주도 | 모든 필수 Milestone 수락 |
| Milestone | 언제 성과를 달성했다고 판정하는가 | 목표 주도 | 기준 충족 증거 + 독립 검수 |
| Epic | 어떤 큰 기능/업무 덩어리인가 | 프로젝트 담당 | 필수 Story 전체 수락 |
| Story | 사용자가 무엇을 할 수 있어야 하는가 | 프로젝트 담당 세션 | 인수기준 + Task 증거 + 검수 |
| Task | 실제 무엇을 실행하는가 | 담당 세션/Runner | 실행 결과 + 산출물 식별자 |

규칙:

1. Task 완료가 Story 완료를 자동 **제안**할 수는 있지만 자동 **수락**하지 않는다.
2. 부모/자식과 선행/차단 관계는 분리한다. `parent_id`와 `work_item_dependencies`
   를 혼용하지 않는다.
3. Story는 사용자 가치와 인수기준이 있어야 하며, 단순 구현 단계는 Task다.
4. Milestone은 날짜 묶음이 아니라 측정 가능한 성과 게이트다.
5. 한 Epic은 한 프로젝트에만 속한다. 교차 프로젝트 Goal은 프로젝트별 Epic으로
   나누고 `[CEO] 통합지시`가 조율한다.

## 4. 담당·협업 거버넌스

### 4.1 프로젝트 경계

| 세션 종류 | 자기 프로젝트 조회/실행 | 타 프로젝트 조회 | 타 프로젝트 담당 지정/변경 |
|---|---|---|---|
| 일반 프로젝트 세션 | 허용 | 차단 | 차단 |
| 프로젝트 주도 세션 | 허용 | 필요 범위만 읽기 | 차단 |
| `[CEO] 통합지시` | 전체 조율 | 허용 | 프로젝트 내부 후보 중 지정 가능 |

`[CEO] 통합지시`의 예외는 “모든 프로젝트 담당 역할을 대신 수행”하는 예외가
아니다. 교차 프로젝트 Goal을 만들고 각 프로젝트의 로컬 담당에게 Epic을
배정·조율하는 권한이다.

### 4.2 담당 고유성

담당 고유성은 work item마다 같은 역할을 재사용하지 못하게 하는 규칙이 아니다.
그렇게 하면 한 담당이 같은 목표의 Story 두 개를 맡을 수 없다. 별도
`project_role_assignments` 정본을 두고 다음 두 제약을 적용한다.

```sql
CREATE UNIQUE INDEX uq_project_active_role
ON project_role_assignments(tenant_id, project, role_key)
WHERE active;

CREATE UNIQUE INDEX uq_project_active_session
ON project_role_assignments(tenant_id, project, session_id)
WHERE active;
```

- 역할이 없으면 **같은 프로젝트 안에서만** 새 담당 세션을 생성한다.
- 같은 프로젝트의 활성 `role_key` 또는 `session_id` 중복 생성은 409로 거절한다.
- Work item은 `assignment_id`를 참조하며 임의 `role_key` 문자열을 직접 저장하지
  않는다.
- 담당 교체는 기존 assignment를 종료하고 새 assignment를 만든다. 과거 Task의
  감사 이력은 기존 assignment를 유지한다.

## 5. 승인체계

### 5.1 세 가지 게이트를 분리한다

| 게이트 | 질문 | 결과 |
|---|---|---|
| 방향 승인 | “이 범위·성공기준·담당으로 진행할 것인가?” | 기준선 버전 승인/거절 |
| 실행 승인 | “이 도구 실행은 위험 범위 안인가?” | `PASS` / `NOTIFY` / `APPROVE` |
| 완료 수락 | “증거가 인수기준을 충족했는가?” | 수락/보완요청 |

방향 승인과 완료 수락은 같은 버튼이 아니다. 계획을 승인한 사람이 자신의 구현
결과까지 자동 수락하는 구조를 금지한다.

### 5.2 승인 등급

| 등급 | 대표 사례 | 결정권자 | 처리 |
|---|---|---|---|
| A0 자동 | 승인된 Epic 안의 Task 분해, 읽기, 테스트 재실행 | 시스템 | 실행 후 감사 이벤트 |
| A1 알림 | 비핵심 설명·예상일·우선순위 보정 | 프로젝트 주도 | 중단 없이 알림 |
| A2 프로젝트 승인 | Epic 기준선, Story 인수기준, 프로젝트 내 담당 교체 | 프로젝트 주도 또는 지정 리뷰어 | 승인 전 미실행 |
| A3 CEO 승인 | Goal 성공기준/활성화/취소, Milestone 기준선·순서, 교차 프로젝트, 운영 배포, 금융·보안·파괴적 변경 | CEO | 승인 전 미실행, 일괄승인 금지 |

기존 `live_trading_guard`의 `TIER_PASS/NOTIFY/APPROVE`는 **실행 승인**에 계속
사용한다. 목표관리 도메인에는 `gate_source='goal_workflow'`를 추가하고,
변경 제안의 정확한 `change_set_id`, `base_version`, `patch_hash`, `project`를
승인 범위에 고정한다. 승인 후 내용이 한 글자라도 바뀌면 기존 승인은 무효다.

### 5.3 주요 행위별 판정

| 행위 | 기본 등급 | 추가 조건 |
|---|---|---|
| Goal 초안 작성 | A1 | 활성화는 A3 |
| Goal 성공기준·프로젝트 범위 변경 | A3 | 기존 승인은 superseded |
| Milestone 최초 기준선·순서·완료기준 변경 | A3 | diff와 롤백 필수 |
| Epic 생성/범위·인수기준 변경 | A2 | 교차 프로젝트면 A3 |
| Story 생성·Task 분해 | A0 | 승인된 Epic 범위를 벗어나면 A2 |
| 담당 생성·교체 | A2 | 타 프로젝트 또는 총괄 배정이면 A3 |
| Task 재시도 | A0 | 대상/명령/위험등급 변화 시 재분류 |
| DB 스키마·운영 배포·시크릿·금융 실행 | A3 | 기존 실행 게이트와 이중 확인 |
| Story/Epic/Milestone 완료 수락 | 독립 검수 | 수행자 자기수락 금지 |

### 5.4 승인 수명주기

```text
draft → proposed → pending_approval → approved → executing → executed
                    ├─ rejected → revised → proposed
                    ├─ expired
                    └─ superseded

approved → revoked (실행 전만 가능)
executed → verification_pending → accepted | changes_requested
```

- 승인 자체와 work item 상태를 분리한다.
- `approved`는 실행 허가이지 `completed`가 아니다.
- 승인 실행은 DB lease로 한 소비자만 소유하며 `execution_key`로 정확히 한 번만
  반영한다.
- 거절 사유, 수정 diff, 만료·회수, 실제 실행 결과를 append-only 이벤트로 남긴다.

### 5.5 단계별 자동승인 권한

자동승인은 목표 전체의 `high/critical` 스위치가 아니라, 권한자가 미리 정한
범위 안에서만 작동하는 capability grant로 관리한다.

| 단계 | 자동승인 가능한 범위 | 자동승인 금지선 |
|---|---|---|
| Goal | 초안 보강·읽기·증거 연결 | 활성화·성공기준·범위·취소 |
| Milestone | 진행률·증거·상태 알림 | 기준선·순서·인수기준·수락 |
| Epic | 승인된 Milestone 안 동일 프로젝트 보정 | 범위/프로젝트 변경·수락 |
| Story | 승인된 Epic 안 생성·분해·로컬 배정 | 인수기준 확대·프로젝트 변경·수락 |
| Task | 안전 실행·테스트·동일 입력 재시도 | 배포·DB schema·금융·시크릿·파괴 작업 |

각 grant는 `session_id + assignment_id`, tenant/project/goal/단계/target, 행위,
위험등급, environment, 횟수, 파일·행·비용 예산, 만료, policy version에 묶는다.
재위임은 기본 금지하고 CEO가 명시한 경우에도 1단계·원 grant 교집합만 허용한다.
자기 발급·자기 승인·자기 독립 검수는 금지한다.

판정은 `명시적 DENY/kill switch → A3 강제 수동 → grant 일치 → 원자적 소진 →
실행 직전 재검증` 순서다. 권한이 없거나 만료·회수·담당교체·정책변경이 감지되면
자동 실행하지 않고 기존 승인 경로로 올린다. 여러 grant의 범위를 합쳐 한 요청을
통과시키지 않는다.

동일 `execution_key` 재시도는 같은 소비 예약을 재사용해 이중 차감·이중 실행을
막는다. 선행 단계 evidence와 독립 검수가 없으면 다음 단계 권한을 소비하지 않는다.
회수 시 실행 중 작업은 사전에 고정한 `중단|현재 단계 완료|보상` 전략으로 처리하며,
정책 저장소나 캐시 무효화가 실패하면 자동승인이 아니라 수동 승인으로 전환한다.

운영 화면은 활성 grant의 주체·단계·행위·만료·잔여 횟수·마지막 사용·발급자를
표시하고 즉시 회수와 project/goal kill switch를 제공한다. 새 정책은 과거 결정에
shadow replay하여 권한 확대가 0건일 때만 `audit_only → project canary → enabled`
순서로 활성화한다. 상세 데이터·API·회귀시험은 PRD 4.4~4.9, FR-010~013,
T16~T35를 따른다.

## 6. 데이터·API 개요

### 6.1 정본 모델

| 정본 | 용도 |
|---|---|
| `work_items` | Epic/Story/Task 내용·상태·버전·프로젝트 |
| `project_role_assignments` | 프로젝트별 유일 담당 세션 |
| `work_item_dependencies` | blocks/blocked_by 관계 |
| `work_item_change_sets` | 승인받을 불변 diff, 위험등급, 기준 버전 |
| `agent_permission_requests` | 기존 승인 UI·결정 엔진 재사용 |
| `work_item_events` | 생성·승인·실행·검수·롤백 감사로그 |

핵심 무결성:

- `work_items`: `(tenant_id, project, parent_id, idempotency_key)` UNIQUE
- Epic 부모는 없음, Story 부모는 Epic, Task 부모는 Story. 교차 행 CHECK가 아닌
  트리거와 서비스 계층에서 둘 다 검증한다.
- update는 `WHERE id=? AND version=?` 낙관적 잠금으로 충돌을 409 처리한다.
- 완료는 `evidence_count > 0`, 필수 검수 통과, 미해결 blocker 0을 모두 만족해야
  한다.

### 6.2 API

```text
GET    /api/v1/goals/{goal_id}/tree
POST   /api/v1/goals/{goal_id}/work-items
POST   /api/v1/work-items/{id}/change-sets
GET    /api/v1/work-items/{id}/approval-preview
POST   /api/v1/work-items/{id}/submit-review
POST   /api/v1/work-items/{id}/accept
POST   /api/v1/work-items/{id}/request-changes
GET    /api/v1/goals/{goal_id}/governance
```

승인 결정은 기존 `POST /api/v1/approvals/{id}/decide`를 재사용한다. 승인 서비스가
`goal_workflow` 요청을 결정하면 change set을 다시 검증하고, 버전이 같을 때만
outbox에 실행 이벤트를 넣는다.

## 7. 화면 설계

### 7.1 첫 진입

`/goals/{id}` 첫 화면은 설정이 아니라 현재 결정 지점을 보여준다.

```text
[목표 상태·진행률·마지막 오류] [승인 필요 2] [재시도 1]
Milestone
└─ Epic
   └─ Story
      └─ Task
```

- 승인 필요 카드는 대상, 변경 전/후, 영향 프로젝트, 위험, 권장안, 롤백,
  만료를 한 화면에서 보여준다.
- A3는 체크박스 일괄승인 대상에서 제외한다.
- 거절은 사유가 필수이고, 보완요청은 원래 change set과 연결한다.

### 7.2 반복 사용·실패 복구

- 기본 뷰는 “내 담당·진행 중·승인 필요·막힘” 필터다.
- 세션 만료는 로그인 복구 후 같은 Goal/승인 카드로 돌아온다.
- 네트워크 실패 시 승인 버튼을 중복 전송하지 않고 동일 idempotency key로
  결과를 재조회한다.
- 실행 실패는 마지막 오류, 재시도 가능 여부, 승인 재사용 여부를 같이 표시한다.
- 모바일 390px에서 승인/거절 버튼은 44px 이상 터치 영역과 2행 이내 요약을
  제공하며 상세 diff는 펼침 영역으로 둔다.

## 8. 구현 단계

| 단계 | 범위 | 선행 | 완료 기준 |
|---|---|---|---|
| M12 | 업무계층·담당·change set 스키마 | 선행 러너 반영 | 마이그레이션/제약/롤백 시험 |
| M13 | CRUD·트리·의존성·프로젝트 권한 API | M12 | API 계약·테넌트 격리 시험 |
| M14 | A0~A3 판정·단계별 grant·원자 소진·회수·shadow 정책·exactly-once 실행 | M13 | 승인 변조·동시 소비·회수·stale grant·kill switch 시험 |
| M15 | GoalPanel 트리·승인 diff·검수·복구 UI | M14 | 390/768/1440px E2E 캡처 |
| M16 | 데이터 정리·블루그린 릴리스·관찰 | M15 | 동일 digest, routed health, 5분 P0/P1 무오류 |

## 9. 성공 지표

| 지표 | 목표 |
|---|---|
| 프로젝트 경계 위반 | 0건 |
| 활성 담당 role/session 중복 | 0건 |
| 승인된 diff와 실행 diff 불일치 | 0건 |
| 승인 1건의 중복 실행 | 0건 |
| 증거·독립 검수 없는 완료 | 0건 |
| A0/A1인데 CEO 승인을 요구한 오탐 | 0건 |
| 만료·회수 후 실행 | 0건 |
| grant 범위 밖 자동승인 | 0건 |
| 담당교체·정책변경 뒤 stale grant 실행 | 0건 |
| 자동결정의 decision id·정책버전 추적 누락 | 0건 |
| 동일 실행 재시도의 grant 이중 차감 | 0건 |
| 증거 없는 다음 단계 자동 전진 | 0건 |
| 회수·kill switch 전파 | p95 5초 이내 |

## 10. 범위 밖

- 자연어만으로 운영 배포·금융 실행까지 무승인 자동화
- `[CEO] 통합지시` 세션을 각 프로젝트의 로컬 담당으로 중복 등록
- 승인과 완료 수락을 한 버튼으로 합치기
- 승인 후 변경된 diff를 기존 승인으로 실행하기
- 이 문서 단계에서 운영 DB 변경·코드 배포 수행
