# 정본 기존문서 연결 해제 API — RESULT

TASK_ID: AADS-PROJECT-DOCUMENTS-M3-LEGACY-UNLINK-API-20261002

## 지시서와 다르게 된 점 (먼저 읽을 것)

지시서는 `app/api/canonical_documents.py` 만 고치면 되는 것으로 봤으나, 현재 스키마로는 해제 API 가 **동작할 수 없었다**. 세 가지가 막고 있었다.

1. `project_document_legacy_links` 에 `BEFORE UPDATE OR DELETE` 트리거(`project_document_legacy_links_immutable`)가 있어 모든 DELETE 가 `project document history is append-only` 로 거부된다.
2. `project_document_events.action` CHECK 에 `'legacy_unlinked'` 가 없다.
3. 이벤트 테이블에 `goal_document_id` 를 담을 컬럼이 없다(지시서의 "어느 goal_document_id" 를 기록할 곳이 없음).

그래서 **지시서에 없던 마이그레이션 2개**를 추가했다 (additive, 멱등). **운영 DB 에는 적용하지 않았다.** 이 API 는 마이그레이션이 적용되기 전에는 운영에서 500(트리거/CHECK 위반)을 낸다 — 배포 순서는 "마이그레이션 → 코드".

## STEP 0 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `POST /{document_key}/legacy-links` (link_legacy_goal_document) | 유지 | 동작 변경 없음 |
| `GET /{document_key}/legacy-links` | 유지 | |
| `POST /{document_key}/archive` (archive_document) | 수정(동작 불변 리팩터링) | UPDATE+INSERT 두 문장을 `_archive_approved()` 로 추출해 해제 API 가 재사용. 응답·검증·이벤트 동일. 기존 테스트 통과 |
| `DELETE /{document_key}/legacy-links/{goal_document_id}` | 신규 | |
| `_archive_approved()` | 신규 | 위 추출분 |
| `migrations/20261002_project_document_legacy_unlink.sql` | 신규 | events CHECK 확장, `goal_document_id` 컬럼, legacy_links 트리거 교체 |
| `migrations/rollback/20261002_project_document_legacy_unlink.down.sql` | 신규 | |
| `tests/integration/test_canonical_documents_legacy_unlink.py` | 신규 | |
| 삭제 | 없음 | |

## 엔드포인트 명세

`DELETE /api/v1/projects/{project_key}/documents/{document_key}/legacy-links/{goal_document_id}?archive_head=false`

- 인증/권한: 연결 API 와 동일(`WRITE` = tenant MEMBER + `_authorize`). 프로젝트 grant `write` 이상. `archive_head=true` 이면 `archive` 와 동일하게 `approve` grant 필요(없으면 403 `project_access_denied`). tenant admin/owner/내부 관리자는 grant 없이 통과(기존 `_scope` 규칙 그대로).
- 스코프: head 를 `tenant_id + project_key + document_key` 로 `FOR UPDATE` 조회 후, 삭제는 `legacy_links.tenant_id/project_key/goal_document_id` 와 `revision.head_id = head.id` 를 모두 만족하는 행만.
- 같은 트랜잭션에서 `project_document_events` 에 `action='legacy_unlinked'`, `actor_id`(누가), `created_at`(언제), `goal_document_id`, `revision_id` 기록.
- `goal_documents` 는 읽지도 쓰지도 않는다.
- `archive_head=true`: 해제 후 그 head 의 legacy link 가 0건이고 head 에 approved revision 이 있으면 `_archive_approved()` 로 archive(`approved_revision_id=NULL`, generation+1, `archived` 이벤트). 승인본이 없으면 `archived:false`. 기본값 false.

응답 200: `{"unlinked": true, "goal_document_id": N, "revision_id": "...", "remaining_links": n, "archived": false}`

| 상태 | detail |
|---|---|
| 404 | `document_not_found` (head 없음 / 다른 tenant·project) |
| 404 | `legacy_link_not_found` (해당 head 에 그 연결 없음 — 재호출 포함) |
| 403 | `project_access_denied` |
| 422 | `invalid_goal_document_id` (0 이하) / `invalid_project_key` |

### 마이그레이션이 하는 일
- `project_document_events.goal_document_id bigint` (nullable) 추가, action CHECK 에 `legacy_unlinked` 추가.
- legacy_links 트리거: UPDATE 는 항상 거부, DELETE 는 **트랜잭션 로컬 설정 `aads.legacy_unlink='on'` 일 때만** 허용. API 가 `set_config(..., true)` 로 켠다. 임의 SQL 의 DELETE 는 계속 막힌다(테스트로 확인).
- `project_document_goal_links`(head↔goal) 행은 해제해도 **지우지 않는다**. 연결 API 가 `ON CONFLICT DO NOTHING` 으로 넣어서, 이전부터 있던 행인지 구분할 수 없기 때문이다.

## 롤백 절차
1. (연결 되돌리기) `DELETE .../legacy-links/{goal_document_id}` — 한 건씩.
2. (필요 시 승인 해제) 마지막 연결까지 지우면서 `?archive_head=true`, 또는 별도로 `POST .../archive`.
3. 해제 뒤 같은 문서를 다시 연결해도 된다(테스트로 확인: 해제 → `POST legacy-links` 재연결 성공).
4. 마이그레이션 자체를 되돌리려면 `migrations/rollback/20261002_project_document_legacy_unlink.down.sql` — 트리거만 원래대로 복원한다. 이벤트 컬럼/CHECK 는 append-only 이벤트에 `legacy_unlinked` 가 이미 쌓였을 수 있어 일부러 남긴다.

## 테스트 결과 (실제 실행)

- `tests/integration/test_canonical_documents_legacy_unlink.py` + 기존 `test_canonical_documents_workflow.py`: 호스트 venv(`/root/aads/aads-server/.venv/bin/python -m pytest`), 격리 PG(`127.0.0.1:55439/aads_doc_m1_final`), `AADS_CANONICAL_DB_REQUIRED=1` → **3 passed**. 신규 테스트만 재실행(마이그레이션 2회차 적용 = 멱등 확인) → **1 passed**. 종료 후 격리 DB 의 `goal_documents`/legacy_links/test goals 잔여 0건.
- 신규 테스트가 검증하는 것: 연결 2건 → 조회 2건 → 해제 → 재조회 / 다른 tenant 해제 시도 404 / 다른 project 해제 시도 404 / grant 없는 project 403 / 없는 문서 404 / id 0 → 422 / write-only 권한으로 `archive_head=true` 403 / 임의 SQL DELETE 차단 / 재해제 404 / `legacy_unlinked` 이벤트의 actor·goal_document_id·revision_id·시각 / 남은 연결이 있으면 archive 안 됨 / 마지막 연결 해제 + `archive_head=true` → approved NULL, brief 비어 있음 / 해제 후 재연결 / **`goal_documents` 전체 컬럼 스냅샷 전후 동일**.
- `bash scripts/run_unit_tests.sh tests/unit/test_canonical_documents.py tests/unit/test_project_document_m2.py tests/unit/test_project_document_m3_inventory.py` → **39 passed**. (이 게이트 컨테이너는 격리 DB 에 라우트가 없어 통합 테스트는 skip 된다 — 통합 테스트는 위 호스트 venv 실행 결과가 근거다.)
- `ruff check --select F821,F811` (`app/api/canonical_documents.py`, 신규 테스트) → 0건. `python3 -m compileall` 통과. `scripts/dup_guard.py --paths app/api/canonical_documents.py` 이상 없음.

## 한계
- 격리 DB 에는 테스트가 마이그레이션을 직접 적용한다(이 DB 한정, 멱등). 운영 DB 에는 쓰지 않았다.
- 인증 핵심 파일 수정 없음. 기존 엔드포인트의 응답·검증 동작 변경 없음.
- commit/push/배포는 하지 않았다. 변경 파일: `app/api/canonical_documents.py`, `migrations/20261002_project_document_legacy_unlink.sql`, `migrations/rollback/20261002_project_document_legacy_unlink.down.sql`, `tests/integration/test_canonical_documents_legacy_unlink.py`, 이 문서.
