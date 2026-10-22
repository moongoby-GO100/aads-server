# 오비서 M1 직원 화면 설정 게이트 403 해소 결과

문서 키는 obys-m1-employee-settings-gate 이다. 상태는 초안이다.
작업 ID는 OBYS-M1-EMPLOYEE-SETTINGS-GATE-20261022 이다.

## 요약

- 직원 E2E(m1-staff2) FAIL 은 두 원인이 겹친 결과였다.
  1. 실제 결함: 직원 화면이 로그인 직후 레거시 `GET /settings` 를 불러 403 을 받고 "기초설정 불러오기 실패" 토스트를 띄웠다.
  2. 검증 스크립트의 낡은 판정: 2026-10-15 이후 직원은 `#onboardingView` 가 아니라 직원 포털(`#employeePortal`)로 들어가고 `#appShell` 은 숨겨진다. 스크립트는 `#onboardingView` 가시성과 `h1,h2,.panel-title` 만 봐서 포털을 인식하지 못했다.
- 게이트(`_TENANT_SCOPED_PREFIXES`·화이트리스트)에 `/settings` 를 추가하지 않았다. 백엔드는 변경 없음.

## STEP 0 판정표 — 직원 화면이 /settings 를 쓰는 곳

| 항목 | 위치(index.html) | 직원이 쓰는가 | 판정 |
|---|---|---|---|
| loadServerSettings 로그인 직후 호출 | 로그인 성공 핸들러, 회원가입 핸들러, init | 아니오 — 결과는 사업자·지점·계좌 선택용 state.settings | 수정 |
| reloadDataAfterTenantSwitch | 사업자 전환 후 재조회 | 직원은 전환 UI 없음 | 유지(함수 내부 가드로 보호) |
| persistSettingsToServer (PUT /settings) | canManageSettings() 일 때만 | 아니오 | 유지 |
| hasMeaningfulSettings | 응답 형태 검사 | - | 유지 |
| 직원 포털 데이터 | 계약서·급여명세·입사서류는 /contracts, /payroll, /onboarding/documents 에서 로드 | /settings 불필요 | 유지 |

## STEP 0 판정표 — 직원이 계약서·서류 화면으로 가는 조건

| 단계 | 근거 | 결과 |
|---|---|---|
| 로그인 후 V4.1 이동 여부 | shouldOpenV41AppVerified → /session 확인 후 shouldOpenV41App, 직원이면 false | 직원은 index.html 유지(E2E 에서도 확인) |
| 기본 화면 | defaultView() 가 직원이면 "onboarding", allowedViews 는 employeeViewNames | onboarding 뷰가 현재 뷰가 됨 |
| 실제 표시 | employeePortalActive() 이면 `#employeePortal` 이 보이고 body.ep-on 이 `#appShell` 을 display:none | 직원은 포털 홈을 본다. 홈 카드와 상단 탭에 계약서·입사서류 진입이 있다 |
| E2E 스크린샷(staff-pc.png) | "안녕하세요 … 직원", 계약서 서명 대기, 급여명세, 입사서류 0/4 제출 카드와 상단 탭 | 계약서·서류 진입은 보였다 |

판정: onboarding 이 안 보인 이유는 설정 실패 때문이 아니라 기본 화면 라우팅(포털이 onboarding 뷰를 가림) 때문이다. 스크립트의 `onboarding_visible` 은 `#appShell` 숨김으로 항상 false, `headings` 는 포털이 h3 를 써서 항상 빈 배열이었다. 설정 403 토스트는 별개의 실제 UX 결함이고, 스크립트 판정은 `error_text` 를 보지 않았다.

## 변경 내역 (함수 이름 변경·삭제 없음)

| 대상 | 분류 | 내용 |
|---|---|---|
| app/static/apps/obys/index.html `loadServerSettings` | 수정 | 직원 계정(isEmployeeAccount)이면 /settings 를 부르지 않고 null 반환. 설정 권한 없는 계정의 403 은 null 반환(토스트 없음, 빈 설정으로 진행). 관리자 403·기타 오류는 종전처럼 예외 전파 |
| scripts/verify_obys_m1_employee_entry_e2e.py | 수정 | portal_visible·portal_entries 수집, 직원은 onboarding 뷰 또는 포털의 계약서·서류 진입 노출이면 통과, "기초설정 불러오기 실패" 토스트가 있으면 FAIL |
| tests/unit/test_obys_employee_settings_gate.py | 신규 | 아래 테스트 |
| app/core/obys_tenant.py, yeoljeong_finance_service.get_settings | 유지 | 변경 없음. /settings 는 신규 테넌트에게 계속 403 |
| admin.html, 급여·계약 서명·세금 영역 | 유지 | 변경 없음 |

지시서에 명시되지 않은 파일(verify 스크립트)을 바꾼 사유: 스크립트가 직원 포털을 몰라 현재 정상 화면을 FAIL 로 판정했고, 설정 실패 토스트는 판정하지 않았다. 되돌리기는 해당 커밋 revert 로 충분하다.

## 보안 검토

레거시 전역 settings(사업자·지점·계좌·직원·연동)는 신규 테넌트에게 노출되지 않는다. 게이트는 그대로이고, 클라이언트가 직원일 때 호출을 생략한다. 클라이언트 가드는 UX 용이며 보안 경계는 서버 403 이다.

## 테스트

- 신규 test_obys_employee_settings_gate.py: 신규 테넌트(직원·관리자) GET/PUT /settings 403, 레거시 테넌트 통과, /settings 가 prefix·화이트리스트에 없음, loadServerSettings node 동작 7개 케이스, 검증 스크립트 판정 5개 케이스.
- run_unit_tests.sh(운영 이미지)로 신규 파일 + test_obys_employee_skip_v41, test_obys_employee_portal, test_obys_tenant_session_employee, test_obys_onboarding_docs_employee_read, test_obys_tenant_gate: 83 passed, 10 skipped. skip 은 컨테이너에 node 가 없어서다.
- node 의존 테스트는 호스트(node v20)에서 따로 실행: 신규 loadServerSettings 동작 테스트 통과, 검증 스크립트 판정 테스트 통과, test_obys_employee_portal·employee_skip_v41·index_pwa_install·release_assets 60 passed.
- 전체 `bash scripts/run_unit_tests.sh` 는 1200초 상한(rc=124)에 걸려 끝나지 못했다. 전체 통과로 적지 않는다.
- obys·index.html·obys_tenant 를 참조하는 68개 테스트 파일을 따로 실행한 결과, 실패는 이 변경과 무관한 것만 남았다.
  - test_obys_contract_pdf_preview.py: 컨테이너에 fitz·fontTools 모듈이 없음(ModuleNotFoundError).
  - test_obys_employee_tenant_membership.py 2건: 백엔드 signing-view 라우트 매칭 단언. 백엔드를 바꾸지 않았다.
  - test_yeoljeong_finance_api.py 2건: '최신양식 v2026.07.23' 문구가 HEAD 의 index.html 에도 없음(HEAD 0건, 변경 후 0건).
  - test_yeoljeong_purchase_inventory_o3.py 1건: 매입·재고 문구 단언. 이 변경이 건드린 줄이 아니다.
  - 이 실패들이 변경 전부터 있었는지는 git checkout 이 금지돼 변경 전 트리로 재실행하지 못했다. 위는 원인 메시지와 HEAD 대조에 근거한 판단이다.
- git diff HEAD 에서 삭제된 def/class/function 선언 줄: 0건.
- ruff F821,F811: 신규 테스트·검증 스크립트 통과.
- 운영 반영과 E2E 재실행은 별도 릴리스 작업이다.
