# AADS 교육자료 포털 통합 — 2026-09-13

## 범위

- 신규 AI 교육자료 12건을 `app/static/reports/index.html`의 교육자료 목록에 등록했다.
- 문서 수 집계를 하드코딩 값에서 `docs` 배열 기준 동적 집계로 변경했다.
- 공개 전용 `/api/v1/project-docs/public-education-index` 결과에서 신규 `*_education.html` 파일을 자동 병합한다(`92eb5f8f` 이후. 그 이전에는 인증이 필요한 `/project-docs/scan` 을 호출해 비로그인에서 401 이었다).
- 자동발견 API가 실패하거나 아직 배포되지 않았어도 검증된 정적 목록 18건을 즉시 렌더링한다.

## 사용자 경로

- 포털: `https://aads.newtalk.kr/education/index.html`
- 대시보드 사이드바: `교육자료 포털` (관리자 메뉴)

## 검증

- 신규 12개 파일 존재 및 개별 공개 URL HTTP 200 확인.
- 포털 공개 URL HTTP 200, 신규 12개 링크 포함 확인.
- Chromium 1,440×1,200 렌더에서 전체 47건·교육자료 18건 및 신규 카드 노출 확인.
- 스캔 API mock에서 미등록 교육자료 1건 자동 병합, 전체 48건·교육자료 19건 확인.
- `git diff --check` 통과.

## 남은 작업

- 신규 12건 중 P0 4건(프롬프트·보안·평가·법률)은 1차 출처 보강 완료.
- 나머지 8건은 출처 품질 전수검수가 미완료다. 원격 `origin/main` 기준으로 외부 URL이 없는 문서 6건, URL 1건만 있는 문서 2건이다.
- 자동 발견은 `92eb5f8f` 배포 후 비로그인 공개 방문자에게도 동작한다. 배포 전까지, 그리고 API 실패·오프라인 시에는 정적 목록 폴백을 사용한다.

## Git

- `59ea54f0` — 신규 12건 포털 등록
- `ec112a54` — 포털 커밋에 섞인 비관련 autoheal 변경 제거
- `927f9b62` — 교육자료 자동 발견
- `23794709` — 스캔 대기 중 빈 화면 방지
- `18835536` — P1 교육자료 8건 출처 121건 검수·보강
- `92eb5f8f` — 공개 전용 `public-education-index` 엔드포인트 + exact-path 인증 예외
- `0bafdeb5` — 인증 면제를 실제 미들웨어 행위로 검증하는 테스트

## AADS-EDU-PORTAL-AUTO-PUBLIC 검증 보강 (2026-09-13)

`92eb5f8f`(타 세션 선행 구현)를 인수 검증하고 결함 1건을 수정했다.

### 수정

- `app/static/reports/index.html` — 같은 응답에 동일 파일명이 두 번 오면 카드가 두 번
  렌더되던 중복 제거 결함 수정. `known.add()` 가 `.filter()` 완료 후에 실행되어
  응답 내 중복에는 무력했다. 중복 검사를 `unshift` 직전 루프 안으로 이동.
- `tests/unit/test_project_docs_viewer.py` — `0bafdeb5`(타 세션)가 추가한 미들웨어
  동작 테스트 위에 2건을 덧붙였다. 중복 픽스처를 만들지 않고 기존 `middleware_client`
  를 재사용한다.
  - `test_sibling_project_doc_routes_still_require_auth` 에 prefix 변형 2건 추가
    (`…public-education-index/extra`, `…public-education-index2`)
  - `test_public_education_index_response_leaks_no_filesystem_location` (신규)
  - `test_portal_merges_discovered_docs_and_keeps_static_fallback` (신규)

### 검증 결과

- 단위 테스트 24/24 통과 (`scripts/run_unit_tests.sh`).
- 비인증 `GET /api/v1/project-docs/public-education-index` → **200**,
  응답 키는 `basename/title/date/size` 뿐. 절대경로·`full_path`·`host` 미포함.
- 비인증 `/project-docs/scan`, `/project-docs/content`,
  `/public-education-index/extra`, `/public-education-index2` → 전부 **401**
  (면제가 prefix 로 새지 않음).
- 실제 `app/static/reports` 대상 E2E: 신규 `_education.html` 1건 생성 시
  index.html 수정 없이 18→19건 자동 노출. 숨김(`.hidden_…`)·비교육(`_notes.html`)·
  `.bak` 파일은 미노출. 최신순 정렬 확인.
- Chromium 1,280×1,400 렌더: 전체 48건·교육자료 19건, 신규 카드 최상단 노출,
  중복 앵커 0건, 콘솔 에러 0건.
- 정적 HTML 검사: 필수 id 5종 존재·중복 없음, `getElementById` 참조 6종 전부 해소,
  태그 균형 정상, `project-docs/scan|content` 호출 없음.
- 직접 슬롯 헬스: `127.0.0.1:8100`, `127.0.0.1:8102` 모두 HTTP 200.
  두 슬롯 모두 신규 경로는 **401** — 실행 이미지 `c8459cc66258` 가 `92eb5f8f` 이전이라
  예상된 결과이며, **배포 전까지 공개 자동발견은 정적 폴백으로 동작**한다.

### 남은 조건

- 공개 반영은 `deploy.sh bluegreen` 배포 후에만 성립한다. 배포는 CEO 승인 대기.
