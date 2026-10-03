# R-DOC 목업 필수 제출·승인 — 검토 패키지 검증

판정: 문서 초안·목업 제출 완료. 운영 승인 기능과 강제 게이트는 설계 상태다.

## 대상과 범위
- 프로젝트 AADS, 목표 `0361c451-cc03-4bd1-a423-76051b0546b2` 연결. 목표 자체의 기존 blocked 상태는 변경하지 않았다.
- 기획·PRD·설계 문서 버전 `1.0.1`, 화면 목업 v1. 신규 검토 화면 MR-01이므로 실제 Before는 해당 없음이다. 기존 Workbench·채팅 화면은 변경하지 않았다.
- 미리보기: https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/index.html
- 문서 열람: 같은 디렉터리의 `plan.html`, `prd.html`, `spec.html`. 게시본은 초안 열람 사본이며 DB 정본과 승인 상태를 대체하지 않는다.
- 정적 게시 대상은 `/var/www/certbot/screenshots/rdoc-mockup-review-v1/`뿐이다. 기존 nginx 설정을 사용했고 앱 배포·재시작을 하지 않았다. 철회가 필요하면 이 새 디렉터리를 웹 루트 밖으로 이동해 보존할 수 있다.

## 실행 근거
- `python3 /tmp/rdoc_mockup_verify.py`: 로컬 Playwright Chromium으로 전체 화면 캡처, 빈 수정 요청 거절, 수정 요청→재제출→재확인, 승인 대상 버전 표시, 실제 Before 누락 차단, 주요 실패 상태·입력 보존, 화면 폭별 넘침 검사를 수행했다. 재실행 사본은 `evidence-v1/verify_preview.py`다.
- `evidence-v1/verification.json`: 로컬 검사 결과와 서버에서 실측한 KST 기록.
- 서버 Playwright `browser_navigate`→`browser_snapshot`: 공개 URL에서 제목·문서 링크·승인 대상·시연 안내 확인.
- 공개 URL Playwright 실행: 문서 링크 HTTP 성공, 데스크톱·모바일 렌더, 수정 요청→새 버전 승인 시연을 확인했다. 로그인은 사용하지 않았다. `evidence-v1/public-verification.json`과 `public-desktop.png`, `public-mobile.png`에 증거 보존.
- 원본 HTML과 게시 파일의 SHA-256은 일치한다. CDN은 응답에 자체 요소/스크립트를 추가하므로 HTTP 응답 전체 해시는 원본과 다르다. 공개 응답 내 원래 CSS/JS가 유지됨을 별도 검사했다. 이 시안에 운영 승인용 해시 검증 기능이 있다고 주장하지 않는다.
- 최초 파일 다운로드 API는 무인증 HTTP 401. 대체 검증에서 API health HTTP 200과 nginx/uvicorn 프로세스를 확인한 뒤, 실제 공개 미리보기 브라우저 검증까지 완료했다. 최초 `/gallery/` 후보는 HTTP 500으로 채택하지 않았다.

## 정본 등록 확인
`canonical_document_register`로 초안만 등록하고 `query_database`로 최신 리비전·목표·본문 해시를 재조회했다. 아래 본문 해시는 로컬 Markdown의 SHA-256과 일치한다.

| 문서 키 | 최신 revision | revision id | 본문 SHA-256 |
|---|---|---|---|
| rdoc-mockup-review-plan | 2 | 16465cbf-3879-4f25-863f-6a3014af30ad | d3f016d31b5378980235a166e0e543e5e4d2e25462220afa7c0870aa4fc5d1f2 |
| rdoc-mockup-review-prd | 1 | 80e4a82e-05f2-412a-bbe7-cbfb54a3d01f | 33929843b1b671787858649de180f1761d3e54e70885d160a4053af8ba947686 |
| rdoc-mockup-review-spec | 1 | 4af12543-e1f9-4407-9785-7040faedb469 | 4563a3b64ccda5de4c55e43b86d3b0467b4cf3b543cca441cdacb57b3e1b40df |

[출처: canonical_document_register, query_database, 로컬 hashlib 대조]

모든 `approved_revision_id`는 null이다. 파일 경로 등록이 `invalid_source_path`로 거절돼 동일 본문을 content로 등록했다. 따라서 DB `source_path`는 null이며, 원본 파일 경로는 change_summary 및 manifest에 남겼다. 이 제한을 파일 경로 등록 성공으로 표현하지 않는다.

## 미구현과 다음 검증
- 서버 필수 제출 검사, 버전별 승인 저장, API/Runner/CLI 실행 차단, 전 세션 L1 적용은 미구현/미검증이다. 문서 작성 완료를 운영 강제 완료로 보고하지 않는다.
- 시안의 승인·권한·세션 복구는 메모리 내 동작 예시다. 실제 인증·권한·DB·실행 경로는 구현 후 PRD AC-M01~12로 검증해야 한다.
- 자동 시각 diff, 접근성 인증, AAG 최신 대조는 수행하지 않았다. 구현 착수 전에 대조하고 실제 구현의 접근성·권한·회귀 검사를 별도 수행한다.
- 문서와 목업 검토 후 고정 리비전에 대한 실제 승인, 이어서 실행 게이트와 검토 화면 구현 범위를 확정한다. 포괄적 진행 지시를 시안 승인으로 추정하지 않는다.
- 유료 외부 디자인/LLM 호출을 추가 실행하지 않았다. 세션 전체 비용은 $ 미측정이다.
