# 오비서 정지 소속 세션 재로그인 복구 결과

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
