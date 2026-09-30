# 기획 대화 검토·설계 확정·목표 반영 PRD

- 버전: 1.0 / 상태: CEO 지시에 따른 상세 기획 기준선. 구현·검수·운영 적용은 별도 상태.
- 근거 실측: 2026-09-23 17:56:16 KST, DB SELECT 및 저장소 코드 조회.
- 프로젝트: AADS / 목표: `d17c7a5e-0923-4a00-8000-000000000001`.
- 원 세션: `8bf0405a-1f22-4ad9-bb09-6e0fce8c6339`.
- CEO 원문: “기획 설계 PRD우선 상세 작성 저장하고 진행해”.
- 우선순위 원문: “M1 보다 기획 설계 관련 마일스톤을 최상단에 배치해”, “이게 작동되면 지시서는 나중에 진행해도 될듯하다 제일 마지막으로 이동시켜놔”.
- 범위 원문: “해당 논의 건 부터 목표에 반영되게 해봐”, “마일스톤에 업무계층등록은 안하나?”.
- 연결 기록 키: `d17c7a5e-planning-conversation-prd-v1` (AADS DB 핸드오버).

## 1. 제품 결정과 완료의 의미

대표님은 기존 채팅에서 기획을 논의한다. 시스템은 논의 중인 설계와 변경안을 자동으로 갱신하고, 독립 검토와 근거 확인을 거친 권장안을 보여준다. 대표님이 특정 범위를 확정하면 그 범위의 목표·마일스톤·하위 업무를 한 번만 반영한다. 질문·가정·대안 비교는 정식 실행계획을 바꾸지 않는다.

이번 1차 제품 완료 범위는 **대화 → 설계 초안 → 독립 검토 → 권장안 교정 → CEO 확정 → 목표·마일스톤·업무계층 반영 → 화면/API/DB 검증**이다. 지시서 생성·제출·러너 연결 기능의 개선은 이 기능의 검수 이후 후순위로 진행한다. 구현을 위해 기존 Runner를 사용하는 것과 후순위 제품인 지시서 기능을 개발하는 것은 구분한다.

PRD 저장, 코드 작성, 테스트 통과, 운영 반영, 독립 인수는 각각 다른 완료 상태다. 이 문서 저장으로 M7~M11을 완료하거나 자동 진행을 켜지 않는다.

## 2. 현재 근거와 해결할 문제

| 확인 사항 | 근거 | 제품에 미치는 영향 |
|---|---|---|
| 목표 상태 blocked | 해당 목표 DB SELECT | 순서 변경만으로 구현이 시작되지 않음 |
| M7~M11은 순서 1~5, pending, auto_advance=false | milestones DB SELECT | 기획 우선 정렬과 실행 가능 여부를 구분해야 함 |
| M1은 순서 6, in_progress | milestones DB SELECT | 목표 차단 해제만 하면 기존 M1을 먼저 선택할 수 있음 |
| M4·M5 blocked, M5는 명시 보류 | milestones DB SELECT | 후순위 실패/보류가 기획 검수를 막지 않게 해야 함 |
| goal_manager는 일부 선택 경로에서 in_progress를 먼저 정렬 | app/services/goal_manager.py:384~386, 작업 트리 기준 | 단계별 착수 정책 필요. 현재 dirty 파일이므로 직접 수정 금지 |
| 초안 revision과 이벤트 저장 코드가 이미 있음 | app/services/directive_draft_service.py, app/api/directive_drafts.py | 패턴 재사용. 지시서가 설계 초안의 정본이 되게 하지 않음 |
| work_items·dependencies·events 테이블과 서비스가 있음 | information_schema 조회, app/services/goal_work_hierarchy.py, app/routers/work_items.py | 별도의 병렬 업무계층 엔진을 만들지 않음 |
| 담당 역할은 M7~M9 Developer, M10 GoalSystemAdmin, M11 PM | milestones.owner_role_key 조회 | 역할만 채우는 것으로 업무계층 등록 완료를 판정하지 않음 |
| 이 목표의 work_items 조회 결과 0건 | goal_id를 지정한 work_items SELECT | Epic/Story/Task는 아직 미등록. PRD의 분해표는 등록 계획이며 DB 등록 증거가 아님 |
| AAG brief가 partial/not_proven | aag_brief 결과 | findings 없음은 영향 없음의 증거가 아님. 구현 전 실제 파일 영향 조사 필요 |

위 항목 외의 운영 구현률·성능·비용은 미측정이다. 다른 목표의 업무계층 PRD가 존재한다는 사실을 이 목표의 Epic/Story/Task 등록 완료로 해석하지 않는다. 이 문서의 신규 테이블·API·플래그 이름은 제안이며 운영 존재를 주장하지 않는다.

## 3. 사용자와 사용자 여정

| 사용자 | 첫 진입 | 반복 사용 | 실패 복구 |
|---|---|---|---|
| CEO | 기존 채팅을 열고 기획 질문 입력 | 권장안 확인, 확정 또는 부분 수정 | 미검토/충돌 이유 확인 후 재검토·변경안 재생성 |
| PM/목표 주도 | 현재 목표의 설계·미결정 항목 확인 | 범위와 인수기준 정리, 하위 업무 검수 | 소유자·검수자 미배정 항목 보완 |
| 개발 담당 | 확정 PRD 버전과 배정 Task 확인 | 승인된 범위 구현, 증거 연결 | 범위 변경 요청·차단 사유 기록 |
| 독립 QA/Ops | 검수 대기 항목과 증거 확인 | 기능 검수·릴리스 검증 | 반려 사유와 실패 재현 경로 등록 |

첫 화면은 채팅과 현재 목표다. 리뷰 모델 설정·타임아웃·정책 편집은 Admin/Settings에 둔다. 대표님이 매 턴 리뷰어를 지정하거나 같은 내용을 다른 세션에 다시 설명하게 하지 않는다.

## 4. 범위와 자동화 경계

### 4.1 포함

- 기획 의도 판정, 원문·확정/거부 결정·현재 목표의 출처를 가진 컨텍스트 묶음.
- 변경 불가능한 설계 revision, 요구사항·금지사항·미결정 항목·계획 diff.
- 응답 전 독립 관점 검토, 검토 지적의 채택/기각과 근거, 권장안.
- 확정 범위의 변경안 승인·반영, 버전 충돌·중복·원자성·복구.
- Goal→Milestone→Epic→Story→Task와 담당/검수 역할의 연결.
- 채팅·목표 화면 표시, 세션 복귀, 상태 조회, 지표 및 회귀 사례.

### 4.2 제외 및 후순위

- M1~M6 지시서 실행 기능 개선의 신규 착수는 M11 이후. M5는 별도 보류 해제 전 제외.
- 현재 실행 중인 M1 산출물 삭제, 강제 종료, 기존 변경 되돌리기.
- 새 코드리뷰 시스템, 전역 자동 승인, 승인 없이 운영 배포·스키마 적용.
- 다른 목표/프로젝트 변경, 모든 대화에 고비용 리뷰 강제.
- 질문만으로 목표 활성화·담당 교체·마일스톤 완료 처리.

### 4.3 발언별 동작

| 발언 | 자동 수행 | 정식 계획 반영 |
|---|---|---|
| “장단점 보고해”, “어떻게 작동하나?” | 검토 및 설계 초안·대안 갱신 | 없음 |
| “이 안이면 어떨까?” | 가정으로 표시, 변경안 미리보기 | 없음 |
| “해당 논의 건부터 목표에 반영해” | 연결된 설계 버전·범위·diff 확인 | 참조 대상이 명확하고 권한/정책을 만족하면 해당 범위만 반영 |
| “순서만 바꿔” | 정렬 diff 생성 | 명시한 순서만 변경, 상태·의존성은 임의 변경하지 않음 |
| “PRD 먼저 저장하고 진행해” | PRD 저장 후 승인 범위의 구현 작업 연결 | 전 기능 확정·운영 배포까지 포괄 승인으로 해석하지 않음 |
| “그건 하지 마” | 금지 결정 기록, 관련 미실행 제안 무효화 | 이미 실행한 코드/배포 자동 롤백은 별도 판단 |

확정 대상이 여러 개면 자연어 승인만으로 임의 선택하지 않는다. 같은 채팅에서 대상 diff를 보여주고 짧게 명확화한다. 이미 승인한 동일 범위·동일 버전에는 중복 승인을 요구하지 않는다.

## 5. 정상 흐름과 상태 전이

1. 서버가 인증된 tenant/project/session과 현재 사용자 메시지를 확인한다. 목표 연결이 모호하면 초안만 저장한다.
2. 현재 발언, 직전 논의의 유효 결정, 취소/금지 결정, 목표와 마일스톤의 기준 버전을 스냅샷으로 묶는다. 과거 인용문을 새 지시로 실행하지 않는다.
3. 설계 초안을 저장한다. 문제·사용자·요구사항·대안·금지·완료 기준·미결정 사항을 구조화하고 각 항목에 원문 출처를 연결한다.
4. 응답 초안을 만든 뒤 독립 리뷰를 실행한다. 의도/범위, 대안/사용성, 구현/운영 위험 관점을 분리한다. 초기 권장 구성은 독립 리뷰 호출 2개가 관점을 나누어 맡는 방식이며 호출 수는 정책화한다.
5. 주 응답자가 각 지적의 근거를 확인한다. 채택/기각/미해결을 기록하고 권장안·비권장 사유·장단점·남은 쟁점을 교정한다. 리뷰 점수를 다수결처럼 실행 승인에 사용하지 않는다.
6. 교정된 답변과 설계 변경 요약을 표시한다. 리뷰 원문은 펼쳐보기로 제공한다. 정상 통과마다 별도 장문 리뷰 보고를 덧붙이지 않는다. 중요한 충돌·변경·미검토만 요약한다.
7. 계획 diff를 준비하고 CEO 확정 이벤트를 연결한다. 서버가 설계 hash, 대상 버전, tenant/project, actor 권한, 정책 결과를 다시 검증한다.
8. 정식 목표·마일스톤·업무계층 변경과 감사 이벤트를 하나의 DB 트랜잭션으로 반영한다. 알림/화면 반영은 outbox로 전달한다.
9. 채팅과 목표 패널이 같은 결과 revision을 표시한다. 실행 작업 생성은 별도 승인/기존 Runner 절차를 따르고 지시서 후순위 게이트를 확인한다.

```text
설계: draft → reviewing → reviewed | review_degraded → awaiting_confirmation
                                  ↓ 수정 발생
                            새 draft revision
반영: proposed → approved → applying → applied
                   ├─ version_conflict → 새 diff/필요시 재확정
                   ├─ revoked/expired → 실행 차단
                   └─ failed → 동일 멱등키로 재조회/복구
```

검토 실패 시에도 사용자 발언과 초안을 보존하고 미검토 답변을 제공할 수 있다. 그러나 미검토 상태를 정상 검수 통과로 표시하지 않는다. 1차 출시에서는 정식 자동 반영을 차단하고, CEO가 그 상태를 알고 명시적으로 결정한 예외만 기존 정책 허용 범위에서 사유와 함께 처리한다.

## 6. 기능 요구사항

| ID | 요구사항 | 수락 조건 |
|---|---|---|
| FR-01 | 현재 의도·프로젝트·목표 결합 | 인용된 옛 발언과 새 지시 구분, 불명확 목표에 정식 변경 없음 |
| FR-02 | 설계 revision·출처 저장 | 원문→revision→diff 역추적, 같은 턴 재전송 중복 없음 |
| FR-03 | 유효 결정/금지 유지 | 나중에 한 거부·수정 발언이 다음 초안에 반영됨 |
| FR-04 | 독립 리뷰 | 실제 model/provider/run ID, 관점, 지적, 근거·지연·비용 기록 |
| FR-05 | 리뷰 검증·교정 | 지적별 채택/기각 이유, 수정 전후 diff와 최종 답 연결 |
| FR-06 | 자연어 확정 범위 해석 | 특정 revision·patch·actor·원문 확정 이벤트에 결합 |
| FR-07 | 정식 계획 반영 | 서버 검증 후 원자적 반영, stale 승인 거절 |
| FR-08 | 업무계층 생성·변경 | 기존 work_items 활용, 유효 parent와 goal/milestone 연결 |
| FR-09 | 사용자 상태 표시 | 초안/검토중/미검토/확정대기/반영됨/충돌을 구분 |
| FR-10 | 재시도·복구 | 실패 단계만 재시도, 적용 결과 불명확하면 먼저 조회 |
| FR-11 | 기획 우선 착수 | M11 검수 전 후순위 신규 dispatch 없음, 기존 실행 결과 보존 |
| FR-12 | 관찰·회귀 | 분류·리뷰·반영·화면 결과를 correlation ID로 추적 |

## 7. 데이터·API 계약 (구현 제안)

### 7.1 정본과 재사용

- 원문: 기존 chat_messages. 원문 메시지 ID와 content hash를 참조하고 임의 ID를 만들지 않는다. 원문 삭제/권한 변경 시 출처 상태를 표시한다.
- 실행계획: 기존 goals/milestones/work_items 및 변경 승인 서비스. 새 계획 엔진을 병렬로 만들지 않는다.
- 기획 정본: 신규 planning_drafts, planning_draft_revisions, planning_review_runs, planning_review_findings, planning_decisions를 제안한다. 실제 migration 명명은 구현 시 기존 스키마와 대조한다.
- 반영 change set/승인은 기존 기능을 우선 재사용한다. 기존 업무계층 서비스가 goal/milestone 변경까지 보장하는지는 M7~M10 설계에서 확인하고 어댑터 경계를 명시한다.
- 핸드오버: project_handover_entries. 이 Markdown은 상세 기획 산출물이며 작업 수행 기록은 DB에도 남긴다.

공통 필드: tenant_id, project, goal_id, session_id, source_message_ids, created_by, created_at, correlation_id. revision은 draft_id/revision/base_goal_version/base_milestone_versions/content_hash/schema_version/requirements/constraints/open_questions를 가진다. 출처 권한 확인 전에 LLM 입력에 넣지 않는다.

리뷰는 requested_model과 actual_model/provider, viewpoint, status, findings, evidence_refs, latency_ms, usage, cost_usd(측정 불가 시 null)를 저장한다. 동일 제공사·동일 모델이면 독립 호출과 제공사 다양성을 구분해 표시한다. 리뷰어에게 승인/목표 변경 도구 권한을 주지 않는다.

확정 이벤트는 actor_session_id, source_user_message_id, draft_revision, patch_hash, target_versions, scope, policy_decision_id를 가진다. 세션의 오래된 포괄 발언이나 모델의 “승인됨” 문자열로 생성하지 않는다.

### 7.2 멱등성·원자성

- 초안 키: tenant/project/session/goal/source_user_message_id/generation_kind. 같은 키·같은 입력은 기존 결과, 같은 키·다른 hash는 409.
- 변경 키: tenant/project/decision_id/patch_hash. unique 제약으로 이중 반영 차단.
- 트랜잭션에서 대상 버전 확인 및 잠금 → 변경 → revision/audit/outbox 기록 → commit. 하나라도 실패하면 정식 변경 전체 rollback.
- 승인 후 설계 내용/목표 기준 버전/권한이 변하면 자동 재해석하지 않고 새 diff를 생성한다.
- 되돌리기는 과거 audit 삭제가 아닌 새 보상 change set이다. 후속 업무·실행 작업의 영향 분석을 표시하고 코드/배포 복원과 구분한다.
- 비동기 작업은 DB lease(owner_instance/owner_epoch)와 fencing을 적용한다. 단일 로컬 mutex만으로 서버 재시작·양 슬롯 중복을 막았다고 주장하지 않는다.

### 7.3 API 제안

| Method | 경로 제안 | 계약 |
|---|---|---|
| POST | /api/v1/chat/sessions/{session_id}/planning-drafts | 원문 참조·멱등키, 201 또는 기존 결과 |
| GET | /api/v1/chat/sessions/{session_id}/planning-drafts | 권한 내 초안·현재 상태·revision 목록 |
| GET | /api/v1/planning-drafts/{id}/revisions/{revision} | 원문 출처·설계·리뷰·diff 참조 |
| POST | /api/v1/planning-drafts/{id}/reviews | 특정 revision 리뷰 생성·상태 반환 |
| POST | /api/v1/planning-drafts/{id}/change-preview | 대상 버전·검증된 diff·정책 결과 |
| POST | /api/v1/planning-drafts/{id}/confirm | CEO 확정 근거·revision·hash 검증 |
| POST | /api/v1/planning-drafts/{id}/apply | 기존 승인 실행 서비스 경유, 멱등 반영 |

오류는 인증 401, 권한 부족 403(타 tenant 자원 존재는 404), 대상 없음 404, 버전/키 충돌 409, 불명확 확정/검토 미완/잘못된 계층 422로 구분한다. 사용자 응답에는 다음 행동과 request/correlation ID를 제공하고 시크릿·내부 스택을 노출하지 않는다.

## 8. 업무계층·담당·마일스톤 실행계획

역할(`owner_role_key`)은 누가 맡는지를 뜻하고 계층(`work_items.type/parent_id`)은 어떤 일에 속하는지를 뜻한다. **owner_role_key 등록만으로 Epic/Story/Task 등록 완료가 아니다.** 기존 활성 assignment와 독립 reviewer를 연결하고, 없는 역할/세션을 임의 생성하거나 타 목표 담당으로 대체하지 않는다.

| 마일스톤 | Epic → Story 묶음 | Task·증거 예시 | 담당/검수 |
|---|---|---|---|
| M7 | 대화 설계 정본 → 맥락 결합, revision 저장, 조회 | 데이터 계약, migration 파일, 서비스/API, 격리·멱등 테스트 | Developer / PM+독립 QA |
| M8 | 응답 사전 검토 → 관점별 리뷰, 장애 복구 | 라우팅·타임아웃·실사용 모델 기록, 실패 주입 테스트 | Developer / 독립 QA |
| M9 | 권장안 교정 → 지적 판정, 채팅 표시 | 교정 이력, 펼쳐보기·재시도, 화면 증거 | Developer / PM+독립 QA |
| M10 | 확정 계획 반영 → 결정 결합, change set, 계층 반영 | 버전·멱등·원자성·권한·복구 시험 | GoalSystemAdmin 조율, Developer 구현 / 독립 QA |
| M11 | 기획 흐름 인수 → 통합 재현, 운영 검수 | 채팅·목표 화면/API/DB 일치, 지시서 없는 완료 증거 | PM 수락 조율 / 독립 QA·Ops |

각 Story에는 사용자 가치와 Given/When/Then 인수기준을 둔다. Task에는 허용 파일·테스트·출력 증거·차단조건을 둔다. 등록 후 SELECT/API로 parent_id, milestone_id, assignment_id, 검수자와 의존 edge를 확인해야 업무계층 등록 완료다. 담당과 검수자가 같으면 수락하지 않는다.

기획 구현 의존: M7 → M8 → M9 → M10 → M11. M7 계약이 고정되면 M8 테스트 준비와 M9 화면 명세 작성은 별도 파일에서 병렬 가능하다. 동일 파일 구현은 depends_on으로 직렬화한다. 후순위 제품은 M11 뒤 M1/M2/M3/M4/M6을 진행하고 M5는 계속 보류한다. 표시 순서는 기존 M7~M11→M1~M6을 보존한다.

현재 M1 running 및 목표 blocked는 상태 사실이다. PRD 저장만으로 목표 상태를 active로 바꾸거나 M1을 completed로 만들지 않는다. 첫 M7 작업은 목표 전체 자동 진행을 켜지 않고 명시적인 milestone 연결 Runner로 수행할 수 있다. 이후 단계 제어 보완은 현재 goal_manager dirty 소유권·활성 Runner 의존성을 확인한 별도 작업으로 처리한다. M4의 실패를 성공으로 고쳐 기획 차단을 푸는 방식은 금지한다.

### 8.1 바로 다음 구현 단위: M7a

M7을 한 번에 전 채팅에 연결하지 않고 다음 범위로 시작한다.

1. 현재 PRD를 격리 worktree에 복사·검증하고 정상 commit hook을 통과시킨다.
2. 기존 초안/업무계층 패턴을 검토하여 기획 정본 schema와 migration 파일을 작성한다. 운영 DB에는 적용하지 않는다.
3. 인증·tenant/project/session/goal 연결 검증, revision 저장/조회, 멱등키 및 CAS 충돌을 구현한다. 새 라우터와 서비스가 중심이며 기존 공유 파일은 충돌 확인 후 최소 연결만 한다.
4. 격리 테스트 DB에서 API 통합·트랜잭션·권한 테스트를 실행한다. 테스트 DB가 없으면 mock 통과와 실제 DB 미검증을 구분한다.
5. M7b에서 채팅 자동 연결·기존 화면 통합을 수행할 수 있도록 계약과 남은 작업을 기록한다. M7a만으로 M7 전체를 완료 처리하지 않는다.

## 9. UX·장애·성능 설계

- 정상 응답은 권장안과 결정 근거를 우선 표시한다. 리뷰 상태와 설계 버전은 보조 표시하고 검토 원문은 기본 접는다.
- 리뷰 중에는 진행 상태를 먼저 SSE로 보낸다. 아직 교정되지 않은 텍스트를 검토 완료된 최종안처럼 스트리밍하지 않는다. 중단되면 저장된 초안과 실패 단계를 복구한다.
- 목표 패널은 현재 설계, 확정 범위, 적용/대기 상태, 마지막 오류, 재시도 버튼을 같은 맥락으로 표시한다. 기술 식별자는 상세 증거에서만 표시한다.
- 세션 전환·새로고침·모바일 재실행 시 서버 revision을 재조회한다. stale 클라이언트 캐시로 이전 목표에 반영하지 않는다.
- 로그인 만료는 재로그인 후 같은 초안 복귀, 권한 부족은 읽기 가능한 범위와 담당 요청, 네트워크 실패는 저장 결과 조회 후 재시도로 연결한다.
- 리뷰 무응답은 제한된 재시도 후 degraded. 모든 LLM 호출은 프로젝트의 기존 중앙 fallback 경로와 LiteLLM 정책을 따른다. ANTHROPIC_API_KEY 직접 사용·자격증명 파일 변경은 범위 밖이다.
- 배포된 모델의 실지원 여부를 확인하며 제품명만으로 호출 성공을 단정하지 않는다. 동일 모델 독립 호출은 다중 제공사 검증으로 보고하지 않는다.
- 타임아웃·입력 상한·동시 호출수·예산은 설정으로 관리한다. 초기 기본값은 staging 실측 뒤 확정한다. 모델 비용 미수집은 0달러가 아니라 미측정이다.
- 측정: draft 저장/리뷰/교정/apply 별 p50·p95, review_degraded 비율, 잘못된 의도 판정, CEO 재교정 횟수, 중복 차단, 충돌/복구 건수, 실제 토큰·비용. baseline 없는 개선율은 발표하지 않는다.

## 10. 수락·회귀 테스트

아래 숫자는 관찰 결과가 아닌 목표 기준이다. 운영 성능 목표는 baseline 측정 후 확정한다.

| ID | 입력/실패 시나리오 | 기대 결과 |
|---|---|---|
| AC-01 | “장단점 보고해” | 대안·리뷰만 갱신, 정식 목표 변경 0건 |
| AC-02 | “대화하면서 작동이 무슨 의미냐” | 구현 현황과 제안 분리, 임의 착수 없음 |
| AC-03 | 이번 대화의 “목표에 반영되게 해봐” | 특정 설계 revision/diff에 결합된 변경 1회 |
| AC-04 | 동일 메시지/승인 재전송 | 초안·변경·outbox 중복 생성 없음 |
| AC-05 | 동일 멱등키에 다른 입력 | 409, 기존 결과 보존 |
| AC-06 | 다른 tenant/project/session 목표 ID 주입 | 조회/쓰기 거절, 정보 누출 없음 |
| AC-07 | 두 세션의 동시 수정 | 하나만 적용, 다른 요청 409, 덮어쓰기 없음 |
| AC-08 | 리뷰가 보고 길이 개선으로 의도 오독 | 기획 방향 검토 목적을 복원하고 기각 근거 기록 |
| AC-09 | 리뷰 timeout/모델 불일치/단일 제공사 | 미검토/다양성 한계를 표시, 정상 승인으로 포장하지 않음 |
| AC-10 | 승인 후 초안/대상 버전 변경 | stale 적용 차단, 새 diff 요청 |
| AC-11 | 적용 중 DB/audit 실패 | 정식 계획 부분 반영 없음 |
| AC-12 | commit 뒤 응답 유실 | 키로 기존 결과 조회, 재적용 없음 |
| AC-13 | SSE 중단·세션 복귀·로그인 만료 | 서버 저장 상태 복구, 사용자 다음 행동 제공 |
| AC-14 | “지시서는 마지막” | M11 전 지시서 개선 신규 dispatch 없음, 기존 실행 보존 |
| AC-15 | 역할만 등록, 하위 업무 없음 | 업무계층 완료로 판정하지 않음 |
| AC-16 | 잘못된 parent/순환 dependency/자기 검수 | 저장/수락 차단 |
| AC-17 | 이전 revision으로 복구 | 새 보상 이력, 후속 영향 표시, audit 보존 |
| AC-18 | 리뷰어가 근거 없는 주장을 추가 | 검증되지 않은 지적은 미해결/기각, 확정 사실로 보고하지 않음 |
| AC-19 | 사용자가 새 금지·부분 승인 입력 | 해당 범위 밖 변경 없음, 이전 제안 무효화 |
| AC-20 | 기획 플래그 비활성 | 기존 채팅·목표 경로 회귀 없음, 데이터 보존 |
| AC-21 | 양 슬롯/worker 재시도 | fenced owner만 적용, 중복 실행 없음 |
| AC-22 | 채팅·목표 화면과 DB 비교 | 같은 revision/순서/계층, 데스크톱·모바일 캡처 증거 |

실행 방법: 구현된 테스트 파일의 실제 pytest 명령과 결과를 Runner가 기록한다. 제안 명령은 `pytest -q tests/unit/test_planning_draft_service.py tests/integration/test_planning_drafts_api.py`이며 파일 생성 전 통과했다고 보고하지 않는다. M9/M11 화면 대상 route는 기존 채팅/목표 URL을 조사해 확정한다. 로그인 사용 여부, Playwright/capture 도구, 캡처 결과를 기록한다. 브라우저 실패 시 HTTP→API health→프로세스 순 폴백을 수행하고 UI 검수 미완 상태를 분리한다.

## 11. 출시·중단·롤백

설계·계약 → M7 격리 구현 → M8/M9 → M10 반영 → M11 검수 → 기획 기능 운영 활성화 → 지시서 후순위 순서다. 초기에는 계획 초안 저장/리뷰/apply 각각의 기능 플래그를 분리하고 기본 비활성으로 배포한다. 감사 모드에서는 정식 목표 변경을 하지 않는다.

배포가 승인된 단계에서는 저장소 release 규칙을 적용한다: clean committed worktree, SHA당 한 이미지 빌드, candidate/standby --no-build, candidate health 후 nginx lock, DB 실행 ownership fencing, 라우팅 health 실패 즉시 rollback, standby 동일 digest, 최소 5분 P0/P1 관찰. 앱 변경으로 full compose stack 배포나 active API 직접 재시작을 하지 않는다.

tenant 누출, 미확정 정식 변경, 중복 적용, stale 승인 통과, audit 누락, 금지 범위 신규 실행 중 하나라도 발생하면 apply를 중지한다. 플래그를 끄고 기존 경로를 유지하며 관련 diff와 audit를 보존한다. 스키마 rollback은 파괴적 DROP 대신 호환 코드/기능 비활성 및 검토된 보상 변경을 사용한다.

## 12. 리스크·대안·남은 결정

| 쟁점 | 권장안 | 대안/비권장 사유 |
|---|---|---|
| 대화의 자연스러움 vs 승인 부담 | 초안은 자동, 명확히 확정한 범위만 정식 반영 | 매 턴 승인 요구는 부담, 모든 발언 자동 반영은 의도 오독 위험 |
| 리뷰 지연·비용 | 기획 턴만 실행, 관점별 독립 호출·변경 부분 중심 검토 | 모든 인사/조회에 리뷰 강제는 비용·지연 증가 |
| 리뷰 편향 | 실제 모델 기록, 근거 기반 지적 검증 | 다수결 점수만으로 승인하면 같은 오류 강화 |
| 기존 엔진 중복 | work_items/승인 경로 재사용 | 별도 목표 엔진은 정본 분열 위험 |
| 현재 dirty·Runner 충돌 | 신규 문서 1개와 격리 worktree 중심 M7a | 공유 goal_manager 직접 수정은 타세션 변경 충돌 |
| 목표 blocked와 실행 순서 | M7 명시 연결·단계 gate, 전체 auto_advance 유지 | 상태만 active로 변경하면 M1 우선 재개 위험 |

구현 중 확정할 항목: 실제 API mount 지점, 기존 change set의 goal/milestone 지원 범위, 리뷰 모델 가용성·timeout·예산, 활성 QA assignment, work_items 등록 정책, M7b 화면 route. 이 미결정 항목은 M7a의 격리 계약·저장 구현을 막지 않지만 운영 활성화·M11 완료 전에는 모두 근거와 함께 닫아야 한다.

## 13. 추적·완료 보고 계약

요구사항 FR → AC → milestone → work_item → Runner job → commit → 배포 run → 화면/API/DB evidence를 연결한다. PRD 변경은 새 버전과 변경 사유를 기록하며 진행 중 Runner는 자신이 받은 PRD hash를 고정한다. 새 논의가 들어오면 진행 중 지시를 덮어쓰지 않고 후속 change set으로 연결한다.

각 작업 보고에는 실제 변경 파일, 검증 명령/결과, 테스트 DB 사용 여부, commit/push/배포 상태, 미완료 조건, DB 핸드오버 entry key, 비용(측정 불가 시 미측정)을 남긴다. 문서 저장·작업 제출만으로 구현 완료를 선언하지 않는다.

관련 문서: [업무계층·승인 PRD](20260919_GOAL_WORK_HIERARCHY_APPROVAL_PRD.md), [기존 지시 코파일럿 PRD](../plans/20260910_OHVIS_DIRECTIVE_COPILOT_PRD.md). 관련 문서의 구현·운영 승인 상태를 그대로 본 목표에 전이하지 않는다.
