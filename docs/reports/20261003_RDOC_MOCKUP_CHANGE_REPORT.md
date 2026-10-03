# R-DOC 목업 검토 수정 보고 — v1 → v2 채팅 중심 전환

- 작업: `AADS-RDOC-MOCKUP-BACKEND-20261003` (Runner `runner-2f93e68d`) · 2026-10-03 KST
- 문서 성격: **초안(draft) · 미승인.** 이 문서는 승인도 구현 명령도 아니다. canonical `approved_revision_id` 는 바꾸지 않았다.
- 원 채팅 세션: [`8bf0405a-1f22-4ad9-bb09-6e0fce8c6339`](https://aads.newtalk.kr/chat#8bf0405a-1f22-4ad9-bb09-6e0fce8c6339) (이 문서 전문이 같은 세션의 채팅 문서 아티팩트로 연결된다 — 아래 "열람 링크")
- 정본 키: `rdoc-mockup-review-change-report` (project `AADS`, kind `report`)

## 1. 무엇이 바뀌었나 (CEO 지시 요약)

| | v1 | v2 |
|---|---|---|
| 목업을 여는 주 경로 | 별도 페이지로 이동 | **채팅의 목업 카드 → 같은 채팅 아티팩트 패널** (모바일은 전체화면) |
| 별도 페이지 | 주 경로 | 확대·공유용 **보조** 경로 |
| 수정 요청 | 정의 없음 | 보고 **직후** "방금 것 수정" 흐름. 요청마다 `change_request_id` + `source_message_id` + `base_revision` 기록 |
| 수정 결과 | 덮어쓰기 가능 | 수정중 → **새 불변 revision** → 요청별 반영/미반영 사유 + 전후 비교 → 재검토 |
| 승인 | 정의 약함 | 새 revision 은 항상 **미승인**, 이전 승인 **승계 금지** |
| 진행 중 작업 | 정의 없음 | 같은 변경건 작업은 **보류**, 실행 중이면 체크포인트 재검토 (**kill 금지**) |
| 예외 | 흩어져 있음 | 예외 6종 계약 (§3) |
| 승인 UI | 정의 없음 | iframe 밖 신뢰 영역에서만 승인 (샌드박스 완화 금지) |

출처: [`HANDOVER-v2.md`](../specs/rdoc-mockup-review/HANDOVER-v2.md), [`VERIFICATION-v2.md`](../specs/rdoc-mockup-review/VERIFICATION-v2.md), [`spec.md`](../specs/rdoc-mockup-review/spec.md), 커밋 `31312361`(v2 문서·목업), `2e983445`(아티팩트 열람 복구).

## 2. 구현 / 계획 구분 (이 표가 이 문서의 핵심이다)

### 2.1 이미 구현·실측된 것

| 항목 | 상태 | 근거 |
|---|---|---|
| plan / prd / spec **2.0.0 초안** 정본 등록 | 완료, 미승인 | plan rev3 `bd8aa614-019f-46c9-adf0-e960572083e4` · prd rev2 `d98a04e0-bb89-439d-82e8-505030c00698` · spec rev2 `230fa6b9-2f26-485d-bcef-a4da74557892` |
| v1 / v2 HTML 목업, v2 CSS 전용 정적 아티팩트 | 완료 | [`mockup-v1.html`](../specs/rdoc-mockup-review/mockup-v1.html) · [`mockup-v2.html`](../specs/rdoc-mockup-review/mockup-v2.html) · [`artifact-v2.html`](../specs/rdoc-mockup-review/artifact-v2.html) |
| 공개 목업 URL | 게시됨 | <https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v2/index.html> |
| 현재 세션 html_preview 아티팩트 | 등록됨 | `ba75b010-af90-5c98-b96d-8e71aaa0327a` |
| plan/prd/spec 본문 아티팩트 | 등록됨 (열람 복구) | 커밋 `2e983445` |
| **백엔드 코드: 불변 revision·서버 manifest hash·승인 게이트·회수·검증 API** | **코드 + 단위 50 + Postgres 통합 15 통과 (스크래치 DB)** | `app/api/mockup_reviews.py`, `app/services/mockup_review_service.py`, `app/models/mockup_review.py`, `migrations/20261003_mockup_reviews.sql`, [백엔드 보고서](20261003_RDOC_MOCKUP_BACKEND.md) |
| **수정 요청 저장·수정보고 조회 API** | **코드 + 통합 테스트 통과** | `POST .../changes`, `GET .../change-report` |

"백엔드 코드" 행의 의미: 워크트리 코드와 격리된 스크래치 Postgres 에서 테스트가 통과했다는 뜻이다. **운영 DB 에는 migration 이 적용되지 않았고 운영 API 에는 이 코드가 올라가 있지 않다.** 따라서 운영에서 `mockup-reviews` 경로는 아직 호출할 수 없다.

### 2.2 아직 구현하지 않은 것 (계획)

| 항목 | 담당 단계 | 비고 |
|---|---|---|
| 운영 DB migration 적용, API 반영 | D 단계 | 이 작업은 코드 검수·push 준비까지 |
| 채팅 UI: 목업 카드, 아티팩트 패널 연동, 수정 요청 입력, 전후 비교 화면 | 프론트 B | API JSON 계약은 [백엔드 보고서 §3](20261003_RDOC_MOCKUP_BACKEND.md) |
| 신뢰 영역 승인 버튼 (iframe 밖) | 프론트 B | 서버 쪽 승인 게이트는 구현됨. 버튼은 아님 |
| 파이프라인 러너가 실행 직전 `verify` 호출 | 후속 | 엔드포인트만 구현. 러너 연동은 미구현 |
| 실제 `/chat` Before 캡처 | 후속 | **blocked_evidence** — 비로그인 캡처가 로그인 화면으로 리다이렉트됨. 합성 화면을 Before 로 쓰지 않는다 |
| 로그인된 브라우저에서 아티팩트 탭 열림 확인 | 후속 | 이 러너는 로그인 불가 |
| plan/prd/spec 승인 | CEO | 승인 경로(`POST .../approve`)로만 |
| 공통 `HANDOVER.md` 병합 | 마지막 D 작업 | 이 작업은 건드리지 않음 |

## 3. 예외 6종 — 서버가 실제로 돌려주는 코드

모두 `detail.code` 로 내려가며, 구현·Postgres 통합 테스트로 검증했다.

| # | 계약 | HTTP | `detail.code` | 언제 |
|---|---|---|---|---|
| 1 | 동시 수정 충돌 | 409 | `generation_conflict` | `expected_generation` 이 현재와 다름 |
| 2 | 오래된 revision 기준 | 409 | `stale_revision` | base/제출/승인 대상이 최신 revision 이 아님 |
| 3 | 멱등 키 재사용 충돌 | 409 | `idempotency_key_conflict` | 같은 키에 다른 요청 본문 |
| 4 | 제출 산출물 누락 | 422 | `missing_artifacts` | 화면 상태·뷰포트·Before·증거 누락, `blocked_evidence` 포함 |
| 5 | 승인 없이 실행 | 409 | `approval_required` | `verify` 가 승인본·해시·상태 불일치를 발견 (감사 이벤트 남김) |
| 6 | 자산 변조 | 409 | `asset_tampered` | 승인 시점 이후 실제 파일 바이트가 manifest 해시와 다름 |

## 4. 수정 요청 한 건이 남기는 기록

`GET /api/v1/projects/{project}/mockup-reviews/{review_id}/change-report` 가 요청마다 아래를 돌려준다.

| 필드 | 내용 |
|---|---|
| `original_text` | 요청 원문. **저장 시 공백·개행을 다듬지 않는다** (비어 있는지 검사할 때만 trim) |
| `source_message_id` | 요청이 나온 채팅 메시지. 요청자가 접근 가능한 세션의 `user` 메시지만 허용(리뷰에 세션이 묶여 있으면 그 세션) |
| `base_revision` | 요청 당시 최신 revision (`revision_id`, 번호, 링크) |
| `new_revision` | 반영 결과 revision (`resolves` 로 닫힌 경우) |
| `outcome` / `outcome_reason` | `applied` / `not_applied` 와 사유(3자 이상 필수) |
| 전후 링크 | `base_revision.url` ↔ `new_revision.url` (revision 상세 + 자산 URL) |

같은 응답의 `markdown` 필드가 사람이 읽는 수정보고 본문(반영 / 미반영 / 대기·진행 중 / Revision 목록, 각 revision 링크 포함)이다.
수정 요청이 접수돼도 **승인되거나 구현 명령이 되지 않는다** (`approved:false`, `implementation_command:false`).
미해결 요청이 남아 있으면 새 revision 은 `resolves` 로 모두 닫아야 한다(`unresolved_change_requests` → 422).

### 이번 CEO 전환 지시를 이 모델로 환산하면

| 항목 | 값 |
|---|---|
| 요청 | 목업을 채팅 중심으로 보고, 보고 직후 같은 채팅에서 수정하는 흐름 (v1 → v2) |
| 기준(base) | v1 산출물 (`mockup-v1.html`, plan/prd/spec 이전 revision) |
| 결과(new) | v2 산출물 (`mockup-v2.html`, plan rev3 / prd rev2 / spec rev2 — 2.0.0 초안) |
| 반영 사유 | 주 경로·수정 흐름·예외 6종·승계 금지를 plan/prd/spec 에 반영 |
| 미반영 | §2.2 전체 — 채팅 UI·신뢰 영역 승인 버튼·러너 연동은 계획 단계 |

이 환산은 설명이다. 이 CEO 지시는 `mockup_reviews` 테이블에 행으로 들어가 있지 않다(테이블이 운영에 아직 없다). 원문 메시지 ID 를 이 문서가 임의로 만들어 적지 않는다.

## 5. 열람 링크

| 대상 | 링크 |
|---|---|
| 현재 채팅 세션 (이 문서 아티팩트가 연결된 곳) | <https://aads.newtalk.kr/chat#8bf0405a-1f22-4ad9-bb09-6e0fce8c6339> |
| v2 목업 (공개) | <https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v2/index.html> |
| v2 목업 (저장소) | [`mockup-v2.html`](../specs/rdoc-mockup-review/mockup-v2.html) · v1: [`mockup-v1.html`](../specs/rdoc-mockup-review/mockup-v1.html) |
| 설계 명세 | [`spec.md`](../specs/rdoc-mockup-review/spec.md) |
| 검증 / 핸드오버 | [`VERIFICATION-v2.md`](../specs/rdoc-mockup-review/VERIFICATION-v2.md) · [`HANDOVER-v2.md`](../specs/rdoc-mockup-review/HANDOVER-v2.md) |
| 백엔드 보고서 (API JSON 계약 포함) | [`20261003_RDOC_MOCKUP_BACKEND.md`](20261003_RDOC_MOCKUP_BACKEND.md) |
| 수정보고 API (D 단계 이후 동작) | `/api/v1/projects/AADS/mockup-reviews/{review_id}/change-report` |

기존 HTML 목업 링크는 참고용이다. 수정 내용의 기록은 이 문서와 정본 등록본이다.
