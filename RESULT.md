# 오비서 정지 소속 세션 재로그인 복구 결과

## FOOD 인사 M1 기존 목표 연결 보존 교정 결과 (runner-401e7e23)

- document_key: food-hr-goal-link-rework-result. source_task_id: runner-401e7e23. change_summary: 기존 링크 덮어쓰기와 되돌림의 작업 연결 손실을 방지하는 교정 결과 기록.
- 상태: 코드 수정 및 단위 검증 완료, 승인 대기. 정본 미등록(미완료), 열람 링크 미제공. 기존 RESULT에 결과만 추가하는 이유는 요청된 STEP 0·검증·미검증 기록을 보존하기 위함이며, HANDOVER.md는 변경하지 않음.
- 정확한 기준 SHA: ea0215f549f3e0b5743f6323360d4e309574b67e. 보존한 선행 구현 SHA: 16f92c34462737c2be7e87731c0d46f5d95e09bd. 새 커밋 없음. 시작 시 현재 격리 작업트리 clean. 선행 커밋의 코드·테스트 8개 파일 diff만 적용했으며 과거 보고서와 다른 공유 작업트리/static은 변경하지 않음.
- 변경 범위 사유: AAG는 관련분만 제시했고 현 HEAD에는 backfill·공통 정책 모듈이 없었음. 지시서가 직접 지정한 선행 커밋에서 실제 호출경로(제출/활동 자동연결 → 명시 연결 정책, 재조정, 일반 backfill)를 확인한 뒤 해당 구현을 복원함. AADS는 호스팅 프로젝트이고 FOOD는 업무 목표 프로젝트인 방향성 정책, ACCT 호환, tenant/milestone 검사와 미배포 done 승격 금지는 유지.
- STEP 0 유지: goal_binding 파서, 기존 제출·활동·목표 조회 테스트/fixture, 재조정 plan_actions의 릴리스 증거 판정, 일반 backfill plan_link의 tenant/상태 검사. HTTP mount/엔드포인트/스케줄러 신규 없음.
- STEP 0 수정: pipeline_runner_service._link_job_to_goal_explicit, goal_link_reconciler.plan_actions, backfill_job_goal_links.plan_link는 선행 정책 공유 변경을 보존. test_goal_link_integrity.py와 test_goal_autolink_activity.py에는 선행 FOOD 검증 사례를 보존하고 import 순서를 정리함.
- STEP 0 신규(현 HEAD 대비)/수정(선행 구현 대비): app/services/goal_link_policy.py의 is_goal_project_allowed 및 scripts/backfill_food_hr_m1_links.py의 plan_target, _snapshot, _print_snapshot, run, main, _DryRunRollback과 SQL 상수. 새 owned_link는 insert-only v2 출처만 인정. tests/unit/test_food_hr_m1_backfill.py의 _load/_job/_goal/_ms/_plan/_Tx/_Conn/conn fixture와 기존 dry-run/멱등/되돌림/불변성 테스트를 유지·교정하고 회귀 사례 추가. 삭제한 함수/파일 없음. 중복 프로젝트 정책 상수는 선행 커밋대로 공통 함수로 통합하며 호출부 영향은 위 3경로, 코드 롤백은 이 변경분만 역적용하는 방식(운영 데이터 변경 없음).
- DB 접점: pipeline_jobs의 목표·milestone 연결, goal_task_links의 INSERT/제한된 detach, goals/milestones의 SELECT·전후 전체 행 비교. project_document_heads와 project_handover_entries는 요청된 메타데이터 기록 대상이나 이번 환경에서 연결 미확인.
- 교정: 기존 링크는 상태(active/detached/completed), 다른 milestone/goal/tenant 및 release 증거와 무관하게 보존하고 skip. 직접 만든 v2 활성 연결만 멱등 noop. UPSERT를 ON CONFLICT DO NOTHING으로 변경하고 INSERT 0 0을 실패 처리하여 선행 job UPDATE까지 트랜잭션 롤백. job의 원래 milestone만 있는 경우도 보존. v1 표지는 기존 링크 덮어쓰기 이력이 가능하므로 revert 소유권 증거로 인정하지 않음.
- 안전한 revert: v2 job-created만 작업 목표 연결을 되돌리고 v2 link-only는 원래 작업 연결 유지. tenant/milestone/bind_source/active/증거 없음 조건과 UPDATE 1 검증을 적용. 되돌린 detached 링크는 재활성화하지 않고 반복 apply/revert에도 보존. 기존 링크는 전체 컬럼 SELECT와 전후 비교로 추가 보호.
- 실제 검증: JWT_SECRET_KEY에 테스트 전용 임시값을 준 저장소 venv Python으로 tests/unit/test_food_hr_m1_backfill.py, tests/unit/test_goal_link_integrity.py, tests/unit/test_goal_autolink_activity.py, tests/unit/test_runner_deploy_directive_contract.py, tests/unit/test_pipeline_runner_deploy_directive_gate.py 실행 → 325 passed in 12.05s. 기존 링크 모든 필드 비교, 경쟁 INSERT 0, 작업 UPDATE 0, detach/revert UPDATE 0 롤백, 증거 있는 링크, tenant/goal/milestone 충돌, 원래 연결 보존, 반복 apply/revert 포함.
- 초기 실패도 기록: 기본 Python은 fastapi 미설치로 관련 테스트 실패; 저장소 venv의 초기 실행은 JWT_SECRET_KEY 부재로 9개 실패. 테스트 임시 설정 후 전체 지정 테스트 통과. 프로덕션 인증 설정은 변경하지 않음.
- Ruff: 신규 3개 파일 전체 검사 통과. 기존 5개 파일 전체 검사는 기준 HEAD부터 존재한 진단 135건으로 실패(통과로 보고하지 않음); 코드/메시지별 baseline 대조에서 신규 진단 0건. 해당 파일 E9/F63/F7/F82 검사는 통과. git diff --check 통과. 빌드 항목: 승인 후 Runner 빌드 검증 대상. 금지 명령 실행 없음.
- PostgreSQL 미검증: 로컬 postgres/initdb/pg_ctl/psql 실행파일 및 격리 테스트 DSN 없음. 기본 Python에는 asyncpg가 없으나 저장소 venv에는 있음. PG 환경으로 connect_timeout=5 읽기 전용 psycopg2 연결을 시도했으나 상세 메시지 없는 OperationalError로 연결 실패하여 SELECT도 실행되지 않음. 운영 DB 쓰기 없음, PostgreSQL 통합·운영 dry-run을 통과로 보고하지 않음. 단위 경쟁 INSERT 테스트는 mock 반환 검증이므로 PostgreSQL 동시성 실증이 아님.
- 현재 DB 기반 목표 링크 누락/행수, 동일 backfill 활성 runner 수와 ledger reconciled_clean 재확인은 연결 실패로 미확인. 과거 사전조회 결과를 현재 사실로 재사용하지 않음. 실제 연결 후속 조치는 이번 범위에서 수행하지 않음. localStorage 대비 DB 행수·인증 weekly·캐시삭제 유지 등 운영 완료기준 미검증.
- 문서·핸드오버: 정본 새 판본 초안 등록 및 entry_key=runner:runner-401e7e23 upsert/재조회 미수행. DB 연결 실패, API URL/쓰기 키/monitor 키 미설정, 전용 연결 도구 없음. 핸드오버 DB 미기록; handover_db_write_failed DB 이벤트도 미기록. 운영 완료나 문서 등록 완료로 보고하지 않음.
- 비용: 실측 비용 자료 미제공으로 금액 산정 불가. 외부 LLM/API 호출 없음.


## 폐기 서버 운영 후속 검증 결과 (runner-42316eb0)

- 상태: 저장소 수정·검증 완료, 운영 반영 및 인증 미완료. 금지 명령, lease 전송, 시크릿 접근, 폐기 서버 접속은 수행하지 않았다.
- 변경 전 범위 판단: 명시된 `tests/unit/test_jinah244_tcp_probe.py`만 코드 변경. 기존 RESULT에는 이 결과만 추가하며 이전 기록은 보존한다. 새 문서 및 HANDOVER.md 변경 없음.
- STEP 0 분류: 기존 TCP 성공/거부/타임아웃 테스트, 인프라 상태·활성 SSH 보존 테스트, AlertManager 기존 경보 보존 테스트, lease 기본 대상 테스트는 유지. 배포 대상 두 모듈 소스의 폐기 프로브·경보 키/IP 부재 검사는 신규(2개 매개변수 사례). `test_alert_path.py`의 알림 전달·중복 억제·keeper 테스트 및 helper는 유지. 삭제 없음.
- STEP 0 운영 접점: `health_checker.check_infra`, `_check_tcp_reachable`, `AlertManager.evaluate_rules`, 두 lease 스크립트와 service는 유지(선행 정리 이미 포함). HTTP 라우터/mount, 스케줄러, DB 접점 신규·변경 없음. deploy_runs/ledger/handover DB 실측은 미완료.
- 기준 SHA: HEAD `d074cb0d`. `git merge-base --is-ancestor`로 `53e89b49`, `4772ebc861de`, 선행 `d074cb0d` 포함을 각각 exit 0으로 확인. 최초 작업 트리는 clean. origin 최신 상태 및 원격 실행 소유권/동일 파일 충돌은 미확인.
- 운영 기준 대비 관련 diff 확인: 폐기 인프라 체크 및 전용 경보 삭제, lease 기본 대상에서 폐기 IP 제거. 활성 두 호스트, service Nice=5 및 두 ExecStart는 저장소에서 보존. 전체 차이는 110개 파일이므로 ancestry만으로 전체 운영 기능 보존이나 릴리스 적합성을 보증하지 않는다. ACCT 라우팅/DB 목적지는 미확인.
- 실제 검증: `/root/aads/aads-server/.venv/bin/python -m pytest -q tests/unit/test_jinah244_tcp_probe.py tests/unit/test_alert_path.py tests/unit/test_jinah244_no_new_use.py` → 38 passed in 8.17s.
- 실제 검증: `bash -n scripts/claude-lease-push.sh scripts/slot_lease_push.sh deploy.sh` 및 `git diff --check` → exit 0. 두 모듈 소스에서 폐기 프로브·경보 키/IP 부재 검사 통과. 실행 중 컨테이너 코드 확인 결과는 아님.
- 빌드 검증: 승인 후 Runner 빌드 검증 대상. commit/push/build/deploy 실행 없음, 새 배포 ID 없음.
- 미완료: 활성 runner/deploy_runs/ledger와 중복 릴리스 여부, 공유 호스트 파일 dirty/소유권·백업, 설치 service 반영 및 effective Environment, 외부 HTTP 200, 두 슬롯 digest, routed health/rollback 및 5분 P0/P1·폐기 경보 재발 관찰. 운영 변경이 없어 이번 작업의 운영 롤백은 해당 없음. 코드 복원은 신규 테스트 함수만 제거하면 됨.
- 핸드오버 DB 미기록: 사용 가능한 연결 도구와 확인된 tenant 문맥이 없고 시크릿 접근 금지 범위에서 쓰기·재조회하지 못함. 요청 고정 key는 `runner:runner-42316eb0`, project AADS, entry_type verification. DB 실패 이벤트 기록도 미수행. 정본 미등록(미완료), 열람 링크 미제공.

document_key: obys-stale-tenant-session-relogin-result (정본 미등록 — 등록 도구 없음, 열람 링크 미제공)

## STEP 0 기존 구현 분류
| 대상 | 분류 |
|---|---|
| `app/auth.py::_load_tenant_context` (membership 없음 → 403) | 수정 |
| `app/auth.py::get_current_user`, `require_tenant_role`, `require_internal_admin` (역할 403) | 유지 |
| `app/auth.py::switch_user_tenant` (타 조직 전환 403) | 유지 (X-Tenant-ID 와 같은 "명시 요청" 경로) |
| `app/auth.py::resolve_login_tenant_for_user` | 수정 (정지된 조직을 기본 조직으로 고르지 않음) |
| `index.html` financeApi / authApi / workspaceApi | 수정 (공통 `buildApiError`) |
| `index.html` handleExpiredServerSession 및 개별 401 분기 | 유지 |
| `index.html` handleTenantMembershipInactive, isTenantMembershipInactive, buildApiError, `#authGateNotice` | 신규 |
| 삭제 | 없음 |

## 선택한 방식: 401 + 코드
- 원천 403 은 `_load_tenant_context` 의 `Tenant membership required`(auth.py 1701행 부근) 하나였다. `Tenant access denied`(admin 계정이 internal 외 조직 요청)는 세션 무효가 아니므로 403 유지.
- 토큰의 조직(또는 기본 조직)에 active membership/조직이 없으면 `401` + `detail="회사 소속이 바뀌어 다시 로그인이 필요합니다. (tenant_membership_inactive)"` + 헤더 `X-Auth-Error-Code: tenant_membership_inactive`.
- `X-Tenant-ID` 로 토큰과 다른 조직을 명시 요청했는데 소속이 없는 경우, 역할 부족(`... role required`), internal admin 부족은 그대로 403. 헤더도 붙이지 않는다.
- detail 은 문자열 유지(dict 로 바꾸면 기존 클라이언트가 `[object Object]` 를 보여줌). body 에 `code` 키는 없고 헤더+detail 문구로 구분한다.
- 영향 범위: `get_current_user` 를 쓰는 모든 라우트(AADS 대시보드 포함)에서, 소속이 끊긴 토큰이 403 대신 401 을 받는다. 대시보드가 401 에 로그아웃/재로그인으로 보내는 것이 이 상황의 올바른 동작이라 판단했다. 대시보드 저장소는 이 워크트리에 없어 직접 확인하지 못했다 — 영향이 문제되면 `_load_tenant_context` 의 status_code 만 403 으로 되돌려도 프런트는 같은 헤더로 동작한다.
- 프런트는 상태코드가 아니라 코드로 판단하므로 서버가 롤백되어도(403+코드) 동작한다.

## 화면 흐름
1. 정지된 소속의 JWT 로 진입 → financeApi(`/session` 등)가 401+코드 수신.
2. `handleTenantMembershipInactive()`(한 번만): 토큰·쿠키 삭제, authSession 비움, 로그인 게이트 표시, 게이트에 상시 안내 "회사 소속이 바뀌어 다시 로그인이 필요합니다. 다시 로그인해 주십시오." + 토스트.
3. 주소를 건드리지 않으므로 `yf_contract_token` 쿼리가 남는다. 재로그인(토큰 저장 시 플래그·안내 초기화) 후 기존 `handleContractLinkFromUrl` 흐름으로 서명 화면이 열린다.
4. 재로그인 시 `resolve_login_tenant_for_user` 가 정지된 조직을 거르고 active 소속(예: 15055cac)을 고른다.

## 검증
- `bash scripts/run_unit_tests.sh` (신규 테스트 + 관련 8개 파일): 275건 중 272 PASS, 3 FAIL.
  - 실패 3건은 이번 변경과 무관한 기존 소스 검사로 보인다: `test_tenant_rbac_policy::test_memory_context_route_and_service_are_tenant_scoped`, `test_obys_employee_tenant_membership::test_gate_open_paths_resolve_only_to_signing_handlers`, `::test_signing_lookups_are_bound_to_jwt_tenant`. (`chat memory-context`, `signing-view` 라우트·`get_contract_by_token` 소스를 검사하며 내가 건드린 코드가 아님. HEAD 에서의 실패 여부는 git 명령 제한으로 직접 대조하지 못했다.)
  - 신규 `tests/unit/test_obys_stale_tenant_session_relogin.py` 9건 전부 PASS (suspended→401/코드, active→200, role 부족→403, 타 조직 헤더→403, 로그인 기본조직 정지 건너뜀, 프런트 정적 검사).
- `ruff check --select F821,F811` 통과. 인라인 JS 2블록 `node --check` 통과.
- 실제 브라우저 E2E(운영 JWT 재현)는 하지 않았다 — 승인 후 Runner 검증 대상.

## 롤백
커밋 revert 한 번. 운영 DB 변경 없음.


---

## 채팅 정본 승인 카드 서버 재작업 결과

document_key: canonical-chat-approval-api-rework-result
정본 미등록(미완료), 열람 링크 미제공 — DB 접근 PermissionError. 기존 RESULT 기록은 보존했다.
작업: runner-3327ee51 / AADS-CANONICAL-CHAT-APPROVAL-API.
기준 HEAD 및 로컬 origin/main: 47b0d7183e26f75607c8fd8647ad9d051c7f6c30.
이전 반려 커밋 cae5887e3945e4bcb2874b1dfd5d5137eb3e5c10의 변경 7개 파일을 git show로 복원하여 이어서 수정했다.
초기 git status는 clean이었다. 원격 최신 여부는 fetch 없이 확인하지 못했다.
활성 러너/pipeline_jobs 및 chat_workspace_change_ledger 조회는 timeout 20 안에서 시도했으나 DB 연결 PermissionError로 확인하지 못했다.

### STEP 0 기존 구현 분류

| 접점 | 분류 및 처리 |
| --- | --- |
| canonical_documents._scope, _authorize | 유지: user_id → id 순서의 신원 해석과 tenant/project 권한 SQL 재사용 |
| canonical_documents.get_document | 수정: 최신 revision 카드와 실제 can_approve 계산 |
| canonical_documents.approve_document | 수정: 같은 _scope 신원, 인증 방식 및 active membership 검증 |
| canonical_documents._require_human_approver | 신규: 사람 세션·소속 검사 공통 함수 |
| list_documents/get_document_content/create_revision_in_tx/approved_brief 및 archive | 유지 |
| project_document_heads/revisions/events/grants | 유지: 스키마 추가 없음, 승인 트랜잭션·감사 actor·멱등 재사용 |
| canonical_document_tools._document_summary/lookup_documents | 수정: 최신 판본 승인 카드 생성; 기존 SELECT 한도 20 재사용 |
| canonical_document_tools.approval_card/validated_approval_cards | 신규: 카드 계약 생성 및 저장·재조회 검증 |
| chat_service._tool_result_event_snapshot/normalize_tool_events | 수정: content와 별도로 검증된 카드 저장·재조회 |
| model_selector의 네 tool_result 변환 경로 | 수정: 도구 이름 기반 공통 한도, CLI tool id 이름 복원 선행 |
| model_selector._tool_result_content | 신규: 일반 3000자, 정본 조회 32000자 및 큰 결과의 완전한 카드 JSON 보존 |
| auth.get_current_user | 수정: 검증된 로그인 JWT 경로에 auth_type=human_session 부여 |
| 스케줄러/일반 agent_permission_requests 승인 | 유지: 연결·변경 없음 |
| 삭제 | 없음 |

원 지시의 명시 파일 외 app/auth.py 변경 사유: system: 문자열만으로 사람을 판별하지 않도록 인증 경계에서 세션 유형을 부여해야 한다. 변경 전에 commentary로 설명했다. 테스트 3개 파일은 API·도구·저장 경로 회귀 검증을 위해 보완했다. RESULT.md는 요구된 검증·계약 인계를 위해 기존 내용을 보존하여 추가했다. ceo_chat.py의 기존 orphan router는 이번 범위와 무관하여 변경하지 않았다.

### 리뷰 지적별 처리

1. _require_human_approver도 _scope를 사용한다. user_id만 존재하는 경우와 user_id/id가 다른 경우에 _authorize의 grant actor와 동일함을 테스트했다.
2. membership 누락, 빈 객체, status 누락, removed를 모두 403으로 거절한다. active 기본값을 제거했다.
3. 검증된 로그인 JWT 경로에서만 auth_type=human_session을 생성한다. monitor/service/pipeline/handover 경로에는 이 표식이 없다. 접두사 없는 서비스 신원도 auth_type 검사로 거절하며 system: 방어도 유지했다. 요청 본문/모델 출력의 auth_type은 사용하지 않는다.
4. 읽기 권한 카드에 can_approve=false를 제공한다. GET은 동일한 사람·소속·project approve 권한을 확인한 경우에만 true를 준다. 도구/저장 카드는 항상 false이며, 버튼 활성화 전 인증 GET 재조회가 필수다. tenant_id와 revision/generation은 읽기 허용된 문서 메타데이터이며 승인 자격이 아니다.
5. 기존 _document_summary의 revision 내부에서 data['status']=event or draft를 이미 설정한다. 추가로 approval_card 자체도 status 누락/None을 draft로 처리하고 테스트한다.
6. 문자열 포함 검사를 제거하고 네 경로가 _tool_result_content를 사용한다. CLI는 실제 tool id→name 매핑을 넘겨 절단 전에 이름을 복원한다. 외부 결과에 approval_card 문자열이 있어도 3000자다.
7. 저장 및 재생 모두 UUID, project/key, 정수 범위, 상태, 제목/판본 길이, 필드·URL·approval_body 일치와 can_approve=false를 검증하고 서버 생성 함수로 재구성한다. 임의 외부 endpoint/본문/추가 필드/권한 상승은 버린다. 이것은 서명이 아닌 스키마 검증이며, 저장 데이터는 현재 승인 권한이나 현재 판본의 증거로 사용하지 않는다. 실제 액션 전 GET과 POST 인증·DB 권한 검증이 최종 판단이다.
8. 개수 20은 기존 lookup SQL _LOOKUP_LIMIT와 동일하게 맞췄다. 카드당 UTF-8 4096 bytes, 이벤트 카드 합계 24576 bytes 한도를 코드 주석과 여기 기록했다. 일반 content_limit과 독립적으로 적용한다. 큰 정상 조회는 32000자 전송 한도 안에 완전한 카드 JSON을 재구성한다.

### 대시보드 인계 계약

GET /api/v1/projects/{project}/documents/{document_key} : 승인 대상 최신 revision과 approval_card 조회.
GET /api/v1/projects/{project}/documents/{document_key}/content?revision={revision} : 그 판본의 DB 본문 열람.
POST /api/v1/projects/{project}/documents/{document_key}/approve : 기존 승인 API만 사용.
POST JSON: {"revision_id":"<GET 최신 revision UUID>","expected_generation":<GET generation 정수>}.
인증: 사람 로그인 Bearer JWT 또는 기존 로그인 쿠키, 현재 tenant 컨텍스트(X-Tenant-ID 사용 시 해당 tenant의 active membership), tenant member 이상 및 project approve grant 또는 기존 elevated 정책. 모델·runner·서비스 키로 승인을 대행하지 않는다.
카드: type=canonical_document_approval, schema_version=1, tenant_id, project, document_key, title(원본 한글 제목), revision_id, revision, version, generation, status, approved, view, content_endpoint, refresh_endpoint, approval_endpoint, approval_body, auth, can_approve.
도구 결과 단건 approval_card 또는 documents[].approval_card → tool event canonical_document_cards[] → 같은 필드로 저장·재생된다. 모델의 자유 문자열은 승인 액션으로 실행하지 않는다.
도구·저장 카드는 can_approve=false로 시작한다. 사람 로그인으로 refresh_endpoint를 조회해 tenant 일치와 can_approve=true를 확인한 뒤 최신 제목·판본·내용을 다시 보여 주고 명시적 승인 클릭으로 POST한다.
409: GET 갱신 → 바뀐 판본/내용을 표시 → 사람 재확인. 자동 재승인 금지.
성공 또는 네트워크 실패: GET 재조회로 승인 포인터/상태를 확인. 중복 POST는 기존 멱등 동작으로 처리.
대상 key 세 개는 ohvis-ui-ux-design-profile-contract, ohvis-ui-ux-asset-audit-requirements, ohvis-ui-ux-g6-token-tenant-isolation-draft. key별 최신 GET 사용; 9/5/1 고정 없음. 운영 문서 승인 실행 없음.
인계 SHA는 위 기준 SHA와 이전 반려 SHA이며, 이번 변경의 새 SHA는 생성하지 않았다. Runner가 승인 후 생성하는 SHA로 후속 대시보드 작업을 연결해야 한다.

### 실제 검증 결과

- 최초 python3 -m pytest: fastapi/asyncpg 미설치로 수집 오류 3건(통과 아님).
- 기존 .venv 실행 1차: JWT_SECRET_KEY 미설정으로 수집 오류 2건(통과 아님).
- 테스트 전용 JWT_SECRET_KEY와 기존 /root/aads/aads-server/.venv/bin/python으로 재실행.
- tests/unit/test_canonical_documents.py, test_canonical_document_tools.py, test_chat_generated_media.py, test_model_selector_dynamic_routing.py, test_model_selector_codex_db_route.py, test_auth_tenant_invite_expiry.py: **169 passed**.
- 허용 사람 승인, 정확한 감사 actor, 중복 멱등, cross-tenant/권한 없음, removed/소속 누락/비사람 인증, stale 판본·generation, 감사 실패 롤백·재시도, 일시 조회 실패 복구, 카드 변조·크기·저장재생, CLI 도구 이름/한도를 검증했다. DB 트랜잭션 실패는 fake transaction을 통한 단위 검증이며 운영 DB 통합 검증은 하지 않았다.
- python3 -m compileall -q (변경 Python 8개): 통과.
- python3 scripts/dup_guard.py --paths (변경 Python 8개): 통과.
- .venv/bin/ruff check --select F821,F811 (변경 Python 8개): 통과.
- git diff --check: 통과.
- scripts/run_unit_tests.sh는 내부 cleanup에서 금지된 kill을 호출하므로 실행하지 않고 위 pytest를 직접 실행했다.
- 코드/테스트 수정 완료. commit/push/build/deploy 실행하지 않음. 승인 후 Runner 빌드 검증 대상.
- DB 사전 연결 PermissionError로 핸드오버 DB 미기록. entry_key=runner:runner-3327ee51의 upsert/readback 확인 미완료; Runner의 awaiting_approval 처리에 인계한다. HANDOVER.md 변경 없음.
- 롤백: 이번 8개 Python 파일의 작업 diff만 되돌리면 된다. DB 마이그레이션·운영 문서 변경 없음.


## 교차 검토 승인본 재반영 결과 (runner-c3275e55)

### STEP 0 조사 및 변경 전 분류

- 기준 HEAD 및 로컬 origin/main: 043bcf3e0193f2659447cf945b53be92a962ac47. 초기 작업 트리 clean. git fetch origin main은 Git 관리 디렉터리 읽기 전용으로 실패하여 원격 최신 여부는 확인하지 못했다.
- 유지: code_reviewer의 diff 전처리, _cross_file_moves, 보존 게이트·예외·측정·CLI 실행 및 code_reviews 저장 계약. review_code_diff의 보존 게이트 흐름을 그대로 두고 모델 선택 부분에만 승인 diff 적용.
- 수정: _get_review_models(등급 설정 및 기존 AI_REVIEW 폴백), _save_review_result(cross 메타데이터), review_code_diff(cross_first 정렬).
- 신규(승인 커밋 복원): review_config_for_size, cross_first, _job_review_context. pipeline_jobs 조회 및 runner_model_config 읽기 추가.
- 수정: canonical_documents의 list_documents/get_document/review_revision_in_tx/approve_document에 검토 상태·강제 게이트 연결. 기존 권한·사람 승인·정본 판본 처리 유지. 신규: cross_review_document 라우트.
- 수정: pipeline_runner._RunnerModelItem 검증 및 get_runner_model_config 등급 목록. canonical_document_tools.review_document의 상태·구조화 오류 반환. 나머지 함수·클래스·엔드포인트 유지.
- 신규(승인 커밋 복원): doc_cross_review의 ReviewerPick, pick_reviewers, tier_of, summarize, summary, notify_blocked, _record, execute_review, schedule, enforce. CLI relay, project_document_reviews, lease/budget 접점 및 비동기 검토 task 등록. 별도 주기 스케줄러 추가 없음.
- 신규: 승인본의 정방향/롤백 SQL 파일 및 test_doc_cross_review. 수정: 기존 canonical_documents 및 canonical_document_review_tool 테스트 fake DB. 기존 테스트 삭제 없음. scripts/run_unit_tests.sh는 유지.
- 변경 전 범위 사유: 착수 브리프에 없는 API 2개, canonical_document_tools, doc_cross_review, SQL 2개, test_doc_cross_review는 지정 승인 커밋 자체의 변경 파일이므로 동일 변경 목록 복원을 위해 포함한다. RESULT.md는 기존 내용 뒤에 이번 결과만 추가한다. 새 앱 구현이나 추가 개선은 하지 않는다.
- 삭제 없음. DB 마이그레이션 적용·운영 설정 변경 없음. 코드 롤백은 이번 작업 diff만 되돌리는 방식이며 SQL 롤백 파일은 승인본 그대로 보존한다.
- 커밋 생성 금지 지시에 따라 cherry-pick 대신 f3078d8d의 RESULT.md 제외 diff를 git apply로 적용한다. 부모 커밋 생성/검증은 수행하지 않는다.

### 실제 반영 및 검증 결과

- 변경 파일(승인본과 같은 11개): RESULT.md, app/api/canonical_documents.py, app/api/pipeline_runner.py, app/services/canonical_document_tools.py, app/services/code_reviewer.py, app/services/doc_cross_review.py, migrations/20261029_doc_cross_review.sql, migrations/rollback/20261029_doc_cross_review.down.sql, tests/unit/test_canonical_document_review_tool.py, tests/unit/test_canonical_documents.py, tests/unit/test_doc_cross_review.py.
- 승인 diff는 충돌 없이 적용됐다. 코드/테스트/SQL 10개 파일 모두 승인본과 추가·삭제 줄 또는 신규 파일 바이트가 동일함을 검증했다. 최신 main의 교차 파일 이동 보존 게이트는 수정하지 않았으며 AI_REVIEW 3분할·cross_first가 동일 review_code_diff 흐름에 공존한다.
- 요구 명령 실행: bash scripts/run_unit_tests.sh tests/unit/test_code_reviewer_cross_file_move.py tests/unit/test_doc_cross_review.py tests/unit/test_canonical_documents.py tests/unit/test_canonical_document_review_tool.py tests/unit/test_canonical_review_chat_card.py tests/unit/test_doc_tier.py → 종료코드 2. 기준 이미지 부재(no such container: aads-server/aads-server-green).
- 호스트 기존 가상환경 Python 3.12.3 사용, 테스트용 JWT_SECRET_KEY 지정. 문서 관련 5개 파일 별도 실행: 260 passed in 4.20s.
- 최초 호스트 pytest(요구 6개 + test_code_reviewer_*.py 전체)는 24개 진행 후 정지. 단일 테스트 faulthandler 진단에서도 asyncio.runners.close → run_until_complete → selectors.select에서 대기하는 상태를 확인했다. 이 실행은 통과로 처리하지 않는다. 프로세스 종료 명령은 실행하지 않았다.
- 호스트 우회 검증: 별도 Python 실행에서 asyncio.to_thread만 동일 함수를 호출하는 async wrapper로 치환한 뒤 pytest.main으로 요구 6개 + tests/unit/test_code_reviewer_*.py 전체 실행. **516 passed in 23.68s, 실패 0, 종료코드 0**. 앱/테스트 파일 변경 없이 검증했으며 스레드 실행·종료 자체는 이 결과가 검증하지 않는다. 원래 호스트 pytest 및 단일 진단 실행은 종료 완료를 확인하지 못했다.
- 변경 Python 8개: /root/aads/aads-server/.venv/bin/ruff check --select F821,F811 → All checks passed, 종료코드 0.
- 변경 Python 8개: python3 -m compileall -q → 종료코드 0. git diff --check → 종료코드 0.
- 승인본 변경 파일 집합 11개 일치 검사 및 10개 코드/테스트/SQL 승인 diff 동일성 검사 → 통과. RESULT.md의 기존 내용은 보존했다.
- 원격 최신 origin/main은 fetch 권한 제약으로 미확인. 로컬 origin/main=HEAD=043bcf3e 기준으로 반영. git cherry-pick/commit/push 실행 없음, 커밋 부모 완료 기준은 검증하지 못했다. 빌드 실행 없음: 승인 후 Runner 빌드 검증 대상.
- 운영 마이그레이션/runner_model_config 값/대시보드/compose/.env/시크릿/HANDOVER.md 변경 없음. 새 정본 문서 생성 없이 기존 RESULT.md만 추가했으며 열람 링크 미제공.

- 핸드오버 DB 미기록: 기존 runner_handover_write.write_and_verify 경로를 쓰기 위해 pipeline_jobs의 tenant/project 조회를 시도했으나 연결/종료 대기 상태로 upsert/readback 성공을 확인하지 못했다. handover_db_write_failed 이벤트를 이 결과에 기록하며, Runner의 awaiting_approval 처리에 DB 재기록을 인계한다. 쓰기 전용/monitor 자격은 현재 환경에 없고 비밀 값 조회·복사·출력은 하지 않았다.

## 문서 교차 검토 롤백 보정 결과 (AADS-DOC-CROSS-REVIEW-ROLLBACK-NO-DROP-20261029)

### STEP 0 기존 구현 분류

| 대상 | 분류 및 처리 |
|---|---|
| `20261029_doc_cross_review.down.sql` 트랜잭션 | 유지: BEGIN/COMMIT |
| rollback backup 테이블 생성 및 세 행 백업 | 유지: CREATE TABLE IF NOT EXISTS와 INSERT 보존 |
| 세 교차 검토 설정 행 DELETE | 유지: 요청된 백업 후 제거 순서 보존 |
| runner_model_config_size_chk 제거 | 수정: DROP CONSTRAINT만 수행하고 재추가하지 않음 |
| document_cross_review_budget/leases 테이블 | 수정: 보존, DROP 문 제거 |
| 기존 test_doc_cross_review 테스트 | 유지: 삭제·수정 없음 |
| 롤백 SQL 정적 회귀 테스트 | 신규: 주석 제외 DROP TABLE/TRUNCATE 및 CHECK 재추가 부재 확인 |
| 삭제 | 없음 |

### 실제 변경 및 검증

- 변경: migrations/rollback/20261029_doc_cross_review.down.sql, tests/unit/test_doc_cross_review.py. 요청된 결과 기록은 이 RESULT.md에 추가했다.
- `bash scripts/run_unit_tests.sh tests/unit/test_doc_cross_review.py`: 종료코드 2. 테스트 컨테이너 `aads-server`, `aads-server-green`가 없어 5초 간격 6회 재시도 후 중단됐다. 통과 아님.
- 전체 `git diff --stat`: RESULT.md를 포함해 3개 파일(38 insertions, 5 deletions). 보고서 기록 요구까지 반영한 결과다.
- 대상 코드 2개만 `git diff --stat -- migrations/rollback/20261029_doc_cross_review.down.sql tests/unit/test_doc_cross_review.py`: 2 files changed, 14 insertions(+), 5 deletions(-).
- `git diff --check`: 통과.
- up 마이그레이션, 앱 코드, 운영 DB 및 HANDOVER.md 변경 없음. commit/push/build/deploy 실행 없음.

## 근태·급여 재검토 교정 결과 (runner-2120f418 / 재작업 2차)

### STEP 0 기존 구현 조사와 분류

- 시작 작업 트리는 clean이었다. 반려 산출물 4d4a95637a4d의 코드·테스트 7개 파일 diff를 git apply로 복원했다. 금지된 worktree/checkout/commit 명령은 사용하지 않았다.
- 유지: 기존 renderPageV41의 다른 메뉴 렌더링, workspaceApi/401 clearAuthAndOpenLogin, 서버 인증·권한·사업자 접근 검사, 출퇴근 저장/수정 API, 급여 상태 전이·금액 계산, 기존 CANONICAL_BUSINESSES 및 acct_company_map 매핑, 읽기 전용 레거시 접속.
- 수정: admin.html의 attendanceLoadV41/loadLiveRoute/renderPeopleV41/월 변경 이벤트. 전용 근태·급여 화면, 새 화면 내 직원 진입, 레거시 관리자 링크 제거는 반려 산출물을 유지하며 아래 세 결함만 보정했다.
- 수정: obys_workspaces.py의 _source_rows(급여 날짜 바인딩), list_attendance_records(병합 후 페이지 분할), workspace_records(다음 페이지 메타데이터), _record(마스킹과 별개 직원 키), _legacy_people_rows(직원 식별 필드 보존). 기존 라우트 경로 유지, 새로운 엔드포인트/스케줄러/테이블 없음.
- 신규: peopleMonthBoundsV41/peopleLoadV41, 서버 익명 직원 키 및 회귀 테스트. 기존 테스트 파일 삭제 없음.
- 범위 사유: 브리프에 개별 열거되지 않은 obys_workspaces.py는 지시된 V4.1 조회 응답과 테넌트 격리 구현의 실제 소유 모듈이다. admin.html 및 tests/unit/test_obys_people_dedicated_views.py와 기존 people 테스트 4개는 원 지시 및 반려 커밋의 변경 파일이다. 테스트 게이트 scripts/run_unit_tests.sh는 수정하지 않았다. RESULT.md는 기존 보고서에 결과를 추가한다.
- DB 접점 유지: yeoljeong_payroll_statements/yeoljeong_attendance_records 조회, 기존 사업자 소유권 검사 및 읽기 전용 legacy transaction. 마이그레이션과 운영 DB 쓰기 없음.
- 삭제: 관리자 legacy 링크/이동 핸들러만 반려 커밋대로 제거. 로그인 index.html 진입과 직원 초대 링크는 유지. 롤백은 이번 7개 코드·테스트 파일 diff만 복구하면 되며 DB 롤백은 없다.

### 리뷰 지적별 처리

1. P1 월 변경: 실제 loadLiveRoute 호출로 변경. 근태는 선택 월의 from/to, 급여는 date_from/date_to를 서버에 전송한다. 급여 로컬 SQL은 날짜를 바인딩한 뒤 조회하고 레거시도 같은 월을 바인딩한다. 병합 전 500건 제한을 없애고 응답은 500건씩 페이지 분할한다. 프론트는 has_more/next_offset을 따라 모두 읽는다. 초기 급여는 기존대로 최신 자료 월을 선택한다. 월 요청 실행 테스트와 601건 근태 페이지 회귀 테스트 통과.
2. P1 동명이인: 이름 Set/이름 find를 제거하고 서버 직원 키로 목록·달력·통계·개인 상세를 구분한다. 키는 서버의 employee_id 또는 정규화 이메일과 사업자 이름의 SHA-256이며 이메일 마스킹은 유지한다. 식별 근거가 없는 기록은 record ID별로 분리하여 이름만으로 합치지 않는다. 급여 상세는 직원 이름 대신 명세 record ID로 선택한다. 렌더러 실행으로 동명이인 달력과 두 번째 급여명세 선택을 검증했다.
3. P2 검색 후 미출근: 검색 결과의 직원 범위에서 후보를 고르고, 오늘 기록 유무는 검색 전 전체 월 자료로 판정한다. 검색에서 빠진 출근자를 미출근으로 추가하지 않는다. 검색 회귀 실행 테스트 통과.

### 실제 검증 결과와 제한

- 요구 명령 실행: bash scripts/run_unit_tests.sh tests/unit/test_obys_people_e2e_fix.py tests/unit/test_obys_people_menu_button_audit.py tests/unit/test_obys_attendance_api.py tests/unit/test_obys_stale_tenant_session_relogin.py tests/unit/test_obys_v41_payroll_permissions.py tests/unit/test_obys_menu_payroll_slice.py tests/unit/test_obys_payroll_state_machine.py tests/unit/test_obys_people_dedicated_views.py → exit 2. 기준 컨테이너 aads-server 없음. 통과 아님.
- 호스트 기존 가상환경으로 동일 전체 pytest 실행: 첫 실행은 Pillow 의존성 누락으로 수집 오류(exit 2). 시스템의 기존 dist-packages를 PYTHONPATH에 추가한 재실행은 20개 진행 후 HTTP TestClient 단계에서 대기하며 종료되지 않았다. asyncio.to_thread를 실행 프로세스 내에서만 직접 호출로 바꾼 별도 진단도 같은 지점에서 대기했다. 두 실행 모두 완료/통과로 기록하지 않는다. 프로세스 종료 명령은 실행하지 않았다.
- 호스트 별도 실행: people_e2e_fix, people_menu_button_audit, menu_payroll_slice, people_dedicated_views 4파일 → 36 passed, exit 0. 이후 601건 회귀 테스트 추가 후 people_dedicated_views 단독 → 13 passed, exit 0.
- 신규 테스트는 기존 급여 매핑 사업자 조회 및 타 테넌트 0건, 본인 근태 이메일 강제, 읽기 전용 DB transaction/close, 명세 금액 표시, 전용 탭과 공통 안내 카드 부재, 관리자 legacy 링크 부재, 월 요청·페이지·동명이인·검색을 포함한다.
- python3 tests/unit/obys_people_e2e_fix_check.py → exit 1. 로컬 HTTP 서버 socket 생성이 PermissionError(Operation not permitted)로 거절됨. 3시나리오/403 0건 미검증, 호스트 실행 필요.
- python3 -m compileall -q app/api/obys_workspaces.py tests/unit/test_obys_people_dedicated_views.py → exit 0.
- ruff check --select F821,F811 동일 두 파일 → All checks passed, exit 0.
- python3 scripts/dup_guard.py --paths 동일 두 파일 → exit 0. git diff --check → exit 0.
- 사업자 연결은 기존 정본 매핑에 정확히 일치하는 경우만 허용한다. 매핑 없는 사업자는 기존 자료 미연결로 표시한다. 운영 7사업자 실데이터 조회는 수행하지 않아 실제 연결별 건수는 미확인이다.
- 근무 예정표 원천과 지각·결근 판정 기준은 연결되지 않아 미집계 표시를 유지한다. 식별자가 없는 과거 근태는 기록 단위로 분리하며 동일 직원 월 합산을 추측하지 않는다. 브라우저 시각 검증과 전체 HTTP 테스트 exit 0는 미완료다.
- 변경 파일: app/api/obys_workspaces.py, app/static/apps/obys/admin.html, tests/unit/obys_people_e2e_fix_check.py, tests/unit/test_obys_menu_payroll_slice.py, tests/unit/test_obys_people_e2e_fix.py, tests/unit/test_obys_people_menu_button_audit.py, tests/unit/test_obys_people_dedicated_views.py, RESULT.md.
- commit/push/build/deploy 미실행, 새 SHA 없음. HANDOVER.md/테스트 게이트/시크릿 수정 없음.
- 새 정본 문서 생성 없음. 기존 RESULT.md 결과 추가이며 열람 링크 미제공. 운영 DB 쓰기 금지 범위에 따라 핸드오버 DB 미기록; Runner 완료 처리의 DB upsert/readback에 인계한다.

## 오비서 근무표 재검토 교정 결과 (ACCT-V41-WORK-SCHEDULE-LATE-ABSENT-20261030)

document_key: obys-work-schedule-rework-result
열람 링크 미제공. 기존 RESULT.md에 결과를 추가했으며 새 정본 문서는 생성하지 않았다.

### STEP 0 기존 구현 조사 및 분류

- 시작 HEAD ba42dc6b, 작업 트리 clean. 반려 커밋 814efe4f640f936b7cb7fa41e2912c14fa9ad7d9의 8개 파일 diff를 git show/git apply로 복원했다. 기존 산출물을 통째 재작성하지 않았다.
- 유지: _business, _require_route_read, _require_attendance_admin, require_legacy_obys_access, _legacy_people_rows의 소유 테넌트/정확한 사업자 매핑, _record/_public_raw 마스킹, 출퇴근 CRUD·주간 재계산·감사 로그, offset+limit 근태 목록 창, 기존 라우터 mount /api/v1/workspaces. 스케줄러 추가/변경 없음.
- 수정: list_attendance_records/ATTENDANCE_LIST_SQL의 판정 연결; 판정 함수와 일별 합산; list_work_schedules의 로컬/레거시 묶음 보강; _save_work_schedule의 마스킹 이메일 갱신. 반려본의 관리자 renderPeopleV41 근무표/현황/통계/달력, 직원 index.html 일정 읽기 전용 연결을 유지한다.
- 신규(반려 산출물 복원): WorkScheduleIn/Employee/BulkIn, 일정 GET/POST/PUT/DELETE 및 bulk, _schedule_business/_schedule_values/expand_work_schedules, 계약 차이 판정, work_schedules 마이그레이션/보존 롤백 SQL, 신규 테스트. 재검토에서 _schedule_time/_schedule_record_key/_attendance_schedule_matches/_schedule_actual_records를 추가했다.
- DB 접점: 기존 attendance_records/weekly, 계약·사업자·감사 조회/쓰기는 유지. 신규 yeoljeong_work_schedules의 활성 행 부분 고유키 및 business_id/work_date 인덱스. 운영 DB 접속·마이그레이션 적용 없음.
- 삭제: 기존 함수/라우트/파일/테스트 삭제 없음. 반복 attendance 상관 서브쿼리 및 행별 레거시 호출만 묶음 처리로 교체했다. 호출처 계약 유지, 롤백은 이번 코드 diff 복구 및 필요 시 제공된 데이터 보존 rename SQL 사용(직접 실행하지 않음).
- 명시 파일 외 변경 사유: app/static/apps/obys/index.html은 원 지시 5번 직원 홈의 실제 소유 파일이다. 신규 마이그레이션/rollback/test_obys_work_schedules.py는 원 지시의 필수 산출물이다. test_obys_people_dedicated_views.py는 반려본의 기존 근무표 UI 기대값 및 목록 601건 회귀 mock을 새 묶음 조회 접점과 분리하기 위해 보완했다. RESULT.md는 요청된 조사/검증/지적별 처리 기록이다. finance service는 수정하지 않았다.

### 리뷰 지적별 처리

1. DB 행은 조회 직후 dict로 변환하고 page_rows에 판정을 넣은 뒤 그 동일 page_rows를 응답으로 사용한다. MappingProxyType 불변 행 입력으로 raw.judgment.late_minutes가 실제 응답에 남고 원본은 바뀌지 않는 테스트 통과. _public_raw는 judgment를 재귀 직렬화한다.
2. total_worked_minutes가 없거나 None일 때 실근로 계산값을 사용하도록 명시 분기했다. None 및 480 회귀 통과.
3. time/datetime/ISO timestamp(Z 포함)를 공통 파서에서 지원하고 KST로 변환한다. 여러 행 중 잘못된 시각 행은 제외하여 유효한 직원 기록 처리를 계속한다. timestamp 분할 근무와 malformed 혼합 테스트 통과.
4. 출퇴근 목록에서 근무표·실적 jsonb_agg 상관 쿼리를 제거했다. 페이지 직원/날짜 키를 JSON으로 한 번 전달하여 근무표를 조회하고, 실제 기록도 일괄 조회하여 서버에서 그룹화한다. 조회 창과 하루 500행 한도 유지.
5. 같은 날짜/이메일 키를 set으로 중복 제거한다. attendance 조회는 한 묶음 ranked SQL에서 실행하며 행별 jsonb_agg 반복/actual_records 응답 중복을 제거했다. lower(email)은 대소문자가 섞인 기존 자료 호환을 위해 한 묶음 조인에 남겨두었다. 운영 실행계획·성능 측정은 하지 않았다.
6. 레거시 보강을 요청 기간 전체에 대해 1회 실행 후 직원/날짜로 묶는다. 상한은 페이지 일정 수 × 500, 최대 500,000행이며 무제한 조회가 아니다. 1,000개 일정의 DB fetch 2회/레거시 1회 회귀 통과. 레거시 장애는 503으로 명시하여 잘못된 결근 집계를 반환하지 않는 테스트 통과. 실제 대용량 성능과 이 상한을 넘는 레거시 자료 완전성은 미검증이다.
7. 신규 HTTP 테스트에서 member 요청 employee_email을 본인으로 강제, POST/PUT/DELETE/bulk 403, 다른 사업자·다른 테넌트의 읽기/쓰기 403을 확인했다. bulk 초과 422/정확히 1,000개 허용/선택 칸 등록 테스트 포함. HTTP 테스트는 비동기 httpx ASGITransport로 실행하며 로그인 의존성도 async로 재정의한다. 운영 인증 게이트 변경 없음.
8. ON CONFLICT에서도 employee_email_masked=EXCLUDED.employee_email_masked를 갱신한다. UPDATE의 고유키 충돌→409 트랜잭션 처리는 유지했다.

정책: ATTENDANCE_LATE_GRACE_MINUTES = 0. 서버 KST 기준 결근/오늘 미출근/야간 근무/정상·지각·조퇴·초과·휴무·일정없음 판정을 사용한다. UI는 서버 판정값을 표시한다.

### 실제 검증

- 필수 bash scripts/run_unit_tests.sh + 지정 8파일 + 신규 test_obys_work_schedules.py 실행: exit 2. 기준 container:aads-server 없음(6회 재시도). 게이트 수정 없음, 통과 아님.
- 동일 9파일 호스트 pytest: exit 2. test_obys_v41_payroll_permissions.py 및 test_obys_payroll_state_machine.py 수집 시 PIL 미설치. 처음 PYTHONPATH로 시스템 패키지를 우선한 시도는 typing_extensions.Sentinel ImportError로 실패했고, 이후 venv 기본 경로를 사용했다.
- 최종 호스트 pytest: test_obys_work_schedules.py 전체 + test_obys_people_e2e_fix.py + test_obys_people_menu_button_audit.py + test_obys_menu_payroll_slice.py → **52 passed, exit 0 (5.91s)**. 신규 28개 전체 포함, node 검사에는 shutil.which("node") skip 가드 유지.
- dedicated_views 포함 중간 부분 실행: 51 pass/7 fail. 6개는 PIL 미설치, 1개는 새 조회 접점에 맞지 않는 기존 mock이었다. mock 보완 후 재실행 52 pass/6 fail(PIL), exit 1. 최종 전체 게이트 통과로 기록하지 않는다.
- 초기 TestClient 실행은 10개 후 대기했고 종료 완료를 확인하지 못했다. 프로세스 종료 명령은 사용하지 않았다. 신규 테스트를 비동기 ASGI 방식으로 바꾼 최종 실행은 정상 종료했다.
- python3 tests/unit/obys_people_e2e_fix_check.py: exit 1, 로컬 HTTP 소켓 생성 PermissionError. **호스트 실행 필요**, 3시나리오/403 0건 미검증.
- 최종 python3 -m compileall -q (수정 Python 3파일), ruff check --select F821,F811, python3 scripts/dup_guard.py --paths, git diff --check: 모두 exit 0.
- scripts/deploy_acct_app_cafe24.sh diff는 allowlist **1행 추가만** 확인. HANDOVER.md/테스트 게이트/시크릿 변경 없음.
- 변경 파일: RESULT.md, app/api/obys_workspaces.py, app/static/apps/obys/admin.html, app/static/apps/obys/index.html, migrations/20261030_obys_work_schedules.sql, migrations/rollback/20261030_obys_work_schedules.down.sql, scripts/deploy_acct_app_cafe24.sh, tests/unit/test_obys_people_dedicated_views.py, tests/unit/test_obys_work_schedules.py.
- commit/push/build/deploy 미실행, 새 SHA 없음. 승인 후 Runner 빌드 검증 대상. 핸드오버 DB 미기록 — Runner 완료 처리의 DB upsert/readback 대상.


---

## 설정 모델·Antigravity 계정 API 재작업 결과

document_key: settings-model-account-api-rework-result
source_task_id: runner-b8e1a061
change_summary: 반려된 계정 식별·quota 귀속·출시일 보존 구현과 검증 결과를 후속 화면 작업에 전달한다.
작업: runner-f2b2113a, 자동 재작업 1/2.
정본 미등록(미완료), 열람 링크 미제공. DB 연결 시간 초과로 정본 등록과 실행 원장 확인이 불가능했다.

### 출발점·사전 점검

- 제공된 격리 작업트리는 최초 clean. HEAD와 로컬 origin/main 모두 8ee4fb6a1c2ac7b2c20be5073ec817eec9cc33a4였다. 원격 fetch는 수행하지 않았다.
- 이전 e20d1ad0db55의 의도된 6개 파일 diff를 git apply --check 후 적용하여 이어서 수정했다. 다른 작업의 dirty 파일을 덮어쓰지 않았다.
- /root/aads/AGENTS.md, 저장소 AGENTS.md와 제공 AAG를 먼저 확인했다. 추가 라우트·테이블·스키마를 만들지 않았다.
- pipeline_jobs/ledger/동일파일 충돌 DB 사전 조회는 asyncpg 연결 단계에서 TimeoutError였다. 충돌 없음으로 판정하지 않았으며 작업 상태·의존성을 변경하지 않았다. 기존 Runner가 제공한 격리 작업트리와 ALLOW_DUP_JOB/AUTO_REWORK 범위에서 파일만 수정했다. 원장 재확인은 미완료다.
- error_book.py match 실행: psql 실행파일 없음으로 실패. 원인 확인 뒤 register도 실제 실행했으나 같은 이유로 DB 미기록. 확인한 원인은 아래 1·2·4번이다.

### STEP 0 기존 구현 분류 및 변경 범위

- [유지] app/main.py의 시작 시 sync, _run_periodic_model_registry_sync와 APScheduler 등록(LLM_MODEL_REGISTRY_SYNC_HOURS 기본 6h). 기존 API mount 유지, main.py 변경 없음.
- [수정] model_registry의 _discovered_model_row, _merge_model_rows, build_registry_snapshots, list_registered_models, sync_model_registry: 기존 metadata 안에서 출시일 3개 필드 수집·노출·보존. 테이블 추가 없음.
- [수정] discover_provider_model_rows: 기존 6 provider 실행 결과에 observed_at만 추가. 성공/failed/skipped·error·runtime_executable·discovery_requirement 유지.
- [유지] 공급자 fetcher와 Google route 비활성 정책, 기존 Claude/Codex 인증·LLM 호출·DB 조회·audit/discovery_runs 저장 접점.
- [수정] llm_keys의 _antigravity_account, llm_overview: bound 상태 분기·이메일 안전 필드·quota 귀속·legacy DB 행의 Codex snapshot 오귀속 차단. CRUD·로그인 엔드포인트 유지.
- [수정] account_login._antigravity_binding: agy 자체 credential의 id_token_email 및 만료시각 사용. 로그인/토큰 쓰기/갱신/유료 생성 호출 없음.
- [신규] _antigravity_usage, release_date_fields, _reviewed_release_fields 및 공식 발표 날짜 5개 exact ID 매핑, 회귀 테스트. 기존 구현을 통째로 교체하지 않았다.
- [삭제] 기존 함수·라우트·테이블 삭제 없음. 이전 반려 변경의 3s 전역 timeout, 상수 latest_catalog_guaranteed, 가짜 Antigravity discovery run 추가만 제거했다. 기존 소비자 계약으로 돌아가므로 별도 호출처 이관 없음.
- 지시된 핵심 파일 외 변경 사유: app/api/llm_models.py는 실제 기존 모델 API serializer/selector에도 같은 출시일 필드를 노출하기 위해 변경. tests/unit/test_llm_overview_antigravity.py, tests/unit/test_model_registry.py는 요구된 상태·오귀속·날짜 보존 회귀 검증. 기존 RESULT.md는 요구한 번호별 검수 근거와 JSON 계약 보고를 위해 이 절만 추가(기존 내용 보존).

### 리뷰 지적별 처리

1. 고정 bound=False 제거. 호스트 agy 파일의 실제 schema 키(auth_method/id_token_email/token, token.expiry 등)만 확인했고 값은 출력하지 않았다. 독립 Google active 계정을 쓰지 않는다. id_token_email·access_token 존재·timezone 있는 미래 expiry를 모두 확인한 로컬 자격증명은 bound=True, 이메일을 actual_account에 전달한다. account_source=agy_credential_metadata, account_verified=False로 실시간 공급자 검사를 했다고 표현하지 않는다. 만료·손상·권한 실패는 unknown, 바이너리/credential 부재는 needs_login. test_host_identity_reaches_overview가 실제 binding→overview 경로를 검증한다.
2. 빈 값 하드코딩으로 통과하던 검증을 교정. usage_status=available일 때 bound 상태·quota_account 대소문자 무시 일치·timezone 있는 측정시각·정상 window를 검증한다. 같은 fixture에서 정상 계정은 35%를 수용하고 다른 계정은 collection_failed/null로 거부한다. 잘못된 수치도 거부, 반환 window는 allowlist 필드만 포함한다. 실제 설치 agy에 quota collector가 있다는 뜻은 아니다.
3. 신규 asyncio.wait_for(3s) 제거. overview의 Claude/Codex를 포함한 공통 relay 통신은 기존 httpx 20s/connect 2s timeout과 HTTPException 처리 그대로다. 새 동기 네트워크 probe를 추가하지 않았다. credential은 64KiB 상한의 로컬 읽기만 하며 overview는 기존 비동기 relay 경유다.
4. 전체 metadata 얕은 병합 제거. ON CONFLICT는 새 metadata로 교체하고, 기존 release_date/source/verified_at이 모두 non-null일 때 그 3개 값만 보존한다. 수동 검증된 날짜를 우선 보존하며 raw/discovery_requirement 등은 새 실행 값으로 교체·제거된다. 실제 sync SQL expression을 실행하는 PostgreSQL TEMP TABLE 테스트 추가. AADS_TEST_POSTGRES_DSN 미제공으로 이 테스트는 SKIP, DB 수준 검증 통과를 주장하지 않는다.
5. 읽기 전용이던 날짜 처리에 실제 공급 경로 추가. 공식 발표로 확인한 GPT-4.1/mini/nano와 Sonnet 4.5의 일반/고정 ID 총 5개에 날짜·URL·확인시각을 template/discovery/sync에서 채운다. https://openai.com/index/gpt-4-1/ (2025-04-14), https://www.anthropic.com/news/claude-sonnet-4-5 (2025-09-29), 확인 2026-10-30T11:08:43Z. 모델명 날짜/API created/first_seen 추론 없음. 다른 최신 모델 날짜는 여전히 null이며 전체 회사 최신 출시일 자동 수집 보장을 구현한 것은 아니다.
6. 항상 False인 필드와 Antigravity 합성 skipped run 삭제. run_results 소비자는 sync_model_registry의 Codex 보존·review_required·audit·discovery_runs insert, llm_models.list_model_discovery_runs의 행 반환임을 확인했다. 기존 provider 수/의미를 유지하고 observed_at만 추가. disabled/skipped/failed 테스트 및 기존 성공 discovery 테스트 통과. Antigravity catalog adapter 미지원은 계약/한계에 명시한다.
7. llm_models.py import를 normalize_provider 다음 release_date_fields 순서로 정렬했다.
8. 현재 기준 코드에 이미 from datetime import date, datetime, timezone이 있었다. 불필요한 import 수정 없이 실제 날짜 파싱 테스트·F821 검사·py_compile로 NameError 부재를 검증했다.

### 실행한 검증

- 최초 pytest: JWT_SECRET_KEY 미설정으로 collection error. 운영 시크릿을 읽거나 바꾸지 않고 테스트 프로세스에만 명시적 가짜 JWT 값을 지정해 재실행했다.
- timeout 60 env JWT_SECRET_KEY=<테스트 전용 값> /root/aads/aads-server/.venv/bin/python -m pytest tests/unit/test_llm_overview_antigravity.py tests/unit/test_model_registry.py tests/unit/test_llm_keys_slot_mapping.py tests/unit/test_model_registry_tier_backfill.py -q: **68 passed, 1 skipped (2.88s)**.
- SKIP: test_release_upsert_postgres_preserves_only_release_tuple (테스트 PostgreSQL DSN 없음). 운영 PostgreSQL 연결도 TimeoutError. DB merge는 미검증이다.
- python3 -m py_compile: 위 변경 Python 6개 파일 전부 통과.
- venv python -m ruff check --select F821,F811: 변경 Python 6개 파일 All checks passed.
- git diff --check 통과.
- agy --help, agy help models만 실행: account/quota 명령 없음. agy models 자체는 토큰 자동갱신을 유발할 수 있어 실행하지 않았다.
- http://127.0.0.1:8000의 /api/v1/health, /api/v1/llm-keys/overview, /api/v1/llm-models/discovery-runs GET은 모두 URLError. 실제 API 응답·운영 health 미검증.
- 실제 scheduler/discovery DB 재조회 불가. 2026-10-30 19:25:05 KST cron hourly_cli_discovery 수치 및 OAuth/Google skipped 사유는 제공 브리프 근거이며 새 실측으로 주장하지 않는다. skipped는 최신 자동 갱신 성공이 아니다.
- commit/push/build/deploy/서비스 재시작 실행 없음. HANDOVER.md 변경 없음.

### 후속 화면 JSON 계약·fixture 및 미완료

설정 모델·계정 API 재작업. release 세 필드는 nullable; 검증한 정확한 모델 ID 5개만 공식 발표로 채움, 미확인 모델 null. actual_account는 agy id_token_email, 만료되거나 잘못된 credential은 unknown, 자격증명/바이너리 없음 needs_login. bound는 로컬 유효성, account_verified=false(실시간 공급자 검사 없음). usage_status=available|unsupported|collection_failed|not_measured. windows는 [{window_minutes:int,used_percent:number,resets_at:ISO8601}], quota_account 일치·측정시각 필수. usage_summary=null: 계정 귀속 AADS 통계 미확인, 통계 schema/값을 추정해 제공하지 않음. 실제 agy quota collector 미지원, 실제 quota 미측정; quota_fixture는 테스트 전용. 기존 Claude/Codex 및 20s relay timeout 유지. JSON fixture: {"model": {"release_date": "2025-04-14", "release_date_source": "https://openai.com/index/gpt-4-1/", "release_date_verified_at": "2026-10-30T11:08:43Z"}, "account": {"provider": "antigravity", "actual_account": "agy@example.com", "bound": true, "state": "ok", "account_source": "agy_credential_metadata", "account_verified": false, "usage_status": "unsupported", "usage_note": "Installed relay has no supported provider quota collector", "usage_collected_at": null, "usage_summary": null, "windows": [], "used_percent": null, "resets_at": null}, "quota_fixture": {"bound": true, "account": "agy@example.com", "quota_account": "agy@example.com", "usage_status": "available", "usage_collected_at": "2026-10-30T10:00:00Z", "windows": [{"window_minutes": 300, "used_percent": 35, "resets_at": "2026-10-30T15:00:00Z"}]}}

출시일 미확인은 release_date/release_date_source/release_date_verified_at 모두 null. available이 아닌 quota는 windows=[], used_percent/resets_at/usage_collected_at=null이다. usage_note는 서버가 정한 설명만 내보내며 relay 임의 문자열은 노출하지 않는다. 기존 ok_72h/limit_72h 키는 유지하되 미측정을 0건으로 표현하지 않고 null을 반환한다. usage_summary는 현재 null만 제공하며 검증되지 않은 계정별 호출수·토큰 통계를 만들어 넣지 않는다.

핸드오버 DB 미기록: scripts/runner_handover_write.py로 entry_key=runner:runner-f2b2113a와 위 JSON 계약을 전달해 write_and_verify를 시도했으나 timeout 12 종료코드 124. DB 쓰기 및 재조회 성공은 확인하지 못했다. AADS_API_URL/handOver 인증 환경도 없고 tenant_id를 확인할 DB도 없어 임의 테넌트로 쓰지 않았다. handover_db_write_failed에 해당하는 실패를 여기 기록하며 실제 DB 이벤트 기록도 미확인이다. error_book DB 기록·정본 등록·운영 충돌 원장·실제 API·실제 provider quota·DB 수준 검증이 미완료다.

롤백: 이번 의도된 6개 Python 파일의 diff만 역적용하면 이전 동작으로 돌아간다. 새 스키마·운영 데이터 변경 없음. 이 RESULT 절을 제거할 경우에도 기존 보고 내용은 보존한다.
