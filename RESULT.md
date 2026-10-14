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
