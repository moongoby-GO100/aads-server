# ACCT S2 확정 릴리스 검증 (acct-s2-certified-release-verify)

- 정본 문서 식별자 — `acct-s2-certified-release-verify` (kind=report, 정본 초안 대상)
- 작업: AADS-OBYS-S2-CERTIFIED-RELEASE-VERIFY-20261116-R3
- 작업 카드 ID: `e402d013-1ff4-4f8d-9d14-deff43f527ae` (GOAL_ID: `a9ada8e5-9479-4064-b817-9100308d875c`, MILESTONE_ID: `186cb4ad-c35d-4f84-9bfe-218a417f405b`)
- 재작업 계보: `runner-ea77c158` → `runner-2524705c` → `runner-4a144e13` (범위 축소 재설계)
- 목표: 현재 운영 인증 릴리스 `#6160`(`2ab9748c`) 기준 S2 확정 SHA 검증 — 보안 보완 SHA 4종 조상 포함 확인 및 Node 환경 포함 통합 테스트 3개 파일 실측 결과 기록
- 실측 시각: 2026-11-16 14:41 KST, 실행 환경: contabo116 (`/tmp/aads-wt-runner-4a144e13`)
- 정본 등록 상태: 정본 미등록(미완료), 열람 링크 미제공 (서버가 push 이후 정본 등록 수행)

---

## 1. 개요 및 배경

1. **작업 성격 및 범위 축소**:
   - 본 작업은 업무 카드 `e402d013`("보안 보완 SHA 포함 확인·Node 포함 통합 테스트")에 국한된 **검증 전용 보고서 작성 작업**이다.
   - 후보 health 검증, 동일 이미지 다이제스트(same-digest) 검증, 배포 후 300초 P0/P1 감시, 로그인/서명 E2E 브라우저 테스트는 다른 업무 카드의 소관이므로 본 작업 범위에서 제외하며 **미검증**으로 처리한다.
   - 산출물은 본 보고서 1파일뿐이며, `app/` 및 `tests/` 코드는 일체 수정하지 않는다.

2. **기준 릴리스 정보**:
   - 운영 ACCT/app 환경의 확정 릴리스: `deploy_runs` `#6160`
     - `release_sha`: `2ab9748ce3a056b91b340b24f5e756122fbf21de` (단축 `2ab9748c`)
     - 상태: `success`, `phase`: `release_certified`
     - 배포 시각: 2026-11-11 16:27 KST
     - 롤백 플랜: 직전 릴리스 `7028bc0d`로 블루그린 롤백
   - HEAD 검증: `git merge-base --is-ancestor 2ee3217b HEAD` 확인 결과 `rc=0` (HEAD가 테스트 2건 수정 커밋 `2ee3217b`를 정상 포함).

---

## 2. 보안 보완 SHA 4종 포함 여부 검증 (실측)

운영 확정 릴리스 `2ab9748c`가 필수 보안 패치 커밋 4종을 조상으로 모두 포함하고 있는지 `git merge-base --is-ancestor` 명령으로 실측 검증했다.

- 검증 명령: `git merge-base --is-ancestor <sha> 2ab9748ce3a056b91b340b24f5e756122fbf21de; echo rc=$?`

| 보안 SHA (단축) | 실측 rc | 커밋 요약 | 2ab9748c 조상 여부 |
|---|:---:|---|:---:|
| `2842a769` | `0` | Pipeline-Runner: runner-fa19cec7 | **포함 (조상 확인)** |
| `8c17bd62` | `0` | Pipeline-Runner: runner-8b8bfa65 | **포함 (조상 확인)** |
| `cda45fdc` | `0` | Pipeline-Runner: runner-762594f7 | **포함 (조상 확인)** |
| `5c6937d7` | `0` | fix(pipeline-runner): runner-f765cd5f — [P1/S] 오비서 V4.1 근태 현황 — 기본 출근·결근 수정 | **포함 (조상 확인)** |

- **판정 결과**: 4개 보안 SHA 모두 `rc=0`으로 확정 릴리스 `2ab9748c`의 조상 계보에 온전히 포함되어 있음을 실측 확인했다.

---

## 3. 필수 테스트 파일 목록 조사 결과

- **조사 목적**: 마일스톤 `completion_criteria`에 명시된 "필수 14파일" 목록을 식별하기 위해 저장소 내 공식 문서 대조.
- **조사 범위**: `docs/reports`, `docs/plans`, `docs/specs` 내 전체 문서에서 'S2'와 '필수', '14개', '15개', '14파일', '15파일' 키워드 전수 검색.
- **실측 결과**:
  - 저장소 문서 어디에도 S2 오비서 릴리스 검증용 "필수 14파일" 목록이 정의되어 있지 않음.
  - 마일스톤 메타데이터 및 DB에도 해당 목록이 부재함.
- **판정**: **목록 미확정(미검증)**.
  - 따라서 근거 없는 14개 또는 15개 파일 목록 전체를 자의적으로 '충족'으로 단언하지 않고, 지시서가 특정한 3개 핵심 테스트 파일의 실측 결과만 명확히 기록한다.

---

## 4. 단위 테스트 실측 결과 대조

### 4.1 2ab9748c 대비 최신 origin/main(2ee3217b) 실측 대조표

- **실측 검증 명령**:
  ```bash
  bash scripts/run_unit_tests.sh -rs \
    tests/unit/test_obys_workspace_route_roles.py \
    tests/unit/test_obys_attendance_api.py \
    tests/unit/test_obys_employee_portal.py
  ```
- **Node 환경**: `[run_unit_tests] node: mounted v22.23.4` 정상 마운트 확인.

| 테스트 파일명 | 2ab9748c 결과<br>(출처: CTO 실측 2026-11-16) | origin/main (2ee3217b 이후) 결과<br>(실측 2026-11-16 14:41 KST) | 판정 및 변화 |
|---|:---:|:---:|:---:|
| `tests/unit/test_obys_workspace_route_roles.py` (121개) | 1 failed / 120 passed (rc=1) | **121 passed** (0 failed, rc=0) | **해결 (통과)** |
| `tests/unit/test_obys_attendance_api.py` (71개) | 71 passed (0 failed, rc=0) | **71 passed** (0 failed, rc=0) | **유지 (통과)** |
| `tests/unit/test_obys_employee_portal.py` (12개) | 1 failed / 11 passed (rc=1) | **12 passed** (0 failed, rc=0) | **해결 (통과)** |
| **합계 (3개 파일)** | **2 failed / 202 passed (rc=1)** | **204 passed / 0 failed (rc=0)**<br>(1 warning, 소요시간 17.69s) | **전체 통과 (정상 복구 확인)** |

> [!NOTE]
> `test_obys_attendance_api.py` 실행 시 발생하는 1건의 경고는 Starlette 라이브러리 내부의 `anyio.abc.BlockingPortal` deprecation 관련 경고(`DeprecationWarning`)이며, 테스트 성공 여부에는 영향을 주지 않는다.

### 4.2 2ab9748c 실패 2건의 상세 원인 및 수정 커밋 내역

1. **`test_obys_employee_portal.py::TestStaticWiring::test_portal_uses_only_existing_api_paths`**:
   - **실패 증상**: `AssertionError: assert paths == {"/contracts/signing", "/contracts/${encodeURIComponent(contractId)}/signing-view"}` 단언 실패 (`Extra items in the left set: '/me/resignation'`).
   - **발생 원인**: 커밋 `5f7490ad`(runner-de9479f6, 오비서 퇴사·계약해지 처리)에서 서버 `app/api/obys_finance.py:575` 및 프론트엔드 포털 스크립트에 `'/me/resignation'` API 라우트를 추가했으나, 기존 정적 검사 테스트가 이전의 2개 경로만 허용하고 있어 불일치가 발생했다.
   - **수정 내역**: 커밋 `2ee3217b`(runner-cc2e2734)에서 테스트 허용 집합에 `'/me/resignation'`을 추가하여 정합성을 일치시켰다. (앱 코드는 수정하지 않고 테스트만 정정)

2. **`test_obys_workspace_route_roles.py::test_member_records_are_own_rows_only_and_filtered_in_sql`**:
   - **실패 증상**: `AssertionError: assert ['att-3', 'att-1'] == ['att-1', 'att-3']` 순서 불일치 실패.
   - **발생 원인**: 커밋 `2b45779e`의 `_merge_people_rows`에서 `(work_date, start_at, id)` 역순 정렬 로직이 적용됨에 따라 반환되는 id 순서가 `['att-3', 'att-1']`로 변경되었으나, 기존 테스트 코드는 정렬 순서에 의존하는 리스트 동등 비교(`['att-1', 'att-3']`)를 기대하고 있어 단언 오류가 발생했다.
   - **수정 내역**: 커밋 `2ee3217b`(runner-cc2e2734)에서 순서 무관 집합 비교(`{r["id"] for r in result["records"]} == {"att-1", "att-3"}`) 및 반환 건수 검증(`len(result["records"]) == 2`)으로 테스트 단언 방식을 안전하게 개선했다. (앱 코드는 수정하지 않고 테스트만 정정)

---

## 5. 범위 외 항목 및 미검증 상태 명시

| 항목 | 상태 | 내용 및 조치 계획 |
|---|:---:|---|
| **필수 14개 테스트 전체** | **목록 미확정 (미검증)** | 문서상 14개 파일 목록 부재로 3개 파일 외에는 검증하지 않음. 별도 업무 카드를 통해 14개 파일 목록을 확정한 뒤 전수 검증 필요. |
| **후보 컨테이너 Health** | **미검증 (범위 외)** | `deploy.sh bluegreen` 후보 슬롯 헬스 체크 절차로, 배포 파이프라인 업무 카드의 소관. |
| **Same Digest 검증** | **미검증 (범위 외)** | 대기 슬롯 동기화 이미지 다이제스트 일치 검증은 배포 검증 카드의 소관. |
| **300초 P0/P1 감시** | **미검증 (범위 외)** | 배포 컷오버 후 5분간 실시간 모니터링 절차로, 배포 자동화의 소관. |
| **로그인 / 브라우저 E2E** | **미검증 (범위 외)** | 브라우저 E2E 검증 카드의 소관. |
