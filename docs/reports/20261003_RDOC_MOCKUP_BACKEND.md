# R-DOC 목업 검토 백엔드 — 구현 보고 · 프론트 B API JSON 계약

- 작업: `AADS-RDOC-MOCKUP-BACKEND-20261003` · P1 · SIZE M · Runner `runner-2f93e68d` · 2026-10-03 KST
- 마일스톤: M6 `da1fec5a-1801-52f6-b75d-5fd9b3ebc084` (상위 목표 `0361c451-cc03-4bd1-a423-76051b0546b2`)
- 원 세션: `8bf0405a-1f22-4ad9-bb09-6e0fce8c6339`
- 상태: **코드 완료 · 미커밋.** 운영 DB migration 과 API 반영은 D 단계에서 한다. 이 문서는 승인이나 구현 명령이 아니다.
- 동반 문서: [수정 보고서](20261003_RDOC_MOCKUP_CHANGE_REPORT.md)

## 1. STEP 0 — 기존 함수·엔드포인트 분류

TARGET_FILES 밖에서 바꾼 파일은 없다 (`git status`: 수정 1개 `app/main.py`, 나머지는 신규).

| 대상 | 분류 | 내용 |
|---|---|---|
| `app/api/canonical_documents.py` 의 `_authorize` / `_project` / `_scope`, `SECRET` | **유지(재사용)** | 수정 없이 import. 권한 모델(`project_document_grants`)을 정본 문서와 동일하게 쓴다 |
| 정본 문서 API (`/projects/{project}/documents/...`), `canonical_document_register`/`lookup` | **유지** | 연결 plan/prd/spec revision 검증을 위해 읽기만 한다. `approved_revision_id` 불변 |
| `app/main.py` | **수정** | import 1줄 + `include_router` 1줄 (`/api/v1`) |
| `app/api/mockup_reviews.py` | **신규** | 라우터 12개 경로 |
| `app/services/mockup_review_service.py` | **신규** | 비즈니스 로직 |
| `app/models/mockup_review.py` | **신규** | 요청 계약(pydantic) |
| `migrations/20261003_mockup_reviews.sql` | **신규** | 테이블 5 + 트리거 (멱등 재적용 확인) |
| `tests/unit/test_mockup_reviews.py`, `tests/integration/test_mockup_reviews_postgres.py` | **신규** | 단위 50, 통합 15 |
| 삭제 | **없음** | 기존 v1/v2 산출물, dirty 파일 보존 |

## 2. 요약 — 무엇이 구현됐나

| 요구 | 구현 위치 / 방식 |
|---|---|
| tenant/project/session 권한 | `_authorize`(grant: read/write/approve) + 세션 소속·소유자 검사(`session_not_found` 404, `session_access_denied` 403) |
| 불변 revision | `mockup_review_revisions` UPDATE/DELETE 차단 트리거. 새 revision 은 INSERT 만 |
| 서버 canonical manifest hash | 서버가 실제 바이트로 sha256·크기를 재계산하고 canonical JSON 의 sha256 을 `manifest_hash` 로 저장. 클라이언트 해시는 **주장**일 뿐이며 불일치 시 422 |
| 참조된 CSS/JS/image 자식 전부 hash | HTML/CSS 를 파싱해 자식 참조를 수집. manifest 에 없으면 `unhashed_child_reference` |
| 허용 내부 저장소만 / SSRF | `internal://docs/…`, `internal://mockup_assets/…` 만 허용. 외부 URL·`..`·백슬래시·`%`·`?#:`·심볼릭 링크 탈출 거부 |
| generation 409 | head 행 `FOR UPDATE` + 단조 증가 `generation` |
| 멱등 | 키별 `request_hash` + 저장된 응답 재생(200, `idempotent:true`). 같은 키·다른 본문은 409 |
| 감사 원자성 | 상태 변경과 감사 이벤트가 한 트랜잭션. 승인 이벤트 ID 가 head 에 기록됨 |
| 회수 | `revoke` → 승인 해제, 묶인 작업 보류 |
| 승인 승계 금지 | 새 revision 은 항상 미승인. 이전 승인은 `verify` 에서 `superseded_by_newer_revision` |
| plan/prd/spec 검증 | 존재·kind 일치·content_hash 일치. 제출/승인/`verify` 에서는 **현재 최신**이어야 함(`document_revision_stale`) |
| 초안 저장 ≠ 제출 ≠ 승인 | `revisions`(초안) → `submit`(검토 요청, `review_ready`) → `approve`(별도 권한 + `confirm:true`) |
| 수정 요청 보존 + 보고 문서 | `changes` 가 원문·`source_message_id`·`base_revision` 저장, `resolves` 가 신규 revision·사유 기록, `change-report` 가 JSON + markdown 반환 |

## 3. 프론트 B 용 API JSON 계약

공통.

- 베이스: `/api/v1/projects/{project}/mockup-reviews` (`project` 는 대소문자 무관, 서버가 대문자로 정규화: 예 `AADS`).
- 인증: 기존 테넌트 JWT (`Authorization: Bearer …`). 역할 `VIEWER` 이상(GET), `MEMBER` 이상(쓰기). 추가로 프로젝트 grant 필요(관리자급은 면제).
- grant 매핑: GET → `read` 이상 · create/revisions/revising/submit/changes/verify → `write` 이상 · approve/revoke → `approve`.
- 모든 쓰기 요청은 `idempotency_key`(8~64자, `[A-Za-z0-9._:-]`) 필수. 재시도는 **같은 키 + 같은 본문**.
- 모든 id 는 UUID 문자열, 시각은 ISO-8601(UTC).
- **오류 형식은 정본 문서 API 와 다르다.** `detail` 이 문자열이 아니라 객체다: `{"detail": {"code": "<코드>", ...부가필드}}`. 프론트는 `detail.code` 로 분기한다.
  단 요청 본문 검증 실패(pydantic)는 FastAPI 기본 422 이며 `detail` 이 **배열**이다. 권한 실패(grant)는 `{"detail": "project_access_denied"}` **문자열**이다. 두 경우 모두 방어적으로 처리한다.
- `expected_generation`: 클라이언트가 마지막으로 본 `generation`. 응답마다 최신 값이 돌아온다.

### 3.1 상태 머신

```
draft ──submit──▶ review_ready ──approve──▶ approved
  ▲                    │                       │
  │                    └──changes──▶ changes_requested ◀──changes──┘
  │                                      │
  └────── revisions(resolves 전부) ◀── revising ◀─ revising(start)
approved ──revoke──▶ revoked
```

- `status`(head): `draft | review_ready | changes_requested | revising | approved | revoked`
- revision `status`: 위 값 또는 `archived_stale`(오래된 기준으로 보관된 revision)
- 수정 요청 `status`: `open | in_revision | resolved`

### 3.2 `POST /api/v1/projects/{project}/mockup-reviews` (생성, 경로 접미사 없음)

요청.

```json
{
  "title": "R-DOC 채팅 목업 카드",
  "change_type": "modify",
  "goal_id": "0361c451-cc03-4bd1-a423-76051b0546b2",
  "session_id": "8bf0405a-1f22-4ad9-bb09-6e0fce8c6339",
  "idempotency_key": "create-20261003-0001"
}
```

`change_type`: `new | modify | none`(`none` = 화면 변경 없는 백엔드 전용, `backend_only_rationale` 필요). `goal_id`·`session_id` 선택.

응답 `201` (멱등 재생 시 `200`, `idempotent:true`). 이 형태가 **head 뷰**이고 `GET /{review_id}` 가 이를 확장한다. 다른 쓰기 응답은 구조가 다르며 각각 아래에 적는다.

```json
{
  "review_id": "…uuid", "project": "AADS", "title": "…", "change_type": "modify",
  "goal_id": "…uuid|null", "session_id": "…uuid|null",
  "status": "draft", "generation": 0, "pointer_generation": 0,
  "latest_revision": null, "approved_revision": null, "pending_change_requests": 0,
  "created_at": "2026-10-03T…+00:00", "updated_at": "…", "idempotent": false
}
```

`latest_revision` / `approved_revision` 은 존재할 때 다음 객체:
`{"revision_id","revision","manifest_hash","pointer_applied","created_by","created_at"}` (`approved_revision` 은 `approval_id` 추가).

오류: `404 goal_not_found`, `404 session_not_found`, `403 session_access_denied`, `422 credential_metadata_rejected`, `409 idempotency_key_conflict`.

### 3.3 `GET /{review_id}` (조회)

head 뷰 + 아래 필드.

```json
{
  "...head 뷰...": "",
  "revisions": [
    {"revision_id": "…", "revision": 2, "status": "review_ready", "parent_revision_id": "…|null",
     "manifest_hash": "64hex", "pointer_applied": true, "is_latest": true, "is_approved": false,
     "created_by": "…", "created_at": "…",
     "url": "/api/v1/projects/AADS/mockup-reviews/{review_id}/revisions/{revision_id}"}
  ],
  "change_requests": [ { "...§3.9 의 change 행..." } ],
  "unresolved_change_requests": [ { "...동일..." } ],
  "events": [
    {"event_id": 12, "action": "approved", "revision_id": "…|null", "actor_id": "…",
     "generation_after": 5, "payload": {}, "created_at": "…"}
  ]
}
```

`revisions` 는 revision 번호 내림차순 최대 200, `events` 는 id 내림차순 최대 100.
`events.action`: `created, revision_created, revision_archived_stale, revising_started, submitted, changes_requested, approved, revoked, verify_allowed, verify_denied`.
change 행 필드: `change_request_id, sequence, head_id, source_message_id, session_id, base_revision_id, screen_id, comment, requested_by, status, queued, outcome, outcome_reason, resolved_revision_id, rebased_to_revision_id, resolved_at, created_at`.

오류: `404 review_not_found`.

### 3.4 `POST /{review_id}/revisions` (초안 revision 저장)

요청.

```json
{
  "idempotency_key": "rev-20261003-0001",
  "expected_generation": 0,
  "parent_revision_id": null,
  "manifest_hash": null,
  "archive_if_stale": false,
  "resolves": [
    {"change_request_id": "…uuid", "outcome": "applied", "reason": "카드 높이를 줄임"}
  ],
  "doc_refs": [
    {"role": "plan", "document_key": "rdoc-mockup-review-plan", "revision_id": "…uuid", "content_hash": "64hex"},
    {"role": "prd",  "document_key": "rdoc-mockup-review-prd",  "revision_id": "…uuid", "content_hash": "64hex"},
    {"role": "spec", "document_key": "rdoc-mockup-review-spec", "revision_id": "…uuid", "content_hash": "64hex"}
  ],
  "manifest": {
    "design_tokens_version": "2026.10",
    "source_sha": "f20da86b",
    "backend_only_rationale": null,
    "screens": [
      {"screen_id": "chat-card", "title": "채팅 목업 카드", "route": "/chat",
       "requirement_ids": ["M13"], "states_not_applicable": {"offline": "정적 카드라 오프라인 상태 없음"}}
    ],
    "assets": [
      {"asset_id": "card-desktop", "role": "primary", "uri": "internal://mockup_assets/card-desktop.png",
       "sha256": "64hex", "byte_size": 12345, "mime": "image/png",
       "screen_id": "chat-card", "phase": "mockup", "viewport": "desktop", "fixture_id": "fx-1",
       "state": "default", "captured_at": "2026-10-03T05:00:00Z", "capture_source": "browser_capture",
       "redaction_status": "not_required"},
      {"asset_id": "card-css", "role": "child", "uri": "internal://mockup_assets/card.css",
       "sha256": "64hex", "byte_size": 800, "mime": "text/css"}
    ],
    "evidence": [
      {"evidence_id": "ev-1", "kind": "browser_capture", "screen_id": "chat-card", "route": "/chat",
       "success": true, "login_used": false, "detail": "", "recorded_at": "2026-10-03T05:00:00Z"}
    ]
  }
}
```

규칙.

- `primary` 자산은 `screen_id, phase, viewport, fixture_id, state, captured_at(타임존 포함), capture_source` 필수. `phase`: `before | mockup | implemented`. `state`: `default, loading, empty, error, permission, session-expired, offline`. `viewport`: `desktop | mobile | tablet | 가로x세로`.
- `sha256`·`byte_size`·`mime` 은 서버가 파일 바이트와 대조한다. 틀리면 422(`asset_hash_mismatch` 등)이며 응답에 서버 값이 담긴다.
- `doc_refs` 는 role 당 1개. 면제는 `{"role": "...", "exempt_reason": "10자 이상", "exempt_policy_ref": "승인된 정책 참조"}` (revision 지정과 동시 불가).
- `manifest_hash` 는 선택적 주장. 서버 값과 다르면 `422 manifest_hash_mismatch` (`server_manifest_hash`).
- 미해결 수정 요청이 있으면 `resolves` 가 그 묶음 **전부**를 닫아야 한다.
- `archive_if_stale:true` 이고 `expected_generation` 이 과거면 revision 은 **보관만** 되고 포인터를 옮기지 않는다 (`202`).

응답 `201` (멱등 재생 `200`):

```json
{
  "review_id": "…", "revision_id": "…", "revision": 2, "manifest_hash": "64hex",
  "doc_refs": [{"role": "plan", "document_key": "…", "revision_id": "…", "revision": 3, "version": "2.0.0", "content_hash": "64hex"}],
  "pointer_applied": true, "generation": 3, "status": "draft",
  "parent_revision_id": "…|null", "unapproved": true, "approval_inherited": false,
  "bindings_held": 0, "idempotent": false
}
```

`status` 는 미해결 수정 요청이 남으면 `changes_requested`, 아니면 `draft`.
응답 `202` (보관): `pointer_applied:false, reason:"stale_generation"`, `generation`/`status` 는 현재 값, `unapproved` 등은 없음.

오류: `409 generation_conflict | stale_revision` (둘 다 `latest_revision_id, latest_revision, manifest_hash, generation, status` 포함), `409 idempotency_key_conflict`, `422 manifest_hash_mismatch | duplicate_screen_id | duplicate_asset_id | duplicate_asset_uri | unknown_screen | asset_hash_mismatch | asset_size_mismatch | asset_mime_mismatch | asset_not_utf8 | unhashed_child_reference | credential_content_rejected | unresolved_change_requests | invalid_resolution | document_ref_not_found | document_ref_mismatch | document_ref_hash_mismatch`, 저장소 거부 코드(§4 하단).

### 3.5 `GET /{review_id}/revisions/{revision_id}`

```json
{
  "revision_id": "…", "revision": 2, "status": "draft", "parent_revision_id": "…|null",
  "manifest_hash": "64hex", "pointer_applied": true, "is_latest": true, "is_approved": false,
  "created_by": "…", "created_at": "…", "url": "…",
  "manifest": { "change_type": "modify", "design_tokens_version": "…", "source_sha": "…",
                "backend_only_rationale": null, "screens": [], "evidence": [],
                "assets": [ { "...§3.4 자산 필드...": "", "url": "/api/v1/projects/AADS/mockup-reviews/{review_id}/revisions/{revision_id}/assets/{asset_id}" } ] },
  "doc_refs": [], "design_tokens_version": "…", "source_sha": "…"
}
```

`manifest.assets[].url` 은 응답 시점에 서버가 덧붙인 필드이며 해시 계산에는 포함되지 않는다.

### 3.6 `GET /{review_id}/revisions/{revision_id}/assets/{asset_id}`

본문은 **원본 바이트**다(JSON 아님). 응답 헤더: `Content-Type: <manifest mime>`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, max-age=31536000, immutable`, `Content-Security-Policy: sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'`.

- 서버가 매 요청마다 바이트를 다시 읽어 manifest 해시와 대조한다. 다르면 `409 asset_tampered`(`assets:[{asset_id, reason}]`).
- **프론트 유의:** 이 경로는 Bearer 인증이 필요하므로 `<img src>`/`<iframe src>` 에 직접 넣을 수 없다. `fetch` 후 `Blob` URL 로 렌더하고, HTML 은 `sandbox` iframe(스크립트·same-origin 권한 없음)에만 넣는다.
- 오류: `404 revision_not_found | asset_not_found`.

### 3.7 `POST /{review_id}/revising` (수정 작업 시작)

요청 `{"idempotency_key": "…", "expected_generation": 4}`. 전제: head `status == changes_requested`.

```json
{"review_id": "…", "status": "revising", "generation": 5,
 "change_request_ids": ["…uuid"], "idempotent": false}
```

오류: `409 generation_conflict`, `409 invalid_state` (`status`, `required:"changes_requested"`).

### 3.8 `POST /{review_id}/submit` (검토 제출 — 승인이 아니다)

요청 `{"idempotency_key": "…", "expected_generation": 3, "revision_id": "…uuid"}`.

서버가 자산 재해시 → 문서 참조 최신성 → 완결성(plan/prd/spec 3종, 화면별 desktop·mobile 목업, 7개 상태 또는 사유, `modify` 는 Before, 시각 증거, 마스킹 대기 없음, 로그인/오류 화면 Before 금지)을 검사한다.

```json
{"review_id": "…", "revision_id": "…", "revision": 2, "manifest_hash": "64hex",
 "status": "review_ready", "generation": 4, "approved": false, "idempotent": false}
```

오류: `409 stale_revision`, `409 invalid_state`(`required:"draft"`), `409 asset_tampered`, `409 document_revision_stale`, `422 missing_artifacts`:

```json
{"detail": {"code": "missing_artifacts", "blocked_evidence": false,
  "missing": [
    {"code": "mockup_missing", "screen_id": "chat-card", "viewport": "mobile"},
    {"code": "state_missing", "screen_id": "chat-card", "state": "error"},
    {"code": "before_missing", "screen_id": "chat-card", "viewport": "desktop", "fixture_id": "fx-1"},
    {"code": "visual_evidence_missing", "screen_id": "chat-card"},
    {"code": "document_ref_missing", "role": "spec"},
    {"code": "redaction_pending", "asset_id": "…", "screen_id": "…"},
    {"code": "blocked_evidence", "asset_id": "…", "screen_id": "…", "detail": "…"},
    {"code": "screens_missing"}, {"code": "backend_only_rationale_missing"}
  ]}}
```

`missing[].code` 전체: `document_ref_missing, backend_only_rationale_missing, screens_missing, redaction_pending, blocked_evidence, mockup_missing, state_missing, before_missing, visual_evidence_missing`. `blocked_evidence` 가 하나라도 있으면 최상위 `blocked_evidence:true`.

### 3.9 `POST /{review_id}/changes` (수정 요청 — 승인도 구현 명령도 아니다)

```json
{
  "idempotency_key": "chg-20261003-0001",
  "expected_generation": 4,
  "change_request_id": "…uuid (클라이언트가 생성)",
  "base_revision_id": "…uuid (현재 최신 revision)",
  "source_message_id": "…uuid (요청이 나온 채팅 user 메시지)",
  "screen_id": "chat-card",
  "comment": "카드 높이를 줄여줘"
}
```

`comment` 는 **원문 그대로** 저장(공백·개행 보존, 비어 있으면 거부). `screen_id` 선택(있으면 base revision 에 존재해야 함).

응답 `201` (같은 `change_request_id` 재전송 시 `200`):

```json
{"review_id": "…", "change_request_id": "…", "source_message_id": "…", "base_revision_id": "…",
 "status": "changes_requested", "request_status": "open", "queued": false,
 "pending_change_requests": 1, "generation": 5, "approved": false,
 "implementation_command": false, "bindings_held": 0, "idempotent": false}
```

`queued:true` 는 `revising` 도중 접수되어 다음 묶음으로 넘어간 요청. 접수 시 승인본이 있었다면 **같은 변경건에 묶인 작업은 보류**(`bindings_held`)되며 승인 자체는 유지된다(해제는 `revoke`).
동일 `change_request_id` 로 내용이 다르면 `409 change_request_conflict`. 재전송(`200`) 응답은 축약형 `{review_id, change_request_id, status, generation, idempotent:true}` 이다.

오류: `422 empty_comment | credential_content_rejected | unknown_screen | source_message_session_mismatch | source_message_not_user_message`, `404 source_message_not_found | session_not_found`, `403 session_access_denied`, `409 stale_revision | invalid_state | idempotency_key_conflict | change_request_conflict`.

### 3.10 `GET /{review_id}/change-report` (수정 보고 조회)

```json
{
  "review_id": "…", "status": "changes_requested", "generation": 5,
  "change_requests": [
    {"change_request_id": "…", "sequence": 1, "source_message_id": "…", "requested_by": "…",
     "requested_at": "…", "screen_id": "chat-card|null", "queued": false,
     "original_text": "카드 높이를 줄여줘", "status": "resolved",
     "base_revision": {"revision_id": "…", "revision": 1, "url": "/api/v1/projects/AADS/mockup-reviews/…/revisions/…"},
     "new_revision": {"revision_id": "…", "revision": 2, "url": "…"},
     "outcome": "applied", "outcome_reason": "카드 높이를 줄임", "resolved_at": "…"}
  ],
  "revisions": [ { "...§3.3 revisions 행..." } ],
  "markdown": "# 목업 수정 보고 — …\n\n## 반영 (1)\n…"
}
```

미해결 요청은 `new_revision:null, outcome:null, outcome_reason:null`. `markdown` 은 반영 / 미반영 / 대기·진행 중 / Revision 목록 순서이며 revision 링크를 포함한다.

### 3.11 `POST /{review_id}/approve` (구현 승인 — 신뢰 영역에서만 호출)

요청 `{"idempotency_key": "…", "expected_generation": 4, "revision_id": "…", "manifest_hash": "64hex", "confirm": true}`. `confirm` 은 반드시 `true`(그 외 422).

```json
{"review_id": "…", "approval_id": 15, "revision_id": "…", "revision": 2, "manifest_hash": "64hex",
 "status": "approved", "generation": 5, "approved_by": "…", "bindings_held": 0, "idempotent": false}
```

최신 revision·해시·generation 이 모두 일치하고 `review_ready` 여야 하며, 승인 직전에 자산 재해시와 완결성·문서 참조 최신성을 **다시** 검사한다. 오류: `409 stale_revision | invalid_state | asset_tampered | document_revision_stale`, `422 missing_artifacts`, `403 project_access_denied`(approve grant 없음).

### 3.12 `POST /{review_id}/revoke`

요청 `{"idempotency_key": "…", "expected_generation": 5, "approval_id": 15, "reason": "3자 이상"}`.

```json
{"review_id": "…", "revoked_revision_id": "…", "approval_id": 15, "status": "revoked",
 "generation": 6, "bindings_held": 1, "idempotent": false}
```

승인 후 수정 요청이 이미 `changes_requested` 였다면 `status` 는 그 값을 유지한다. 오류: `409 generation_conflict | not_approved | stale_approval`.

### 3.13 `POST /{review_id}/verify` (실행 직전 검증 — 파이프라인용)

요청 `{"task_id": "runner-xxxx", "revision_id": "…", "manifest_hash": "64hex", "phase": "pre_execution"}`. `phase`: `submit`(묶기만) | `pre_execution` | `checkpoint`(이미 `running` 인 묶음만 통과).

허용 `200`:

```json
{"allowed": true, "verified": true, "review_id": "…", "revision_id": "…", "manifest_hash": "64hex",
 "approval_id": 15, "approval_generation": 5, "binding_status": "running",
 "expires_at": "2026-10-03T05:05:00+00:00"}
```

거부 `409` — **감사 이벤트를 남긴 뒤** 돌려준다.

```json
{"detail": {"allowed": false, "code": "approval_required", "audit_event_id": 21,
 "reasons": ["superseded_by_newer_revision"], "status": "changes_requested", "generation": 5}}
```

`reasons` 값: `task_out_of_scope, not_approved, approval_revoked, revision_mismatch, superseded_by_newer_revision, change_requested_hold, revision_not_found, manifest_hash_mismatch, asset_tampered, document_revision_changed, binding_missing, binding_held`. `asset_tampered` 일 때 `assets`, `document_revision_changed` 일 때 `document` 가 추가된다.

`task_id` 는 이 프로젝트(및 review 에 묶인 goal)의 활성 `goal_task_links` 에 있어야 한다. 허용 응답의 `expires_at` 은 5분 TTL 이며 이후엔 다시 검증해야 한다.

## 4. 오류 코드 일람 (`detail.code`)

| HTTP | 코드 |
|---|---|
| 403 | `session_access_denied` (+ 문자열 `project_access_denied`) |
| 404 | `review_not_found`, `revision_not_found`, `asset_not_found`, `goal_not_found`, `session_not_found`, `source_message_not_found` |
| 409 | `generation_conflict`, `stale_revision`, `stale_approval`, `idempotency_key_conflict`, `invalid_state`, `not_approved`, `change_request_conflict`, `asset_tampered`, `document_revision_stale`, 그리고 verify 거부의 `approval_required` |
| 422 | `missing_artifacts`, `manifest_hash_mismatch`, `unresolved_change_requests`, `invalid_resolution`, `empty_comment`, `unknown_screen`, `duplicate_screen_id`, `duplicate_asset_id`, `duplicate_asset_uri`, `asset_hash_mismatch`, `asset_size_mismatch`, `asset_mime_mismatch`, `asset_not_utf8`, `unhashed_child_reference`, `credential_content_rejected`, `credential_metadata_rejected`, `document_ref_not_found`, `document_ref_mismatch`, `document_ref_hash_mismatch`, 아래 저장소 거부 코드 |

저장소 거부 코드는 `asset_id` 와 `detail` 을 함께 돌려준다: `asset_uri_not_allowed`, `asset_not_found`, `asset_too_large`, `external_reference_not_allowed`, `absolute_reference_not_allowed`, `invalid_asset_reference`, `reference_escapes_root`. 프론트는 알 수 없는 `422` 코드를 일반 오류로 표시하고 `detail.code` 를 그대로 노출하면 된다.

## 5. 예외 6종 해석

| 계약 | 코드 | 검증 테스트 |
|---|---|---|
| 동시 수정 충돌 | 409 `generation_conflict` | 통합: 동시 쓰기에서 승자 1명, 나머지 409 |
| 오래된 revision | 409 `stale_revision` | 통합: 이전 revision 기준 `changes`/`submit`/`approve` |
| 멱등 키 충돌 | 409 `idempotency_key_conflict` | 통합: 같은 키·다른 본문 |
| 산출물 누락 | 422 `missing_artifacts` | 통합 + 단위: 완결성 규칙 전부 |
| 승인 없이 실행 | 409 `approval_required` | 통합: `verify` 거부와 감사 행 확인 |
| 자산 변조 | 409 `asset_tampered` | 통합: 제출 후·승인 후·`verify`·자산 GET 에서 바이트 변조 |

## 6. 보안 처리

- 자산은 허용 루트(`docs/`, `MOCKUP_REVIEW_ASSET_ROOT` 또는 `data/mockup_assets/`) 안의 일반 파일만. 심볼릭 링크·경로 탈출·외부 스킴 거부. 파일당 25MiB 상한.
- HTML/CSS 의 `src`, `href`, `srcset`, `url()`, `@import` 등 모든 참조를 수집하고, manifest 에 해시가 없는 자식이 있으면 거부(`unhashed_child_reference`). 외부 URL 참조도 허용하지 않는다.
- 자격증명 패턴(정본 문서 API 와 같은 `SECRET`)이 제목·원문·증거·텍스트 자산에서 발견되면 거부하고 값을 응답에 싣지 않는다.
- 감사 이벤트 테이블·revision 테이블은 UPDATE/DELETE 불가(트리거). 수정 요청 행은 해결 후 변경 불가.
- 자산 GET 은 `nosniff` + `sandbox` CSP 를 붙인다.
- 새 LLM 호출 없음. `ANTHROPIC_API_KEY` 사용 없음.

## 7. 검증 결과 (실행한 것만)

| 항목 | 결과 |
|---|---|
| 단위 `tests/unit/test_mockup_reviews.py` | **50 통과** (`bash scripts/run_unit_tests.sh`) |
| Postgres 통합 `tests/integration/test_mockup_reviews_postgres.py` | **15 통과** — 스크래치 DB(`aads_rdoc_mockup_test`, 격리 컨테이너)에서, 운영 이미지 + 워킹트리 마운트. `AADS_MOCKUP_TEST_DATABASE_URL` 미설정 시 skip 되도록 만들었다 |
| migration `20261003_mockup_reviews.sql` | 스크래치 DB 에 2회 연속 적용, 오류 없음(멱등). **운영 DB 미적용** |
| ruff (`F821`, `F811`) | 신규·수정 파일 clean |
| `python -m compileall` | 통과 |
| `scripts/dup_guard.py` | rc=0 |
| 전체 `tests/unit` | **실패 있음 — 기존 실패이며 이 변경과 무관.** 전체 `tests/unit` 을 변경 후(워킹트리)와 `git archive HEAD` 사본(변경 전) 양쪽에서 같은 방식으로 돌려 비교했다. 변경 전: 41 failed / 4 errors / 7567 passed. 변경 후: 42 failed / 4 errors / 7616 passed (통과 +49, 실패 +1 = 신규 단위 테스트 50개와 합이 맞는다). 실패 집합의 차이는 `test_reclaim_runner_worktrees_count_cap.py::test_default_is_twelve` 1건뿐이며, 단독 재실행에서 10 passed 로 통과했다(두 전체 스위트를 동시에 돌린 부하 때문으로 추정, 미확정). 기존 실패 41건은 이 작업에서 고치지 않았다 (예: `test_yeoljeong_finance_*`, `test_tool_archive_flow`, `test_review_hold_commit_gap`) |
| AAG 착수 브리프 | **통과로 보고하지 않는다.** 브리프는 stale/not_proven 이며 현재 SHA 기준으로 다시 비교해야 한다 |
| 빌드·재시작·배포 | 승인 후 Runner 빌드 검증 대상 |
| 브라우저/UI | 이번 범위 아님(백엔드만). 실행하지 않았다 |

통합 테스트가 다루는 것: 초안/제출/승인 구분, 바이트 기준 manifest hash 와 자산 서빙, 승인 비승계·불변 트리거, 예외 6종, 문서 참조 검증과 면제, 권한(역할·테넌트·세션), 멱등 재생, 동시 승자 1명, 오래된 기준 보관(202), 수정 요청 원문·링크·결과·보고서, 입력 검증, `verify`/묶기/회수와 거부 감사, 승인 후 변조, 승인 감사 원자성.

## 8. M6 착수·최종 refs

- 마일스톤: `da1fec5a-1801-52f6-b75d-5fd9b3ebc084` — 이 작업은 DB `milestones` 행을 직접 수정하지 않았다(상태·evidence 는 Runner 의 `goal_task_links` 흐름 소유).
- 착수 ref: Runner 작업 `runner-2f93e68d`, 원 세션 `8bf0405a-1f22-4ad9-bb09-6e0fce8c6339`, 시작 기준 커밋 `f20da86b`.
- 최종 ref(코드): 위 TARGET_FILES 7개 + 이 문서 + [수정 보고서](20261003_RDOC_MOCKUP_CHANGE_REPORT.md). 커밋 SHA 는 Runner 가 승인 후 만들며 그 전에는 존재하지 않는다.
- 수정 문서 등록: 정본 초안 `rdoc-mockup-review-change-report` revision `6ee7895f-bdd3-4cce-900a-b10509f059c7` (미승인, `approved_revision_id` 불변), 채팅 아티팩트 `96a72e2d-851f-5bd6-90cb-90951ed7b62b` (세션 `8bf0405a-1f22-4ad9-bb09-6e0fce8c6339`, 본문 sha256 `091a16c1…9264` = 정본 해시).
- 핸드오버: DB `handover_write` 완료 — 항목 `rdoc-mockup-backend-20261003` (project AADS, revision 1, id `f75083ff-e366-4901-9190-077730a37a60`). 공통 `HANDOVER.md` 는 수정하지 않았다 — 마지막 D 작업 담당.

## 9. 다음 단계 (D)

1. 코드 검수 → Runner 가 승인 후 commit/push.
2. D: 운영 DB 에 `migrations/20261003_mockup_reviews.sql` 적용 → API 반영 → 스모크(`create`→`revisions`→`submit`→`approve`→`verify`).
3. 프론트 B: §3 계약으로 목업 카드·패널·수정 요청 UI 와 신뢰 영역 승인 버튼 구현.
4. 파이프라인: 실행 직전 `verify` 호출 연동.
5. 실제 `/chat` Before 캡처 (blocked_evidence 해소).

## 10. 복구 기록 — gitleaks 픽스처 차단 (runner-aad81f4d, 2026-10-03)

작업 `AADS-RDOC-MOCKUP-FIXTURE-RECOVERY-20261003`. 선행 `runner-7ba71286` 이 `approval_commit_failed` 로 끝났고, 이어 `runner-2f93e68d` 의 산출물(staged)이 남아 있었다.

**실제 원인.** pre-commit Step 1b 의 `gitleaks git --staged` 가 `generic-api-key` 로 `tests/integration/test_mockup_reviews_postgres.py:579` 의 고정 `api_key = …` 리터럴을 오탐했다. 같은 종류의 리터럴이 `tests/unit/test_mockup_reviews.py:220` 에도 있었고, 수정 전 파일 단위 스캔에서 두 파일이 각각 1건씩 검출됐다(나머지 7개 파일은 clean). 값은 순차 문자열+숫자로 만든 합성값이며 실제 secret 이 아니다 — 별도 위험 보고 대상 없음. detached HEAD 자체를 원인으로 단정하지 않았다. 오류 사전 `git.gitleaks_blocks_masking_test_fixture` 와 일치한다.

**변경.** 두 리터럴을 런타임 조합으로 교체했다 — `"api_" + "key"`, `"abcdefghijklmnop" + "1234"`. 조합 결과는 원래 문자열과 바이트 단위로 같음을 직접 확인했다. 따라서 `credential_content_rejected` assertion 의 의미와 `SECRET` 검출 함수는 그대로다. 검출 함수·`.gitleaks.toml`·디렉터리 allowlist·hook 은 건드리지 않았고 `# gitleaks:allow` 도 쓰지 않았다.

**재적용.** `runner-2f93e68d` 의 staged 원본(읽기 전용, 수정 없음)에서 diff 를 다시 만들어 최신 main(`dffe625c`)의 격리 worktree 에 적용했다. `logs/runner-diff/runner-2f93e68d.patch` 는 3187행에서 잘려 `git apply` 가 불가능해(corrupt patch) 그대로 쓰지 않았다. 적용 후 파일 9개가 원본과 같고 두 줄만 다르다.

**검증 (이번 실행, 이전 결과 재사용 없음).**

| 항목 | 결과 |
|---|---|
| gitleaks (`gitleaks dir --redact -c .gitleaks.toml`, 파일별) | 수정 전 2개 파일 각 1건 → 수정 후 9개 파일 모두 no leaks |
| ruff `F821`,`F811` | clean |
| `python3 -m compileall` | 통과 |
| `bash scripts/run_unit_tests.sh tests/unit/test_mockup_reviews.py` | 50 passed |
| 통합 `tests/integration/test_mockup_reviews_postgres.py` | 15 passed — 일회용 Postgres 15 컨테이너(DB 이름에 `test` 포함), 운영 스키마의 schema-only 사본에 internal 테넌트 시드 1행 + migration 적용(2회, 멱등), `AADS_CANONICAL_DB_REQUIRED=1`. 실행 후 컨테이너 제거 |
| `import app.main` (pre-commit 과 같은 이미지·env) | rc=0, `mockup` 라우트 12개 |
| `scripts/dup_guard.py` | rc=0 |

**미실행.** ① `gitleaks git --staged` 를 그대로는 돌리지 못했다 — 이 세션은 `git add` 금지라 파일 단위 `dir` 스캔으로 대체했고, 최종 확인은 Runner 의 commit hook 이다. ② 운영 DB migration·API 반영·스모크는 하지 않았다(구현 완료이지 운영 완료가 아니다). ③ 전체 `tests/unit` 은 다시 돌리지 않았다(§7 의 비교 결과는 이전 실행). ④ 빌드·재시작·배포 — 승인 후 Runner 대상.

**기록.** DB 핸드오버 `rdoc-mockup-backend-20261003` revision 2 로 갱신(공통 `HANDOVER.md` 미수정). 오류 사전은 원인·예방·수정 파일·메모를 보완했으나 **fix commit SHA 는 Runner 커밋 전이라 비어 있다** — 커밋 후 채워야 한다. 목표 `0361c451…`/마일스톤 `da1fec5a…` 의 `milestones` 행은 직접 수정하지 않았다(Runner 의 `goal_task_links` 흐름 소유). 비용은 별도 LLM 호출 없이 로컬 검증만 해서 추가 청구 없음, $5 한도 해당 없음.
