# 오비서 직원 승인 → 고용주 테넌트 멤버십 → 계약서 서명

TASK: AADS-OBYS-EMPLOYEE-TENANT-MEMBERSHIP-20260930

## 왜 필요한가 (2026-09-30 진아서버 실측)

직원 서명이 403 으로 막혀 **어떤 직원도 서명을 끝낼 수 없었다.** 세 겹으로 막혀 있었다.

1. 직원 로그인 테넌트가 고용주 테넌트(tenant-32 `15055cac-…`)가 아니라, 가입할 때 자동으로 만들어진
   본인 워크스페이스(tenant-28/29/30)였다. `tenant_memberships` 에 고용주 테넌트 소속 직원은 0명이었다.
2. `require_legacy_obys_access` 가 서명 경로를 막았다(허용목록 밖, `_TENANT_SCOPED_PREFIXES` 에도 없음).
3. 게이트를 통과해도 `_read_hr` 이 본인 워크스페이스로 범위를 좁혀 계약서가 보이지 않았다. 그 워크스페이스에서
   직원은 owner 라서 `_is_admin`=True 가 되고, "관리자는 직원 대신 서명할 수 없습니다" 로 한 번 더 막혔다.

## 순서

| 단계 | 무엇이 일어나나 | 코드 |
|---|---|---|
| 0. 가입요청 | 직원이 **본인 로그인으로** 자기 이메일 요청을 내면 `requester_user_id`·`requester_email`(JWT 값)이 요청에 고정된다. 남의 이메일을 넣은 요청·관리자 대리 등록은 고정되지 않는다(`registered_by` 만 남음) | `upsert_join_request` |
| 1. 가입요청 승인 | `PATCH /api/v1/yeoljeong-finance/employees/join-requests/{id}` `{"action":"approved"}` | `review_join_request_with_membership` |
| 2. 멤버십 연결 | 계정을 정한다 — ① 고정된 `requester_user_id`(현재 이메일 == 요청 이메일일 때만, 다르면 `identity_mismatch`), ② 고정이 없으면 요청 이메일로 `saas_users` 조회(승인자 보증, `identity_basis=email`). 그 계정을 승인자 테넌트에 `role=member, status=active, invited_by=승인자` upsert. 기존 활성 owner/admin 은 강등하지 않는다. 계정이 없으면 `pending_account`, `requester_*` 가 요청 이메일과 어긋난 요청은 `pending_identity` (모두 승인은 성공) | `app/auth.py` `link_employee_tenant_membership` → `upsert_tenant_membership` (초대 수락과 공용) |
| 3. 시작 테넌트 | 이 승인이 멤버십을 **새로 만들거나 되살린 그 한 번**, 직원의 `default_tenant_id` 가 비었거나 본인 혼자인 개인 워크스페이스면 고용주 테넌트로 옮긴다 | `_move_default_off_personal_workspace` |
| 4. 연결 표시·감사 | 가입요청에 `membership_link{tenant_id,user_id,membership_id,linked_by,linked_at}` 을 남기고, `yeoljeong_hr_tenant_attribution_audit` 에 `ledger_table='tenant_memberships'` 행 1개 (actor, tenant, 직원, before/after). 파일·DB I/O 는 워커 스레드에서 한다 | `_save_membership_link`, `record_membership_audit` |
| 5. 직원 로그인 | 로그인 규칙은 **바꾸지 않았다** — `default_tenant_id` 존중 그대로. 3단계에서 옮긴 값으로 고용주 테넌트에 들어간다 | `resolve_login_tenant_for_user`(무변경) |
| 6. 서명 조회·서명 | `GET /contracts/signing/{token}`, `POST /contracts/signing` — 게이트는 이 두 라우트(메서드·경로 모양까지)만 테넌트 스코프로 통과시킨다 | `app/core/obys_tenant.py` `_is_contract_signing_route` |

응답 예 (승인):

```json
{"request": {...,"status":"approved"},
 "membership": {"status":"linked","tenant_id":"15055cac-…","user_id":"…","role":"member",
                "membership_status":"active","audit_recorded":true,
                "default_tenant":{"before":"<개인 워크스페이스>","after":"15055cac-…"}}}
```

`membership.status` 값: `linked`(생성·재활성) / `unchanged`(이미 같은 상태) / `pending_account`(계정 없음) /
`pending_identity`(`requester_*` 가 요청 이메일과 어긋남 — 남의 이메일로 낸 흔적) / `identity_mismatch`(계정 이메일 ≠ 요청 이메일) /
`skipped`(고용주 테넌트가 active customer 아님, 또는 반려 시 회수 대상 아님 — `reason` 참고) / `removed`(회수) /
`kept_elevated` / `not_member` / `error`(인증 DB 오류 — 승인은 성공, 감사에 `membership_failed`).

### 신원 (R2 라운드 2 에서 조정)

라운드 1 은 본인 제출 고정(`requester_user_id`)만 인정했다. 그런데 가입요청·초대 수락 경로
(`/employees/join-requests`, `/employees/invites/accept`)는 `_TENANT_SCOPED_PREFIXES` 밖이라
**레거시 허용 테넌트 JWT 만** 통과한다. 개인 워크스페이스로 로그인한 직원은 고용주 테넌트에 요청을 낼 수 없으므로
(게이트 403 — `test_employee_cannot_file_join_request_from_personal_workspace`), 실제 요청은 거의 전부
관리자 대리 등록·예전 요청이고, 고정만 인정하면 승인 거의 전부가 `pending_identity` 에 머물렀다.

그래서 지금 순서는:

1. 본인 제출 고정이 있으면 그 계정. 계정 이메일이 바뀌었으면 `identity_mismatch`.
2. 고정이 없으면 요청 이메일로 `saas_users` 조회(`identity_basis=email`). 이메일은 **승인하는 owner/admin 이 보증**한다
   — 지시서 B-1 의 원래 규칙이고 백필 스크립트와 같다.
3. `requester_*` 가 남아 있는데 요청 이메일과 어긋나면(남의 이메일로 낸 흔적) 이메일로 찾지 않고 `pending_identity`.

라운드 1 이 걱정한 "개인 워크스페이스에서 남의 이메일로 요청을 내고 스스로 승인" 은 위 게이트 때문에 불가능하다.
가입요청 경로를 나중에 테넌트 스코프로 열면 이 전제가 깨지므로 2번을 다시 검토해야 한다(위 테스트가 그 신호다).
반려 회수는 여전히 이 승인이 남긴 `membership_link`(membership_id 일치, role=member)만 대상이다.

### 로그인 시작 테넌트 (적용 범위)

- 로그인 때 판정하지 않는다. 승인이 멤버십을 새로 만들거나 되살린 순간 한 번만 `default_tenant_id` 를 옮긴다.
  그래서 일반 초대 member·다른 고객 테넌트 member 는 영향이 없고, 재승인(`unchanged`)은 default 를 다시 건드리지 않는다.
- 옮기는 조건: default 가 비었거나, 본인이 만든 customer 테넌트에서 본인이 **활성**(`status='active'`, `deleted_at IS NULL`)
  owner 이고 본인 외 활성 멤버가 없는 개인 워크스페이스.
  본인 사업장(다른 멤버가 있음)이 default 인 사람은 그대로다.
- 이후 `/auth/tenants/{id}/switch` 로 고른 값이 그대로 남는다. 내부 운영자·CEO `default_tenant_id` 존중(2026-09-22)은
  로그인 코드를 건드리지 않았으므로 그대로다.

## 서명 경로의 테넌트 스코프 확인 결과

`/api/v1/yeoljeong-finance/contracts/signing` 을 `_TENANT_SCOPED_PREFIXES` 에 넣은 전제는 "모든 조회가 JWT 테넌트에 묶인다"는
것이다. 코드로 확인한 내용:

- `get_contract_by_token(token, user)` → `_read_hr("contracts", user)` 가 `_tenant_id(user)`(활성 멤버십 테넌트 == JWT 테넌트가
  아니면 403)로 **SQL 에서** `tenant_id` 로 거른 행만 읽는다. 토큰은 그 안에서만 찾는다 → `_contract_signer_email` →
  `_is_admin` 이면 403 → `_require_hr_record(contract, user)` 로 레코드 `tenant_id` 를 다시 대조 → `employee_email` 일치를 확인.
- `sign_contract(payload, user)` → 같은 `_read_hr` + `_contract_signer_email` 경로, 저장은 `_write_hr_record`(레코드 테넌트 ≠
  JWT 테넌트면 403, DB upsert 도 `WHERE tenant_id = EXCLUDED.tenant_id`).
- 토큰이 없거나 다른 테넌트 것이면 존재 여부를 드러내지 않고 **403** 을 준다(이전 404 → 403, `_signing_contract_for_token`).
  소비처 확인: 서명 화면(`app/static/apps/obys/index.html` `handleContractLinkFromUrl`·서명 제출)은 `error.message` 만
  토스트로 보이고 상태코드로 분기하지 않는다. 같은 파일에서 상태코드 분기는 401(세션 만료)과 매출 화면 403 뿐이다.
  `aads-dashboard/src` 에는 `/contracts/signing` 호출이 없다.
- 게이트는 `POST /contracts/signing` 과 `GET /contracts/signing/{token}` 만 연다. R3 부터 prefix 가 아니라
  (메서드, 경로 fullmatch) 명시적 화이트리스트이고, GET 토큰은 서비스 생성 모양(`secrets.token_urlsafe(24)` → URL-safe 32자)만
  받는다. `/contracts/signing/{token}-extra`·`/{token}/sub`·`/signing-extra` 는 게이트로 열리지 않고 기존 판정(레거시 허용목록)을 탄다.
  `DELETE /contracts/signing`(= `/contracts/{id}` 에 id="signing"), `GET /contracts/signing/signed-pdf`, `/contracts`,
  `/contracts/signing-x` 는 계속 403. `_allowed_tenant_ids()` 는 바꾸지 않았다.
- 기존 prefix 9개(session·tenant-registry·uploads·uploaded-ledger·ledger-entries·card-transactions·card-uploads·
  ledger-bank-transactions·journals)의 `startswith` 판정은 **그대로** 두었다(부분 일치 포함). obys_finance 라우트 전수 대조에서
  기존 판정과 달라지는 경로는 0개다.

그대로 유지되는 통제: 관리자 대리 서명 403, 서명자 이메일 == `employee_email`, 이름 일치, 동의 문구 버전, PNG 검증,
봉인 후 수정·삭제 409.

## 배포 전 적용 (오비서 업무 DB 전용)

감사 테이블에 컬럼과 classification 값을 추가한다. AADS DB 자동 적용 대상이 아니다(baseline HOLD).

```bash
psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_employee_membership_audit.sql
# 롤백: migrations/rollback/20260930_obys_employee_membership_audit.down.sql
```

마이그레이션 전에 코드가 먼저 나가도 승인은 성공한다. 감사 INSERT 만 실패하고 응답의 `audit_recorded=false` 와 에러 로그로 남는다.

## 기존 승인 직원 소급 (백필)

마이그레이션·자동 백필은 쓰지 않는다. 명시 실행 스크립트만 쓴다. 기본은 dry-run 이다.

```bash
# 운영 컨테이너 안. DSN 기본값: --obys-dsn=$OBYS_DATABASE_URL, --auth-dsn=$DATABASE_URL
python3 scripts/obys_employee_membership_backfill.py --tenant 15055cac-71b0-45ec-b714-7093dde189ff        # dry-run
python3 scripts/obys_employee_membership_backfill.py --tenant 15055cac-71b0-45ec-b714-7093dde189ff --apply
```

dry-run 출력 예시(단위 테스트의 가짜 DB 로 뽑았다. 실제 운영 값이 아니다):

```
[DRY-RUN] 승인 가입요청 3건 → 대상 (tenant,email) 3건
tenant_id                             email                             user          before              action           after
15055cac-71b0-45ec-b714-7093dde189ff  dudgns3738@naver.com              yes/email     -                   link             member/active (예정)
15055cac-71b0-45ec-b714-7093dde189ff  beullingma3@gmail.com             yes/email     owner/active        kept_elevated    owner/active
15055cac-71b0-45ec-b714-7093dde189ff  not-signed-up@example.com         no            -                   pending_account  -
요약: {"kept_elevated": 1, "link": 1, "pending_account": 1}
dry-run 이다. 반영하려면 --apply 를 붙여 다시 실행한다.
```

- `user` 열의 `yes/user_id` 는 요청에 고정된 계정, `yes/email` 은 이메일로 찾은 계정이다. 이메일 기준은 이 스크립트에서만
  쓴다 — **`--apply` 전에 `yes/email` 행이 실제 그 직원인지 확인한다.**
- `link` 만 `--apply` 에서 쓴다. `kept_elevated`·`unchanged`·`skipped` 는 건드리지 않는다. `link` 한 행은
  가입요청 `request_payload.membership_link` 에 표시를 남기고(이후 반려·퇴사가 회수 가능), 개인 워크스페이스가 default 면
  서비스 경로와 같은 함수로 고용주 테넌트로 옮긴다.
- `pending_account` 인 직원이 나중에 가입하면 **이 스크립트를 다시 돌려 연결한다**(멱등).
  가입 순간에 자동으로 연결하는 경로는 없다. 인증 DB 와 오비서 DB 가 분리돼 있어서 가입 코드가 승인 여부를 모른다.
- `--apply` 는 `link`/`pending_account` 행마다 감사(`source=backfill_script`)를 남긴다.

## 회수 (반려·퇴사)

같은 엔드포인트에 `{"action":"rejected"}`. 승인됐던 직원을 반려로 바꾸는 것이 현재의 퇴사 처리 경로다
(직원 삭제 API 는 따로 없다).

회수는 **그 가입요청의 승인이 만든 멤버십**에만 적용한다. 세 조건을 모두 만족해야 한다.

1. 직전 상태가 `approved` 였다 (한 번도 승인 안 된 요청의 반려는 멤버십을 건드리지 않는다 — `request_was_not_approved`).
2. 가입요청에 `membership_link` 가 있고 테넌트가 같다 — 승인 때 멤버십을 새로 만들었거나 되살렸을 때만 남는다.
   승인 전부터 초대로 활성 member 였다면(`unchanged`) 표시가 없어 회수하지 않는다(`membership_not_linked_by_request`).
3. 현재 멤버십 행의 `id` 가 표시의 `membership_id` 와 같고 역할이 `member` 다.

`invited_by` 대신 `membership_id` 를 쓰는 이유: 공용 upsert 는 초대 수락의 기존 동작대로 충돌 시 `invited_by` 를 갱신하지
않는다. 회수됐다 되살아난 행은 `invited_by` 가 예전 초대자로 남으므로 이 요청과의 연결 근거가 못 된다.

- 조건을 만족하면 `status='removed'` 로 바꾼다. 행은 지우지 않는다.
- 그 사이 `owner`/`admin` 으로 승격됐으면 회수하지 않는다(`kept_elevated`), `viewer` 등 다른 역할이면 `skipped`.
- 회수 뒤 로그인하면 default 멤버십이 죽어 있으므로 본인 워크스페이스로 들어간다. 그 컨텍스트에서는 계약서가 보이지 않는다(403).

## 확인 쿼리

```sql
-- [AADS 인증 DB] 고용주 테넌트 소속 현황
SELECT u.email, tm.role, tm.status, tm.invited_by, tm.updated_at
  FROM tenant_memberships tm JOIN saas_users u ON u.id = tm.user_id
 WHERE tm.tenant_id = '15055cac-71b0-45ec-b714-7093dde189ff'
 ORDER BY tm.role, u.email;

-- [AADS 인증 DB] 직원 로그인 시작 테넌트 판정 재료
SELECT u.email, u.default_tenant_id, tm.tenant_id, tm.role, tm.status, tm.updated_at,
       t.created_by = tm.user_id AS created_by_self, t.kind
  FROM saas_users u JOIN tenant_memberships tm ON tm.user_id = u.id JOIN tenants t ON t.id = tm.tenant_id
 WHERE lower(u.email) IN ('dudgns3738@naver.com', 'zotma1@naver.com');

-- [오비서 DB] 승인 직원 목록 (백필 대상 원본)
SELECT id, lower(employee_email) email, status, tenant_id
  FROM yeoljeong_employee_join_requests
 WHERE deleted_at IS NULL AND tenant_id = '15055cac-71b0-45ec-b714-7093dde189ff'
   AND lower(COALESCE(NULLIF(request_payload->>'status', ''), status)) = 'approved';

-- [오비서 DB] 멤버십 연결·회수 감사
SELECT classified_at, classification, reason, source, actor_email, employee_email,
       before_state, after_state
  FROM yeoljeong_hr_tenant_attribution_audit
 WHERE ledger_table = 'tenant_memberships'
 ORDER BY classified_at DESC LIMIT 50;

-- [오비서 DB] 서명 결과
SELECT id, employee_email, contract_payload->>'status' status, contract_payload->>'signed_at' signed_at,
       contract_payload->>'signature_sha256' sig, contract_payload->>'signed_snapshot_sha256' snap
  FROM yeoljeong_contracts
 WHERE tenant_id = '15055cac-71b0-45ec-b714-7093dde189ff' AND deleted_at IS NULL
 ORDER BY updated_at DESC;
```

## R2 — 1차 커밋이 dup_guard 거짓 양성에 막힌 건

1차(runner-3ef7add7)는 pre-commit 의 `scripts/dup_guard.py` `sql-dup-col` 에서 막혔다.
`tests/unit/test_obys_employee_tenant_membership.py` 의 단정문
`assert "UPDATE tenant_memberships SET updated_at = now()" in switch` 를 SQL 로 보고
뒤쪽 2000자를 SET 본문으로 삼아, 뒤에 나오는 `monkeypatch.setattr(..., raising=False)` 3회를
"SET raising 3회" 로 셌다. 우회(`ALLOW_DUP_COMMIT=1`, 예외목록) 없이 검사기를 고쳤다.

- 매치가 한 줄 문자열 리터럴(또는 `#` 주석) 안에서 시작하면 그 리터럴 끝에서 SET 본문을 닫는다.
  파이썬 암묵적 이어붙이기 줄(`"...SET a = 1, "` 다음 줄 `"b = 2 WHERE ..."`)은 따라간다.
- SET 대상은 괄호 깊이 0 의 쉼표 조각에서만 센다. 짝 없는 `)` 에서 본문을 끝낸다 —
  호출 인자(`raising=`, `match=`)는 괄호 안이라 세지 않는다.
- 저장소 전체 비교(2026-09-30 재측정, 1,704 파일, 같은 트리에 옛/새 검사기): 탐지 12건 → 8건. 빠진 4건은 전부
  문자열 단정문 거짓 양성(`test_goal_dispatch_load_defer.py:53` 기존 기준선 항목 + 회귀 테스트 문자열), 새로 생긴 탐지 0건.
- 회귀 테스트: `tests/unit/test_dup_guard.py` — 문자열+raising= 거짓 양성 0건, 실제 UPDATE SET·INSERT 컬럼
  중복 1건씩, 이어붙인 리터럴·삼중따옴표 SQL 중복 유지.

## R2 라운드 1 — AI 리뷰 반려(runner-0e9ce12d) 지적 처리

| # | 지적 | 처리 |
|---|---|---|
| 1 | 반려가 같은 이메일의 정상 멤버십까지 회수 | 직전 approved + `membership_link` + `membership_id` 일치 + role=member 일 때만 회수 (위 "회수") |
| 2 | 이메일만으로 신원 판단 | `requester_user_id` 고정, 온라인 승인은 그 값으로만 연결 (위 "신원") |
| 3 | `PY_EXT + SH_EXT` 가 튜플인가 | 튜플이다(`scripts/dup_guard.py:58-59`). `test_literal_window_applies_to_code_files` 가 .py/.sh 에서 분기가 실제로 타는 것을 고정 |
| 4 | diff 잘림·파일 누락 | 테스트·백필·baseline HOLD 모두 커밋 대상에 포함(아래 "변경 파일") |
| 5 | 기존 prefix 판정 변경 | 기존 prefix 는 `startswith` 그대로, 서명 두 라우트만 별도 매칭 |
| 6 | 로그인 동작 적용 범위 | 로그인 휴리스틱 제거. 승인 시점 1회 default 이동으로 대체 |
| 7 | 404 → 403 | 403 유지. 프런트 소비처가 상태코드로 분기하지 않음을 확인(위 "서명 경로") |
| 8 | 비동기 경로 블로킹 I/O | 감사·연결 표시는 `asyncio.to_thread`, 파일 원장은 `flock` 으로 읽고-쓰기 직렬화, DB 는 워커의 `_run_db` |

## R2 라운드 2 — AI 리뷰 반려(runner-0f81541a) 지적 처리

| # | 지적 | 처리 |
|---|---|---|
| 1 | 변경 목록에 dup_guard·테스트·백필·마이그레이션 누락 | 리뷰 입력(`.runner_full_diff.patch`)이 잘린 것 — 커밋 `c98d65e0` 자체에는 13개 파일이 모두 있었다. 이번 작업트리에도 전부 포함(아래 "변경 파일") |
| 2 | 감사 INSERT 새 컬럼·CHECK | `migrations/20260930_obys_employee_membership_audit.sql` 이 컬럼 8개 추가 + classification CHECK 재생성(membership_* 6값). 마이그레이션이 HOLD 라 코드가 먼저 나가면 DB INSERT 가 실패하므로, 실패 시 파일 원장(`tenant_membership_audit.json`, `db_insert_failed=true`)에 남긴다 |
| 3 | import 누락 / 승인 전 단계 보호 | `asyncio`·`fcntl`·`json`·`uuid4`·`logger` 는 모듈 상단(9·13·16·32행, logger 모듈 전역)에 이미 있다. `_review_join_request_with_previous` 는 승인 판정 자체라 실패하면 API 가 실패해야 한다(권한 403·검증 400) — 보장은 "멤버십 실패가 승인을 되돌리지 않는다" 이다 |
| 4 | 초대 수락 동작 변화 | `tenant_invites.invited_by` 는 saas_users FK(ON DELETE SET NULL, migrations/100) 라 값 동일. 응답 membership 키는 예전 4개로 되돌림. 동작 테스트 `test_invite_acceptance_behaviour_is_unchanged` 추가 |
| 5 | 직전 상태 읽기·검토 경합 | 검토+멤버십 동기화 전체를 직렬화. (R3 에서 `flock` → DB `pg_advisory_xact_lock` 으로 교체 — 아래 R3) `test_concurrent_reviews_are_serialized` |
| 6 | 서명 경로 매칭이 다른 GET 라우트로 샐 가능성 | 실제 라우트 표 전수 비교 테스트 `test_gate_open_paths_resolve_only_to_signing_handlers` — 게이트가 여는 요청은 FastAPI 매칭상 서명 핸들러 둘로만 간다 |
| 7 | 개인 워크스페이스 판정에 owner 활성 여부 없음 | `tm.status='active' AND tm.deleted_at IS NULL` 추가 |
| 8 | 직원 요청이 개인 워크스페이스로 가 거의 전부 pending_identity | 지적이 맞다(게이트가 개인 워크스페이스 JWT 의 가입요청을 403). 고정 없는 요청은 승인자 보증 이메일로 연결(위 "신원") |

## R3 — 실결함 3건 (AADS-OBYS-EMPLOYEE-TENANT-MEMBERSHIP-R3-20260930)

| # | 결함 | 처리 | 테스트 |
|---|---|---|---|
| 1 | 이메일 → 계정 조회가 "레거시 테넌트 JWT 컨텍스트가 잡혀 있다" 를 주석으로만 전제 | ① 서비스: 승인 저장 **전** `_precheck_join_review` → `_require_employee_membership_context`(JWT 테넌트+활성 멤버십 · 가입요청 테넌트 일치 · 사업자 귀속), 실패 시 403/404/400 이고 아무것도 저장 안 함. ② 인증: `link_employee_tenant_membership(context_tenant_id=)` 와 `_active_user_id_by_email` 진입부 `_require_email_lookup_context` — 부재·불일치면 `EmployeeTenantContextError`. ③ 조회 SQL 에 테넌트 조건(이 테넌트 default·멤버십·초대, 또는 아직 어느 고객 테넌트에도 안 묶인 계정만). 같은 우선순위 후보 둘이면 고르지 않음(`pending_account`) | `test_email_lookup_without_tenant_context_raises`, `test_email_lookup_with_other_tenant_context_raises`, `test_same_email_in_two_tenants_matches_only_the_right_one` 외 5 |
| 2 | 멤버십 직렬화가 파일 `flock` — blue/green 두 컨테이너는 서로를 못 본다 | `pg_advisory_xact_lock(key)`, key = sha256("obys-employee-membership:{tenant}:{email}") 앞 8바이트 signed bigint(`employee_membership_lock_key`). 검토 전체를 `employee_membership_lock` 트랜잭션 안에서 돌리고 link/revoke 는 같은 연결(세이브포인트)에서 재진입 락. `_acquire_join_review_lock`·`_release_join_review_lock`·`JOIN_REVIEW_LOCK` 삭제. UNIQUE(tenant_id,user_id) + ON CONFLICT 유지. 백필 `--apply` 도 같은 락 | `test_same_tenant_email_concurrent_links_create_one_membership` 외 4 |
| 3 | 서명 게이트 GET 을 prefix 로 판정 | `_CONTRACT_SIGNING_WHITELIST` (메서드, fullmatch 정규식) | `test_gate_whitelist_does_not_open_lookalike_paths` 외 4 |

감사 파일 원장(`tenant_membership_audit.json`) 의 `flock` 은 **멤버십 직렬화가 아니라** DB 없는 파일 모드의 읽고-쓰기
보호라 그대로 둔다(DB 모드에서는 감사가 DB INSERT 이고, 파일은 INSERT 실패 폴백일 때만 쓴다).

## 변경 파일

- `app/api/obys_finance.py` — 승인 라우트가 `review_join_request_with_membership` 를 부른다.
- `app/auth.py` — `upsert_tenant_membership`(초대 수락과 공용), `link_employee_tenant_membership`,
  `revoke_employee_tenant_membership`, `_move_default_off_personal_workspace`. 로그인·switch 는 무변경.
- `app/core/obys_tenant.py` — 서명 두 라우트 매칭(`_is_contract_signing_route`). 기존 prefix 무변경.
- `app/services/yeoljeong_finance_service.py` — 요청자 신원 고정, 승인·회수·감사·연결 표시, 서명 토큰 미존재 403.
- `migrations/20260930_obys_employee_membership_audit.sql`(+`rollback/…down.sql`), `scripts/migrations_auto_apply_baseline.txt`(HOLD).
- `scripts/obys_employee_membership_backfill.py`, `scripts/dup_guard.py`.
- `tests/unit/test_obys_employee_tenant_membership.py`, `tests/unit/test_dup_guard.py`.

## 범위 밖 (별건)

- `_is_admin` 판정 규칙 자체(owner + 본인 pending 가입요청 조합)는 바꾸지 않았다.
- 멤버십(인증 DB)과 가입요청·감사(오비서 DB)는 한 트랜잭션이 아니다. 승인이 저장된 뒤 멤버십을 바꾸므로
  "승인 안 됐는데 멤버십만 생기는" 일은 없다. 반대로 멤버십 실패는 `membership.status=error` 로 남으므로
  백필 스크립트로 다시 맞춘다.
