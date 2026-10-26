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
