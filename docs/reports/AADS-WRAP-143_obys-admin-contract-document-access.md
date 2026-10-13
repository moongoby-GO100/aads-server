---
doc_id: AADS-WRAP-143
project: AADS
business_domain: ACCT
document_key: obys-admin-contract-document-access-report
kind: report
version: 1.0.0
status: draft
source_session: acc75e55-0917-4a01-9a01-000000000002
---

# [AADS/오비서] 관리자 직원 계약 문서 명명·중복·열람 정정 검증 v1.0.0

검증 시각: 2026-10-06 13:41:39 KST. 검증 도구: requests, Playwright Chromium.

## 판정과 원인

- 기존 `PLAN.md`, `DESIGN.md`, `PRD.md`, `mockup.html`은 FLOW 파일명 규칙을 지키지 않았다. 업무·문서 종류·버전·초안 상태를 제목과 metadata에 보완했다.
- `/root/aads/.../mockup.html`을 웹 링크로 해석하면 문서 대신 `/login?redirect=...`으로 이동하는 것을 공개 브라우저에서 재현했다. 파일 API는 인증 없는 요청에 401을 반환했다. 로컬 파일 생성만 확인하고 브라우저 열람 가능성을 확인하지 않은 것이 전달 실패의 원인이다.
- DB `prompt_assets`의 `l1-doc-storage-rule`은 `enabled=false`인 초안이다. `YYYYMMDD_PROJECT_제목`을 이미 활성화된 규칙이라고 보고하지 않는다. 이번 정리는 `/root/aads/aads-docs/shared/rules/flow-rules.md`의 `PROJECT-LAYOUT-SEQ_제목`을 적용한다.

## 정리한 정본

| 종류 | 경로 | document_key |
|---|---|---|
| 기획 | `docs/plans/AADS-LAYOUT-023_obys-admin-contract-plan.md` | `obys-admin-contract-plan` |
| 화면·API 설계 | `docs/design/AADS-LAYOUT-024_obys-admin-contract-design.md` | `obys-admin-contract-design` |
| PRD | `docs/prd/AADS-LAYOUT-025_obys-admin-contract-prd.md` | `obys-admin-contract-prd` |
| 목업 | `docs/design/AADS-LAYOUT-026_obys-admin-contract-mockup.html` | `obys-admin-contract-mockup` |

네 정본은 기존 document_id와 document_key를 유지하고 version `1.0.1`, generation `2`로 갱신했다. 모두 draft이며 approved_revision_id는 null로 유지했다. 이전 v1.0.0 판본은 DB에 보존된다. 파일은 이름을 정정한 한 벌만 유지한다. 검토 HTML과 ZIP은 열람·배포용 파생본이다.

제목 형식: `[AADS/오비서] 현재 관리자 직원 계약 통합 — 문서종류 v1.0.1 (검토 초안)`.

다음 수정 시 같은 파일 경로와 document_key를 유지하고 새 version과 현재 expected_generation으로 등록한다. 날짜만 바꿔 정본 사본을 만들지 않는다.

## 공개 열람 검증

- [기획·설계·PRD 읽기](https://aads.newtalk.kr/education/obys-admin-contract-review-v1-0-1-20261006/index.html): 로그인 없이 HTTP 200, 문서 3종 표시, PRD 이동 확인.
- [클릭 목업](https://aads.newtalk.kr/education/obys-admin-contract-review-v1-0-1-20261006/AADS-LAYOUT-026_obys-admin-contract-mockup.html): 로그인 없이 HTTP 200, 직원 → 계약 작성 → 대상 확인 → 계약조건 입력 동작 확인.
- [원본 다운로드](https://aads.newtalk.kr/education/obys-admin-contract-review-v1-0-1-20261006/sources-v1.0.1.zip): HTTP 200, ZIP 시그니처 및 서버 파일 SHA-256 일치.
- 모바일 390×844: 페이지 가로 넘침 없음. 브라우저 JavaScript 오류 0건.
- 정본 4종의 인증된 `/api/v1/files/meta` 조회: 모두 HTTP 200.

## 중복·동시 수정 검증

- 정본 4종에 동일 판본을 재등록: idempotent=true, 동일 revision_id 반환, generation 2 유지.
- 같은 version에 다른 본문 등록: HTTP 409 `revision_conflict`.
- 이전 generation으로 다음 version 등록: HTTP 409 `generation_conflict`.
- 반례 요청 이후 기획 정본 generation이 그대로 2인지 재조회했다. 반례 본문은 저장되지 않았다.

검증 항목 13건 통과, 실패 0건. 원시 근거: [검증 JSON](../design/obys-admin-contract-review-20261006/evidence/document-access-verification.json), [등록 결과](../design/obys-admin-contract-review-20261006/evidence/document-access-fix-registration.json).
화면: [데스크톱 문서](../design/obys-admin-contract-review-20261006/evidence/07-review-access-desktop.png), [목업 계약조건](../design/obys-admin-contract-review-20261006/evidence/08-mockup-access-desktop.png), [모바일 문서](../design/obys-admin-contract-review-20261006/evidence/09-review-access-mobile.png).

## 이전 목업 검증과 범위

최초 목업 상호작용은 2026-10-06 13:32:14 KST 로컬 Playwright에서 13건 통과했다. [이전 원시 결과](../design/obys-admin-contract-review-20261006/evidence/verification.json)에 초기 파일 경로가 남아 있으므로 당시 기록으로만 읽는다. 그 검증은 직원 문맥, 필수조건, 저장 실패, 가상 초안 복구, 세션 만료, 알림 실패, 미동의 서명 차단, 모바일 겹침 수정에 관한 것이다.

운영 앱 코드·계약 API·실제 직원 계정·계약조건은 이번에 변경하지 않았다. 목업은 가상 데이터만 사용하고 실제 저장·알림·서명·PDF의 완료 증거가 아니다. 이전 구현 runner-6ed52003의 중단 기록은 기획 검토 선행을 위한 조치이며 새 구현 재개를 의미하지 않는다.

## 교훈

문서 내용 작성, DB 정본 등록, 브라우저 열람, 저장소 보존은 별도 검증한다. 파일 존재만으로 사용자가 열 수 있다고 보고하지 않는다. 파일명 규칙과 프롬프트 활성화 상태를 대조하고, 중복 방지는 제목이 아니라 고정 document_key·version·멱등성·generation 검사로 검증한다.

다음 단계는 CEO의 기획·PRD·목업 검토 후 구현 범위를 확정하는 것이다. 문서 정리는 구현 승인이나 운영 릴리스가 아니다.
