# RESULT — AADS-PROJECT-DOCUMENTS-M1-20260923

## Step 0 inventory and classification

| Existing item | Classification | Reason |
| --- | --- | --- |
| `app/api/project_docs.py` scanner, content preview, semantic search, approvals routes; filesystem/remote-file and approval DB contacts | 유지 | Discovery and preview are not the canonical document revision ledger. No edit. |
| `app/routers/goals.py` `goal_documents`, `add_goal_document`, `_normalize_goal_document_path`, `_goal_document_identity`; `goal_documents`, `goals` DB contacts | 유지 | Existing goal/path ledger and files are preserved. New canonical goal links are separate. |
| `app/api/artifacts.py` create/list/detail routes; `project_artifacts` DB contact | 유지 | Existing artifact rows are not overwritten or migrated. |
| `app/main.py` existing `project_docs_router`, `goals_router`, `artifacts_router` imports and mounts | 유지 | Existing routes remain mounted. |
| `app/main.py` canonical document router import and `/api/v1` mount | 신규 | New authenticated canonical API must be reachable. This file was outside the explicit overlap list and was changed for that reason. |
| `app/api/canonical_documents.py` models, validators, authorization, routes, `approved_brief`; canonical tables and read-only legacy inventory contacts | 신규 | Isolated M1 implementation, including verified references to existing `goal_documents` rows. |
| `migrations/20260923_project_document_canonical.sql` tables, constraints, append-only audit triggers, staged handover update | 신규 | Additive schema and handover source. |
| `tests/unit/test_canonical_documents.py` and design contract | 신규 | Scoped validation and M2 read interface contract. |
| `app/api/aag.py`, `app/services/context_builder.py`, `app/services/tool_registry.py`, `app/services/tool_executor.py`, `scripts/pipeline-runner.sh`, `deploy.sh` | 유지 | DB-map review overlap; no edits. |

No item was deleted. No legacy data, paths, or rows were changed.

## Verification

- `JWT_SECRET_KEY=test-secret /root/aads/aads-server/.venv/bin/python -m pytest -q tests/unit/test_canonical_documents.py tests/unit/test_goal_document_versions.py`: **24 passed**.
- `python3 -m py_compile app/api/canonical_documents.py app/main.py tests/unit/test_canonical_documents.py`: passed.
- `git diff --check`: passed.
- Existing `test_project_docs_viewer.py` was started (full file and public-education subset), but each stalled after printing passing progress dots and produced no final summary. Their result is **inconclusive**, not counted as passed.
- Initial system `pytest` attempt could not collect tests because that interpreter lacks FastAPI; the repository virtual environment ran them successfully.
- SQL migration execution and rollback in a nonproduction transaction: **not run** (no isolated database supplied; operational DB changes prohibited).
- `migrations/rollback/20260923_project_document_canonical.down.sql` (신규) — 적용분을 되돌리는 down 스크립트. 기존 `migrations/rollback/*.down.sql` 13건과 같은 규칙을 따른다. 운영 스키마 대상 단일 트랜잭션에서 **up → down 왕복**을 실측했다: up 후 `project_document%` 테이블 6, `goal_documents` 유일 인덱스 1, 트리거 함수 1 → down 후 **0 / 0 / 0**, `psql -v ON_ERROR_STOP=1` **exit 0**, 마지막 `ROLLBACK` 으로 영속 변경 0건. 별도로 up 을 **2회 연속** 적용해 멱등성도 확인했다(exit 0, ERROR 0건, 인덱스 중복 생성 없음). 첫 시도는 `DROP TABLE project_document_revisions` 가 `project_document_latest_fk ... depends on table` 로 실패했다 — `heads.latest/approved` 와 `revisions.head_id` 가 **순환 FK** 라서 down 은 테이블 삭제 전에 그 두 제약을 먼저 떼야 한다. 그 단계를 스크립트에 넣어 재검증했다.
- down 스크립트의 핸드오버 회수는 `revision = 1 AND created_by = 'pipeline_runner'` 로 좁혀 놓았다 — up 의 `ON CONFLICT DO UPDATE` 가 기존 행을 갱신한 경우(revision>=2)는 남의 감사기록이므로 지우지 않는다.
- 이 왕복 검증은 앞 항목의 트랜잭션 ROLLBACK 시험과 **다른 것**이다. 그쪽은 적용 취소 시험이고, 이 항목은 실제 down 스크립트 실행 결과다.
- 승인 후 Runner 빌드 검증 대상. No build was run.

## Outstanding

- The staged migration has not been applied; the handover entry update is likewise staged, not yet present in the operational DB.
- Administrative project grant provisioning is by SQL in M1. A grant management interface is outside this scope.
- M2 must wait for DB-map review and then integrate `approved_brief` into session and runner paths. Missing or unapproved documents must remain non-authoritative.
- Legacy mapping candidates require manual identity and hash review before any registration. The inventory endpoint performs no backfill.

## Cost

No external LLM API or paid service was called. Local validation only; infrastructure cost was not measured.

## R2 이식 결과 — AADS-PROJECT-DOCUMENTS-M1-R2-20260923

### STEP 0 분류

| 기존 항목 | 분류 | 처리 |
| --- | --- | --- |
| `app/main.py`의 기존 import, 라우터 등록, lifespan 스케줄러, 인증 미들웨어, health 엔드포인트 및 기존 DB 접점 | 유지 | 기존 순서와 동작을 보존했다. |
| `app/main.py`의 정본 문서 import 및 `/api/v1` 등록 | 수정 | 지정 위치에 각각 한 줄만 추가했다. |
| `app/api/canonical_documents.py`의 인증·입력 모델·검증·승인 요약·정본/레거시 문서 엔드포인트와 문서/목표/세션 DB 접점 | 신규 | `m1-canonical-documents`의 구현을 그대로 이식했다. |
| 정본 문서 SQL migration, 단위테스트, 설계 문서, 이 결과 문서 | 신규 | 지정 경로만 이식했다. 이 결과 문서에는 R2 실측을 추가했다. |
| 삭제 항목 | 해당 없음 | 삭제하지 않았다. |

### 교정 확인

- `create_revision`의 `versioned` 선판정 및 멱등키의 version 비교 확인.
- `_read_source()`의 `stat().st_size` 선검사와 제한 읽기 확인. `read_bytes()` 호출 0건.
- SECRET 정규식의 `sk-`, `gh[pousr]_`, `github_pat_`, `xox[baprs]-`, `AIza`, `AKIA` 확인.

### R2 검증 실측

- `bash scripts/run_unit_tests.sh tests/unit/test_canonical_documents.py`: **exit 2**, 기준 이미지 부재. 출력: `[run_unit_tests] 기준 이미지를 찾지 못했습니다 (5초 간격 6회 재시도)`; `container:aads-server -> no such container`, `container:aads-server-green -> no such container`.
- `JWT_SECRET_KEY=test-secret /root/aads/aads-server/.venv/bin/python -m pytest -q tests/unit/test_canonical_documents.py`: **exit 0, 13 passed in 0.74s**.
- `python3 -c "import ast,sys; ast.parse(open('app/main.py').read())"`: **exit 0**, 출력 없음.
- `grep -c "read_bytes()" app/api/canonical_documents.py`: 출력 **`0`** (grep은 일치 없음으로 exit 1).
- `git diff --check`: **exit 0**.
- `git status --short`: 변경 파일은 지정 6개 경로뿐이다.

운영 DB migration 검증은 브리프의 기존 실측을 참고했으며 R2에서 재실행하지 않았다. 커밋 및 푸시는 상위 사용자 규칙에 따라 실행하지 않았다.
