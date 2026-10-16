# 오비서 U1 첫 슬라이스 재적용 결과

document_key: aads-obys-ui-first-slice-rebase

## 범위 및 기존 구현 분류

- `admin.html`의 메뉴 설정 및 `renderNav`: 수정. 활성 멤버십 기준 역할에 따라 제한 메뉴를 표시한다.
- `admin.html`의 급여 설정 및 `liveRow`: 수정. 총지급액은 `gross_pay`, 공제는 세 공제 항목 합계, 실수령은 `net_pay`에서 표시하며 기본급·수당은 미산출로 표시한다.
- `loadReadPermissionV41`, `startWorkspaceV41`, 복구/재시도 연결: 수정. 기존 인증 조회 경로를 사용하고 권한 조회를 재시도한다.
- 기존 조직·사업자·급여 API 접점과 직원 미리보기 권한: 유지.
- `test_obys_menu_payroll_slice.py`: 신규 회귀 테스트.
- 삭제: 없음. 호출처 제거 없음.

## 기준점 및 적용

작업 디렉터리는 깨끗했고 `origin/main`과 HEAD는 `67cca474b5011cee1140fb3c90de5432ea3d8a23`였다. 지시서에 기재된 `8ac23f6a`보다 앞선 기준점이다. 금지 명령 제한에 따라 새 worktree, commit, push는 수행하지 않았다. 승인된 커밋 `80a60ebc`에서 대상 두 파일의 변경만 적용했고 `RESULT.md`는 변경하지 않았다. 계약 PDF 관련 변경은 손대지 않았다. 활성 러너 프로세스는 확인되지 않았다.

## 검증 결과

- `bash scripts/run_unit_tests.sh tests/unit/test_obys_menu_payroll_slice.py`: 실패. 테스트 러너가 기준 컨테이너 `aads-server`를 찾지 못함.
- 로컬 `python3 -m pytest -q tests/unit/test_obys_menu_payroll_slice.py tests/unit/test_obys_v41_contract_editor.py tests/unit/test_obys_employee_preview_menu.py tests/unit/test_obys_clock_menu_link.py`: 37 passed, 1 skipped. 기존 pytest 설정의 `asyncio_mode` 미지원 경고 1건.
- `git diff --check`: 통과.
- 기준 대비 변경 대상: `app/static/apps/obys/admin.html`, `tests/unit/test_obys_menu_payroll_slice.py`, 본 보고서.
- 화면 미검증(배포 전). desktop/390px 캡처 및 직원 토큰 API 403 검증은 수행하지 않았다.
- 정본 미등록(미완료): project_document_heads 초안 등록과 열람 참조를 이 환경에서 수행하지 못했다.

## 후속 자동화용 상태

이 결과는 아직 origin/main에 커밋되어 있지 않으며 원격 브랜치 포함 여부는 확인되지 않는다. 롤백은 반영 커밋이 생긴 뒤 해당 커밋을 revert 하면 된다.
