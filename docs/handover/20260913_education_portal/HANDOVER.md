# AADS 교육자료 포털 통합 — 2026-09-13

## 범위

- 신규 AI 교육자료 12건을 `app/static/reports/index.html`의 교육자료 목록에 등록했다.
- 문서 수 집계를 하드코딩 값에서 `docs` 배열 기준 동적 집계로 변경했다.
- 로그인된 대시보드에서는 `/api/v1/project-docs/scan` 결과에서 신규 `*_education.html` 파일을 자동 병합한다.
- 스캔 API가 인증되지 않거나 실패해도 검증된 정적 목록 18건을 즉시 렌더링한다.

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
- 자동 발견은 동일 출처의 문서 스캔 API를 사용할 수 있는 로그인 세션에서 동작한다. 공개 비로그인 접근은 정적 목록 폴백을 사용한다.

## Git

- `59ea54f0` — 신규 12건 포털 등록
- `ec112a54` — 포털 커밋에 섞인 비관련 autoheal 변경 제거
- `927f9b62` — 교육자료 자동 발견
- `23794709` — 스캔 대기 중 빈 화면 방지

---

# AADS-EDU-PORTAL-AUTO-PUBLIC — 공개 자동반영 인증 결함 수정 (2026-09-13)

## 문제

위 "남은 작업"의 마지막 줄이 그대로 결함이었다. 포털은 `/api/v1/project-docs/scan` 을
호출하는데 이 경로는 JWT 미들웨어가 막는다(비로그인 401 실측). 그래서 **공개 열람자에게는
자동 발견이 전혀 동작하지 않고** 하드코딩 목록만 보였다. 즉 새 `*_education.html` 을 올려도
`index.html` 을 같이 고치지 않으면 공개 포털에 나타나지 않는다.

`/project-docs/scan` 자체를 공개하는 선택지는 배제했다. 이 엔드포인트는 AADS 외 프로젝트의
원격 파일 메타데이터(절대경로·호스트 포함)까지 돌려준다.

## 조치

교육자료 전용의 좁은 읽기전용 계약을 새로 뚫었다. 기존 구현은 **대체하지 않고 추가만** 했다.

- `app/api/project_docs.py` — **신규** `GET /project-docs/public-education-index`.
  AADS `app/static/reports` **바로 아래**의 일반 파일 중
  `^[A-Za-z0-9._-]+_education\.html$` 만 노출하고 `file/title/date/size` 4개 키만 돌려준다.
  절대경로·호스트·본문·디렉토리 목록·쿼리로 바꾸는 조회 경로는 없다. 재귀하지 않고,
  심볼릭 링크는 디렉토리 밖을 가리킬 수 있어 제외한다. 날짜 내림차순 → 파일명 내림차순으로
  결정적 정렬. 기존 `scan`/`content` 및 헬퍼는 그대로 유지했다(수정·삭제 없음).
  제목은 **파일명에서만** 만든다 — 포털이 제목을 innerHTML 로 렌더링하므로 문서 본문의
  `<title>` 을 읽으면 임의 마크업이 주입될 수 있다.
- `app/main.py` — **신규** `_PUBLIC_EXACT_PATHS` 집합에 그 경로 하나만 넣고 미들웨어 1단계에
  정확일치로 추가했다. prefix 면제(`_AUTH_EXEMPT_PREFIXES`)는 건드리지 않았다 — 거기에
  `/api/v1/project-docs` 를 넣으면 `scan`·`content` 까지 같이 열린다.
- `app/static/reports/index.html` — 기존 `mergeAutoEducationDocs()` 의 **호출 대상만** 좁은
  엔드포인트로 바꿨다. 하드코딩 18건은 오프라인·실패 폴백으로 그대로 두고, 파일명 기준
  중복 제거, 클라이언트에서도 같은 허용목록 정규식으로 한 번 더 거른다. `autoTitle()` 등
  기존 함수는 유지.

## 검증

- 단위 테스트 **53 passed** — `tests/unit/test_project_docs_viewer.py`(기존 9건 포함) +
  신규 `tests/unit/test_education_portal_public_index.py`.
  비로그인 정확경로 200 / `scan`·`content` 401 / 트레일링슬래시·`..`·유사경로 비면제 /
  숨김·traversal·비교육 파일명 차단 / 응답에 절대경로 없음 / 별칭 중복제거 / 심볼릭 링크 제외.
- 실제 디렉토리 대상 동작 확인: 교육자료 **18건 → 신규 1건 생성 후 19건**, 최신순 선두 노출.
  같이 만든 비교육자료·숨김파일은 제외됨. 확인 후 임시 파일 삭제.
- 포털 JS 를 node 로 실제 실행해 병합 검증: edu 18 → 19, 기존 항목 중복 없음,
  `../evil_education.html` 은 클라이언트에서 차단.
- `ruff --select F821,F811`(pre-commit 게이트 규칙) 통과. 신규 엔드포인트 코드는 전체 룰셋에서도 지적 0건.
- 정적 HTML 검사: 참조하는 DOM id 실재, 카드 앵커 `href="${d.file}"`, 폴백 목록 표본 3건 잔존.

## 공개 경로 (중요)

- 공개 포털은 `https://aads.newtalk.kr/education/index.html` 이다.
  nginx 가 `/education/` → `aads_api/static/reports/` 로 프록시한다.
  `aads.newtalk.kr/static/reports/index.html` 은 **404** — `/static/` location 이 없어
  `location /`(대시보드)로 떨어진다. 검증 URL을 여기서 틀리기 쉽다.
- `fb.newtalk.kr/static/reports/index.html` 도 200 이지만 **다른 앱**이다.
  현행 `/etc/nginx/conf.d/fb.conf` 는 `/api/v1/`·`/static/` 을 `yeoljeong_finance_api` 로
  보낸다. 그래서 그 사본에서는 이 수정이 닿지 않고 폴백 목록만 보인다(의도된 범위 밖).

## 미배포 / 남은 작업

- 본 변경은 **승인 대기** 상태다. 빌드·배포·재시작은 수행하지 않았다.
- `app/static/reports/` 는 배포 없이 즉시 서빙된다. 따라서 index.html 만 먼저 반영되면
  API 가 없는 동안 401 → 폴백으로 동작한다(화면 깨짐은 없으나 자동반영은 배포 후 동작).
  index.html 과 API 는 같은 릴리스로 함께 나가야 한다.

## 동시 작업 경고

같은 TASK_ID 를 **4개 세션이 동시에** 잡았다(03:29~03:41). 커밋 전 반드시 확인할 것.

- 본 작업: 격리 워크트리 `/root/aads/.worktrees/edu-portal-public-opus`,
  브랜치 `fix/edu-portal-public-index-opus5`. 면제 집합 이름 `_PUBLIC_EXACT_PATHS`.
- 다른 세션이 **공유 메인 워크트리** `/root/aads/aads-server` 에 동일 4파일을 staged 해 두었다.
  면제 집합 이름 `_AUTH_EXEMPT_EXACT_PATHS` 로 구분된다. 메인에서 `git commit` 하면 그쪽
  작업이 딸려 들어간다.
- codex 파이프라인 러너: `/tmp/aads-wt-runner-bffcddbf`.
- 네 번째 세션에는 stand down 회신함.
