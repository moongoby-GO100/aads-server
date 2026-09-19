# AADS 목표관리 거버넌스·업무계층 개편 기획서

- 문서 버전: v1.0
- 작성 시각: 2026-09-19 13:00 KST
- 대상: AADS Goal Control, 전 프로젝트 담당 세션, `[CEO] 통합지시`
- 관련 목표: `cf1ec2f6-0072-4f85-aa5e-b08760cd6613` — 목표·마일스톤 시스템 무결성 확보
- 상태: 기획 확정 권고안. 구현·마이그레이션·배포는 별도 검증 게이트를 거친다.

## 1. 결론

제안한 Epic → User Story → Task 구분은 도입하는 것이 맞다. 다만 마일스톤을
세 종류 중 하나로 바꾸면 안 된다. AADS에는 다음 5단계가 가장 적합하다.

```text
Goal (왜, 어떤 성과를 달성할 것인가)
└─ Milestone (어떤 검증 가능한 관문을 통과할 것인가)
   └─ Epic (큰 기능·업무 덩어리)
      └─ Story (사용자 또는 운영자가 얻는 독립 가치)
         └─ Task (세션·러너가 실행하는 구체 작업)
```

핵심은 **마일스톤과 작업 계층의 책임을 분리**하는 것이다. 마일스톤은 날짜나
진행률 이름이 아니라 증거를 요구하는 성과 게이트다. Epic/Story/Task는 그
게이트를 통과하기 위한 실행 분해다. 신규 작업은 이 구조를 따르고, 기존
`milestone → pipeline_job` 직접 링크는 이행 기간에만 호환한다.

프로젝트 담당 원칙은 다음과 같이 강제한다.

1. 일반 세션은 자기 `workspace.project_key`와 같은 프로젝트의 책임만 맡는다.
2. `[CEO] 통합지시` 세션만 `portfolio_coordinator` 자격으로 다른 프로젝트를
   감독·질의·조정할 수 있다.
3. 부족한 담당 세션을 만들 때는 요청자의 워크스페이스가 아니라 **대상
   프로젝트 워크스페이스** 안에 만든다.
4. 프로젝트 내 담당 고유성은 범용 `role_key`가 아니라 새
   `responsibility_key`로 판정한다. `CTO` 역할의 세션이 여러 개여도
   `goal-system-admin`, `release-ops`처럼 책임이 다르면 공존할 수 있다.
5. 같은 프로젝트·같은 `responsibility_key`의 활성 담당은 정확히 하나다.

## 2. 현재 시스템 실측

2026-09-19 13:00 KST 운영 DB와 현재 `main` 소스를 읽기 전용으로 대조했다.

| 항목 | 실측 결과 | 문제 |
|---|---:|---|
| 목표 | 28건 | `goals`에 `tenant_id`가 없어 인증 후에도 행 단위 격리 근거가 없다 |
| 주도 없는 목표 | 12건 | 목표 전체 판정·취합 책임이 비어 있다 |
| 마일스톤 | 308건 | 계층 타입 없이 모두 같은 테이블·같은 의미로 취급된다 |
| 담당 세션 없는 마일스톤 | 174건 | 역할만 있거나 둘 다 없어 자동 발송이 멈출 수 있다 |
| 완료 증거 없는 완료 마일스톤 | 15건 | 완료 상태와 검증 가능성이 분리돼 있다 |
| 활성 작업 링크 | 53건 | 48건은 목표 수준, 마일스톤 수준은 5건뿐이다 |
| 중복 마일스톤 묶음 | 13개 | 현재 관련 목표도 M6~M10이 각각 2건씩 생성됐다 |
| 프로젝트가 다른 활성 담당 링크 | 8건 | AADS←CEO 5건, GO100←CEO 3건이며 예외가 데이터로 명시되지 않았다 |
| 동일 목표·동일 역할 중복 | 1건 | NTV2 한 목표에 `CTO` 활성 링크가 4건이다 |
| 실행 중/대기 러너 | 2건 | AADS 목표 핵심 파일과 겹치는 러너가 있어 즉시 직접 수정은 충돌 위험이다 |

### 코드에서 확인한 원인

- `app/routers/goals.py::add_goal_owner`는 프로젝트가 달라도 경고만 하고 연결을
  허용한다. 새 원칙은 경고가 아니라 정책 판정과 거부가 필요하다.
- `app/services/owner_session_provision.py`는 새 담당을 **요청자와 같은
  workspace**에 만든다. CEO 주도가 요청하면 AADS 담당도 CEO workspace에
  생길 수 있어 새 지시와 충돌한다.
- 담당 세션 생성 멱등 키 `owner-session:{project}:{role}`은 방향은 맞지만
  DB 고유 제약이 없고, `role_key=CTO`처럼 넓은 역할을 책임 식별자로 겸용한다.
- `goals`와 `milestones`에는 상태·프로젝트·순서 값의 CHECK/UNIQUE 제약이 없다.
  실제로 같은 목표·순서·제목의 마일스톤이 중복 생성됐다.
- 현재 화면은 `Goal → Milestone → Task link`만 그리며 Epic/Story, 의존성,
  수용 기준, 담당 고유성 충돌을 표현하지 못한다.

## 3. 최신 제품·방법론 조사와 적용 판단

공식 문서를 2026-09-19 KST에 재확인했다.

| 근거 | 현재 방식 | AADS 적용 |
|---|---|---|
| Atlassian Jira | 기본 작업 계층은 Epic → Story → Subtask이며 상위 계층 변경 시 기존 부모·자식 관계가 깨질 수 있다고 경고한다 | 타입별 허용 부모를 고정하고, 전환은 일괄 덮어쓰기가 아니라 병행 이행한다 |
| GitHub Issues | 조직 공통 Issue Type, 부모/하위 이슈 진행률, 최대 8단계 중첩, 명시적 blocking dependency를 제공한다 | AADS는 복잡도를 제한해 3개 작업 단계만 허용하고, 계층과 의존성을 별도 테이블로 둔다 |
| Linear | Initiative는 여러 프로젝트를 묶고, Issue는 한 Team의 워크플로에 속하며, Initiative/Project health를 별도로 롤업한다 | CEO는 포트폴리오 조정자, 실제 실행 소유권은 프로젝트 하나에 귀속한다 |
| Azure Boards | Epic → Feature → Story → Task 계층과 독립적인 dependency/reference link를 사용하고, 프로젝트·팀별 자율 backlog를 포트폴리오에서 조회한다 | 프로젝트 로컬 실행과 CEO 통합 조회를 분리하고 `parent`와 `blocked_by`를 혼용하지 않는다 |
| Scrum Guide | Product Goal, 명확한 Backlog Item, Acceptance/Definition of Done, 짧은 검사·적응 주기를 강조한다 | Goal은 결과, Story는 수용 기준, Task는 실행 증거, Milestone은 독립 검수 게이트를 가진다 |
| Azure Boards MCP | 2026년 공식 문서는 AI 에이전트가 자연어로 Epic 생성·진행 조회·팀 할당을 수행하는 흐름을 제시한다 | AADS 도구도 자유 텍스트 INSERT가 아니라 스키마 검증 API를 통해 생성·이동·완료해야 한다 |

판단: 최신 도구의 공통점은 “깊은 트리” 자체가 아니라 **타입이 있는 작업,
단일 소유 팀, 별도 의존성, 자동 롤업, 증거 가능한 완료**다. 따라서 GitHub가
8단계를 지원하더라도 AADS는 Goal/Milestone 아래 3개 작업 단계로 제한한다.
에이전트가 임의로 더 깊게 쪼개면 진행률과 책임 소재가 다시 흐려진다.

## 4. 목표 데이터 모델

### 4.1 기존 엔터티 보강

`goals`

- `tenant_id uuid NOT NULL`
- `scope text CHECK (scope IN ('project','portfolio'))`
- `project_key text NOT NULL`
- `lead_session_id uuid NOT NULL` — 활성화 전 필수
- `version integer NOT NULL DEFAULT 1` — 낙관적 잠금
- 상태 CHECK: `draft, ready, active, blocked, review, completed, cancelled, archived`

`milestones`

- `tenant_id`, `project_key`, `idempotency_key`, `version`
- `outcome_statement`, `completion_criteria`, `required_evidence_schema`
- `reviewer_session_id`; 실행 담당과 검수 담당의 자기승인 금지
- `UNIQUE (tenant_id, goal_id, idempotency_key)`
- `UNIQUE (tenant_id, goal_id, sequence_order, variant)`

### 4.2 신규 `goal_work_items`

| 필드 | 의미 |
|---|---|
| `id`, `tenant_id`, `project_key` | 테넌트·프로젝트 격리의 정본 |
| `goal_id`, `milestone_id` | 어느 성과와 관문을 위한 일인지 |
| `parent_id` | Epic→Story→Task 한 단계 부모 |
| `item_type` | `epic`, `story`, `task` |
| `responsibility_key` | 프로젝트 내 고유 담당 책임 |
| `owner_session_id` | 실제 실행 세션 |
| `title`, `description` | 일의 내용 |
| `acceptance_criteria` | Story 필수, Task는 검증 명령/산출물 |
| `status` | `draft, ready, in_progress, blocked, review, done, cancelled` |
| `estimate_points`, `weight` | 롤업용; 없으면 leaf 개수 기반 |
| `idempotency_key`, `version` | 중복 생성·동시 수정 방지 |

DB 트리거 또는 서비스 정책으로 다음을 거부한다.

- Epic의 부모가 work item인 경우
- Story의 부모가 Epic이 아닌 경우
- Task의 부모가 Story가 아닌 경우
- 자기 자신 또는 자기 후손을 부모로 지정하는 순환
- 부모와 다른 tenant/project/goal/milestone 연결
- `done` 자식이 남은 부모의 완료

### 4.3 계층과 분리할 관계

- `goal_work_item_dependencies(blocker_id, blocked_id)` — 선행/차단 DAG
- `goal_work_item_evidence(work_item_id, kind, uri, sha256, observed_at, metadata)`
- `project_responsibilities(tenant_id, project_key, responsibility_key,
  session_id, status)`
- `goal_collaborators(goal_id, session_id, collaboration_role,
  scope_project_key)`
- `goal_events(entity_type, entity_id, event_type, idempotency_key, payload)` —
  감사와 outbox 처리

핵심 고유 제약은 다음과 같다.

```sql
CREATE UNIQUE INDEX uq_active_project_responsibility
ON project_responsibilities (tenant_id, project_key, responsibility_key)
WHERE status = 'active';
```

`role_key`는 “어떤 프롬프트 전문성을 붙일지”, `responsibility_key`는 “이
프로젝트에서 무엇을 전담하는지”다. 둘을 분리해야 `CTO`라는 역할을 쓰는 여러
전문 담당을 허용하면서도 같은 책임 담당의 중복은 차단할 수 있다.

## 5. 프로젝트 경계·CEO 예외 정책

모든 후보 조회, 수동 연결, 자동 생성, 재배정 경로가 같은 정책 함수 하나를
호출해야 한다.

```text
allow_assignment(goal, session, collaboration_role):
  require same tenant
  if session.project == goal.project:
      allow project_owner / contributor / reviewer
  else if session.project == "CEO" and collaboration_role == "portfolio_coordinator":
      allow coordination only
  else:
      deny cross_project_assignment
```

CEO 예외도 무제한 실행 권한은 아니다. CEO 세션은 프로젝트 담당에게 질문,
방향 제시, 우선순위 조정, 승인 요청을 할 수 있지만 프로젝트 로컬 Task의
`owner_session_id`가 되지는 않는다. 실행 담당이 없으면 대상 프로젝트
workspace에 담당을 생성하고 CEO 세션은 coordinator로 남는다.

`[CEO] 통합지시` 판정은 화면 이름 문자열이 아니라
`chat_workspaces.project_key='CEO'`와 명시적 capability로 한다. 향후 이름이
바뀌어도 정책이 흔들리지 않게 하기 위함이다.

## 6. 상태 머신과 완료 판정

### Task

`ready → in_progress → review → done`; 실패 시 `blocked`, 중단 시
`cancelled`. `done`에는 실제 runner/release/test/document evidence가 최소
1개 필요하다.

### Story

필수 Task가 모두 `done`이고 acceptance criteria 검증이 통과해야 `review`로
간다. 사용자 가치가 화면과 관련되면 캡처 또는 HTTP/API 폴백 근거가 필요하다.

### Epic

필수 Story 완료와 Epic exit criteria 충족으로 닫는다. 단순 자식 개수 완료만
으로 닫지 않는다.

### Milestone

연결된 필수 Epic 완료, milestone completion criteria, 증거 묶음, 독립
reviewer 승인이 모두 있어야 `completed`다. 실행 담당의 자기승인은 금지한다.

### Goal

필수 Milestone 완료와 success criteria 실측으로 종료한다. 진행률은 DB에
사람이 직접 쓰지 않고 leaf Task의 가중 롤업으로 계산한다. 차단된 항목은
진행률과 별도로 health=`at_risk/off_track`에 반영한다.

## 7. 사용자 흐름과 화면

### 첫 진입

`/goals` 첫 화면은 전체 트리를 펼치지 않는다. CEO에게 필요한 네 신호를 먼저
보인다: 성과 상태, 주도, 현재 milestone, 차단·승인 필요 수. 선택하면
Milestone → Epic → Story → Task를 단계적으로 펼친다.

### 반복 사용

- CEO: portfolio view에서 전 프로젝트 health·차단·승인 필요를 본다.
- 프로젝트 주도: 자기 프로젝트 backlog와 담당 고유성 충돌을 본다.
- 담당 세션: 채팅 상단 `My Work`에서 현재 Task, 상위 Story의 수용 기준,
  선행 의존성, 완료 신고 버튼을 함께 본다.

### 실패 복구

- 담당 없음: 대상 프로젝트의 기존 `responsibility_key` 후보를 먼저 제시한다.
- 후보 없음: 대상 프로젝트 workspace에 생성하는 승인 카드를 제시한다.
- 중복: 새 세션을 만들지 않고 기존 활성 담당으로 연결하거나 명시적 승계한다.
- 작업 실패: `blocked_by`, 마지막 오류, 재시도, 재배정, 범위 축소를 한 카드에
  보여 준다.
- 세션 만료/네트워크 실패: 저장된 Task 상태를 다시 불러와 동일 위치에서
  재개한다.

모바일은 트리 전체 그래프 대신 breadcrumb와 한 단계 목록을 기본으로 하고,
승인·재시도·완료 신고 버튼은 최소 44px 터치 영역을 보장한다.

## 8. API 계약

- `POST /goals/{goal_id}/work-items` — 타입별 필드 검증과 idempotency key 필수
- `PATCH /work-items/{id}` — `If-Match: version`으로 동시 수정 충돌 탐지
- `POST /work-items/{id}/transition` — 허용 상태 전이만 수행
- `POST /work-items/{id}/dependencies` — 순환 검증 후 차단 관계 생성
- `POST /work-items/{id}/evidence` — 증거 종류·해시·관측시각 저장
- `PUT /projects/{project}/responsibilities/{key}` — 단일 활성 담당 upsert/승계
- `POST /goals/{goal_id}/collaborators` — 동일 프로젝트 또는 CEO coordinator만 허용
- `GET /goals/{goal_id}/tree` — 계층, 의존성, 롤업, health를 한 응답으로 제공

에이전트용 도구도 이 API를 사용한다. SQL을 자유 생성해 구조를 바꾸지 않는다.
모든 mutation은 `tenant_id`, `project_key`, `idempotency_key`, actor session을
감사 이벤트에 남긴다.

## 9. 구현 마일스톤 권고

현재 목표의 M6~M11을 먼저 정상화한 뒤 아래를 이어 붙인다.

| 순서 | 마일스톤 | 완료 기준 |
|---:|---|---|
| M12 | 업무계층 스키마·정책 | work item/의존성/증거/책임 테이블과 CHECK·UNIQUE·순환 차단 테스트 통과 |
| M13 | 프로젝트 담당 고유성·CEO 예외 | 모든 연결 경로가 공통 policy를 사용하고 cross-project 음성 테스트 통과 |
| M14 | 상태 머신·증거 롤업 | Task→Story→Epic→Milestone→Goal 완료·차단·재개 통합 테스트 통과 |
| M15 | Goal Control 계층 UI | 데스크톱·모바일에서 생성, 이동, 충돌, 복구, 캡처 E2E 통과 |
| M16 | 운영 데이터 이행·출시 | 중복 격리, legacy 링크 이행, 블루그린, 동일 digest, 5분 P0/P1 관찰 통과 |

이 다섯 건은 동시에 시작하면 안 된다. M12와 M13은 일부 병렬 가능하지만,
M14는 두 스키마가 고정된 후, M15는 API 계약 후, M16은 전부 통과한 후 진행한다.

## 10. 데이터 이행 원칙

1. 기존 중복을 삭제하지 않고 `superseded_by`, `superseded_at`, 사유로 격리한다.
2. 현재 관련 목표의 M6~M10 중복부터 대표 행과 연결된 작업·증거를 대조한다.
3. 기존 목표 수준 chat session 링크는 `goal_collaborators`로 옮긴다.
4. 기존 milestone-level pipeline link는 임시 Story/Task로 감싸되 원본 링크 ID를
   migration metadata에 보존한다.
5. 역할명만 있는 담당은 프로젝트별 `responsibility_key`를 제안하고 사람이
   확정하기 전까지 `unclassified`로 둔다.
6. read-only shadow rollup으로 기존 progress와 신규 계산값을 비교한 뒤 전환한다.
7. 이행 후에만 신규 직접 `milestone → task` 생성 경로를 막는다.

## 11. 검증 매트릭스

| 영역 | 반드시 통과할 사례 |
|---|---|
| 테넌트 | 타 tenant goal/work item/session ID로 조회·수정 시 404/403, 행 유출 0건 |
| 프로젝트 | AADS 세션→GO100 담당 거부, CEO coordinator 허용, CEO가 만든 GO100 담당은 GO100 workspace 귀속 |
| 고유성 | 같은 project+responsibility 두 번째 활성화 실패, 승계 트랜잭션 성공 |
| 계층 | Epic→Task 직결, Story→Story, 순환, 다른 goal 부모 연결 전부 거부 |
| 멱등성 | 같은 생성 요청 10회에 row 1건, 이벤트·승인 카드도 1건 |
| 상태 | 미완료 자식/증거 없음/자기승인에서 상위 완료 거부 |
| 롤업 | leaf 가중치 합과 Goal 진행률 일치, cancelled 선택 항목 제외 규칙 일치 |
| 복구 | runner 실패→blocked→재시도 성공→review 복귀가 감사 이벤트로 남음 |
| UI | 첫 진입·반복 사용·실패 복구·모바일 터치·새로고침 상태 보존 E2E 통과 |
| 출시 | 커밋/푸시, 1 SHA 1 image, candidate health, cutover, routed health, standby 동일 digest, 5분 관찰 |

## 12. 성공 지표

- 프로젝트가 다른 일반 담당 연결: 0건
- 동일 프로젝트·동일 책임의 활성 담당 중복: 0건
- 같은 idempotency key의 중복 work item/milestone: 0건
- 완료 증거 없는 필수 Task·Milestone 완료: 0건
- 담당 없음 상태의 조용한 방치: 0건; 모두 승인 대기/재배정/차단 사유 중 하나
- 목표 진행률 수동값과 leaf 롤업 불일치: 0건
- CEO가 프로젝트별 채팅창을 열지 않고 차단·승인 필요 항목을 확인 가능

## 13. 리스크와 결정

| 선택 | 장점 | 리스크 | 결정 |
|---|---|---|---|
| 기존 milestones에 `type`만 추가 | 구현이 빠름 | 성과 게이트와 실행 항목이 섞이고 상태 규칙이 복잡해짐 | 비권장 |
| 범용 무제한 트리 | 유연함 | 에이전트가 깊이를 늘려 UX·롤업·책임이 불명확해짐 | 비권장 |
| 별도 typed work item + 3단계 제한 | 의미·제약·조회가 명확하고 점진 이행 가능 | 테이블·API·UI 추가 필요 | **권장** |

## 14. 공식 자료

- Atlassian, Configure the work type hierarchy, 2026-09-19 확인:
  https://support.atlassian.com/jira-cloud-administration/docs/configure-the-issue-type-hierarchy/
- GitHub Docs, Adding sub-issues, 2026-09-19 확인:
  https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues
- GitHub Docs, Managing issue types in an organization, 2026-09-19 확인:
  https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/managing-issue-types-in-an-organization
- GitHub Docs, Creating issue dependencies, 2026-09-19 확인:
  https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-issue-dependencies
- Linear Docs, Concepts / Initiatives, 2026-09-19 확인:
  https://linear.app/docs/conceptual-model
  https://linear.app/docs/initiatives
- Microsoft Learn, Manage work items / Portfolio backlogs, 2026-09-19 확인:
  https://learn.microsoft.com/en-us/azure/devops/boards/backlogs/manage-work-items
  https://learn.microsoft.com/en-us/azure/devops/boards/plans/portfolio-management
- Scrum Guide 2020 공식 PDF, 2026-09-19 확인:
  https://scrumguides.org/docs/scrumguide/v2020/2020-Scrum-Guide-US.pdf
