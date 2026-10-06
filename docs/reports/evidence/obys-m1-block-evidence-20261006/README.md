# ACCT-OBYS-M1-E2E-TENANT-BLOCK-EVIDENCE-20261006 — 결과 (부분 완료, 차단 항목 있음)

운영 SHA 2d1ebcbc (카페24 acct-app-candidate-r12 / fb.newtalk.kr). 화면(Playwright) 캡처는 수집하지 못했다. API 응답 코드만 `api-probe-results.json` 에 있다.
이메일은 마스킹, 비밀번호는 Agent Vault(`e2e_credentials`, service=`obys-e2e`)에만 있다.

## 만든 것

| 종류 | 값 | 경로 |
|---|---|---|
| 소유자 계정 A | user 3feb4d60-e85f-41b5-8b55-8d7e3aebaf03 (e2e***@aads.dev), 테넌트 1bbc4f60-a1e6-462f-9a11-e3c52de4b739 `E2E-오비서시험A` | 정식 API `/auth/register` |
| 시험 회사 A | `biz-e2e-m1-a` | 정식 API `POST /tenant-registry/businesses` |
| 시험 회사 B | `biz-e2e-m1-b` (기존 `e2e-acct` 테넌트 0d5f2f62-… 소유로 연결) | DB 직접 INSERT (트랜잭션) |
| 점포 | `br-e2e-m1-a-1`, `br-e2e-m1-b-1` | DB 직접 INSERT (정식 API 없음) |
| 사업자-테넌트 매핑 | 위 두 회사 | DB 직접 INSERT (정식 API 없음) |
| 직원 계정 1 | bc6416b8-bab5-4718-8d83-efa8e67277e0, 테넌트 cf4d829b-a36c-4af2-8499-87a564267f9f | `/auth/register` |
| 직원 계정 2 | 7219023d-d214-4a2a-b5dc-2a84d7c4bd65, 테넌트 514326e0-919f-4831-a714-8acf644d4351 | `/auth/register` |
| 가입요청 | deaa9e5a-… (직원2 → 회사A, pending), fd567e75-… (직원2 → 회사B, pending) | 정식 API |
| Vault | e665c275-14d1-4df3-a93c-38f4e715437a (owner-a), 36bedad0-f3b6-404d-8de7-3779f0b1eac4 (staff1), 0f3852e8-91f5-41f1-bcd1-1cdf2189e1c8 (staff2) | credential_vault.create_credential |

DB 직접 INSERT 전 백업(카페24 114): `/root/backup/obys-m1-e2e-20261006/obys_auth_before.dump`, `obys_biz_before.dump`
(회사 A 생성 직전 시점 — 사업자 5 / 점포 5 / 매핑 5 / 가입요청 17건). 전후 건수: 사업자 6→7, 점포 5→7, 매핑 5→7.

## 항목별 실측

| 항목 | 기대 | 실측 |
|---|---|---|
| 1 누락 입력 | 400 | 공백 실명 400, 이메일형 이름 400, 회사·점포 누락 400 (API). 화면 캡처 미수집 |
| 2 중복 요청 | 1건 유지 | 같은 요청 2회 모두 200, 같은 id, 목록 1건 (API). 화면 캡처 미수집 |
| 3a 없는 토큰 | 404 | resolve·accept 모두 404 |
| 3b 만료 초대 | 410 | **차단 — 시험 회사 소유자가 초대를 발급할 수 없다** |
| 3c 재수락 | 409 | **차단 — 같은 이유** |
| 4 타사 요청 | 403 | 직원 본인 이메일로 회사 B 요청은 **200** (설계상 허용). 남의 이메일로 회사 A/B 요청은 403 |

## 차단 사유

1. 3b/3c: `POST /employees/invites` 가 시험 회사 소유자 토큰으로 403(`이 계정에는 오비서 레거시 데이터 접근 권한이 없습니다`)이다.
   `app/core/obys_tenant.py` 의 `_TENANT_SCOPED_PREFIXES` 는 직원 본인 경로(`join-requests`, `invites/resolve`, `invites/accept`)만 열고, 초대 생성·목록·승인은
   `OBYS_LEGACY_TENANT_IDS` 허용목록(열정국밥 테넌트)만 쓴다. 시험 테넌트를 허용목록에 넣으면 레거시 원장 접근이 열리므로 하지 않았다.
   실제 열정국밥 테넌트로 초대를 만드는 것과 초대 저장소를 직접 수정하는 것은 지시서 금지 범위다.
2. 4: 지시서 기대(직원이 타 시험 회사에 요청하면 403)는 코드와 다르다. `_join_request_scope` 는 본인 이메일 요청이면 다른 사업자에도 허용한다. 403 은 남의 이메일로 낼 때뿐이다.
3. 화면 증거: 개인 워크스페이스 직원은 직원 관리 화면으로 전환되지 않았다(`직원 현황·초대·가입승인` 클릭 후에도 통합 홈 유지). 가입 폼 회사 목록은 시험 회사가 아닌 기본 설정 목록에서 채워질 수 있어 실제 회사를 건드리지 않고는 확인하지 않았다.
4. 앱 코드 변경 없음(지시서 규칙). 수정은 별도 작업이다.

milestone 18f33a6c 는 4항목이 모두 확보되지 않아 report_milestone_done 을 호출하지 않았다.
`expires_in_hours` 는 서버에 하한 검증이 없다(음수 허용, 화면은 min=1). 이번에는 owner 가 막혀 쓰지 못했다.

## 되돌리는 법 (삭제 없음)

카페24 114 에서 실행. 시험 회사 비활성, 로그인 회수:

    docker exec acct-pg psql -U acct_admin -d obys -c "UPDATE yeoljeong_businesses SET deleted_at=NOW() WHERE id IN ('biz-e2e-m1-a','biz-e2e-m1-b'); UPDATE yeoljeong_branches SET deleted_at=NOW() WHERE id IN ('br-e2e-m1-a-1','br-e2e-m1-b-1');"
    docker exec acct-pg psql -U acct_admin -d obys_auth -c "UPDATE saas_users SET status='suspended', is_active=false WHERE id IN ('3feb4d60-e85f-41b5-8b55-8d7e3aebaf03','bc6416b8-bab5-4718-8d83-efa8e67277e0','7219023d-d214-4a2a-b5dc-2a84d7c4bd65'); UPDATE tenants SET status='suspended' WHERE id IN ('1bbc4f60-a1e6-462f-9a11-e3c52de4b739','cf4d829b-a36c-4af2-8499-87a564267f9f','514326e0-919f-4831-a714-8acf644d4351');"

Vault 비활성 (contabo116):

    docker exec aads-server python3 -c "
    import asyncio
    from app.core.db_pool import init_pool, get_pool
    async def m():
        await init_pool()
        await get_pool().execute(\"UPDATE e2e_credentials SET is_active=FALSE WHERE service='obys-e2e'\")
    asyncio.run(m())"

(가입요청 2건은 시험 테넌트 소속 pending 으로 남는다. 회사 비활성 후 목록에서 제외된다.)
