# OHVIS authenticated trace-ingest contract v1.0

Status: AADS receiver hardened and ready for release; GO100 sender connection pending.

## Endpoint

`POST /api/v1/ohvis/llmops/trace-ingest`

The endpoint is exempted from the user JWT middleware by exact path only and
performs its own mandatory service credential verification. Use
`Authorization: Bearer <one-time-provisioned-ingest-token>` and
`Content-Type: application/json`.

Authentication runs before the request body is read or parsed. The route takes
the raw `Request`, so the credential dependency resolves first and the body is
decoded only afterwards. A request without a valid service credential therefore
returns `401` even when its JSON body is empty, truncated, or not JSON at all;
unauthenticated callers cannot use validation responses to probe the ingest
schema. Declaring the body as a typed parameter is not sufficient — FastAPI
decodes a declared body *before* it solves dependencies, so a malformed body
answered `422` to anonymous callers.

## Request v1.0

Required fields:

- `schema_version`: exactly `"1.0"`
- `project`: exactly `"GO100"`
- `external_trace_id`: the original GO100 trace ID, 1..200 printable characters

Optional fields are `graph_run_id`, `session_id`, `run_type`, `status`, `model`,
`input_summary`, `output_summary`, `latency_ms`, `cost_usd`, `quality_score`,
`error`, `tags`, `metadata`, and up to 50 `tool_calls`. The serialized request
is limited to 65,536 bytes, checked against `Content-Length` and the raw body
before any parsing. Summary fields are clipped by the canonical LLMOps store
and sensitive metadata keys are recursively redacted.

External input is accepted strictly; every rule below answers `422`:

- unknown fields are rejected on the request and on each tool call
- `external_trace_id` and `graph_run_id` must be printable ASCII with no
  whitespace, no control characters, and no surrounding blanks
- `session_id`, when present, must be a UUID (it was previously dropped in
  silence when it was not)
- `run_type` and each tool call's `risk_tier`, `approval_state`, and `status`
  must be lowercase slugs (`^[a-z][a-z0-9_.-]*$`)
- text fields (`model`, summaries, `error`, `tags`, `tool_name`) must not carry
  control characters; a NUL byte cannot be stored in TEXT or JSONB and would
  otherwise abort the strict transaction and make the sender retry forever

As defence in depth the store also strips control characters from ingested
text and metadata keys before writing, so a direct caller cannot poison the
ledger either.

## Response v1.0

The successful response contains `accepted`, `deduplicated`,
`external_trace_id`, central `trace_id`, `project`, `schema_version`, and
`ingested_at`. A replay uses the deterministic key
`external:{project}:{external_trace_id}`, returns the same central trace ID,
and does not insert duplicate tool calls. Short composite keys remain readable;
keys over 200 characters use a deterministic SHA-256 suffix. A raw key that is
itself shaped like the hashed form (`external:{project}:sha256:{64 hex}`) is
hashed as well, so a sender cannot submit an ID that mimics the hashed key of a
long ID it does not own. Locking, lookup, and storage use that same bounded key
while `external_trace_id` retains the complete original identifier.

The globally UNIQUE `llmops_traces.trace_id` column is written as the bounded
external key, not as the raw sender ID. An external ID equal to an existing
internal harness `trace_id` used to raise a unique-constraint violation that
the `ON CONFLICT (trace_key)` clause does not cover, so that trace failed with
a retryable `503` on every attempt and never converged. Verified directly on
the production database inside a rolled-back transaction: the raw-ID insert
fails with `duplicate key value violates unique constraint
"idx_llmops_traces_trace_id"`, the namespaced insert succeeds, and zero rows
were persisted.

The trace row and all child tool-call rows are written inside one transaction.
External ingest uses strict writes: a child-write failure aborts and rolls back
the complete request instead of committing a partial trace.

Errors: missing/invalid credential `401`, credential/project mismatch `403`,
unsupported schema or malformed/oversized input `422`, and temporary auth/store
failure `503` with `Retry-After`. Internal exception details are not returned.

## Credential lifecycle

Internal administrators provision or rotate a client through:

- `POST /api/v1/ohvis/llmops/ingest-clients`
- `POST /api/v1/ohvis/llmops/ingest-clients/{client_id}/rotate`
- `DELETE /api/v1/ohvis/llmops/ingest-clients/{client_id}`

The raw token is returned once. PostgreSQL stores only its SHA-256 digest in
`llmops_ingest_clients`; comparison is constant-time and credentials are scoped
to one project. Never put the token in Git, chat, command output, or logs.

`{client_id}` is validated on the path (`^[a-z0-9][a-z0-9._-]+$`, 3..80). Rotate
previously built its request model from an unvalidated path segment, so a bad
`client_id` escaped as an unhandled `ValidationError` (`500`) instead of `422`.

## 변경 조사표 (유지 / 수정 / 신규 / 삭제)

외부 ingest 변경 전체(`de80a95e` 수신기 → `cfc02d62` 1차 보강 → 본 검수 반영분)를
대상으로 한다. 구분은 본 검수 반영분 기준이며, 앞 두 커밋에서 확정되어 이번에
손대지 않은 항목은 "유지"로 적는다.

### app/services/llmops_store.py

| 구분 | 대상 | 내용 |
| --- | --- | --- |
| 신규 | `sanitize_ingest_text()` | 저장 불가능한 제어문자(NUL 포함) 제거. 외부 입력 전용 |
| 신규 | `_HASHED_TRACE_KEY_RE`, `_INGEST_CONTROL_CHARS` | 해시 키 위조 차단·제어문자 패턴 |
| 신규 | `record_trace(trace_id_override=...)` | UNIQUE `trace_id` 컬럼 값 지정용 선택 인자 |
| 수정 | `external_trace_key()` | 해시 형태를 흉내낸 원문 키도 해시 처리 |
| 수정 | `_INSERT_TRACE_SQL` | `COALESCE(NULLIF($21::text,''), …)`로 override를 최우선 적용. $21 미전달 시 기존 순서 그대로 |
| 수정 | `redact_ingest_value()` | 문자열·키를 제어문자 제거 후 기존 `clip()`에 통과 |
| 수정 | `ingest_external_trace()` | 텍스트 필드 sanitize + `trace_id_override=trace_key` 전달 |
| 유지 | `record_trace()`/`record_tool_calls()` 기존 시그니처·비치명 동작 | 내부 호출부 7곳 무변경. `strict`/`trace_id_override`는 기본값 False/None |
| 유지 | `authenticate_ingest_client()`, `provision/revoke/mark_*`, 크기 검증, 마스킹 | 앞 커밋 확정분 그대로 |
| 삭제 | 없음 | 제거한 함수·분기·테이블 없음 |

### app/api/ohvis_llmops.py

| 구분 | 대상 | 내용 |
| --- | --- | --- |
| 신규 | `parse_trace_ingest_body()` | 인증 통과 후에만 본문을 읽고 검증. 크기 → JSON → 스키마 순 |
| 신규 | `llmops_trace_ingest_endpoint()` | 원본 `Request`를 받는 라우트. OpenAPI 본문 스키마는 `openapi_extra`로 보존 |
| 신규 | `_reject_control_characters()`, `_INGEST_*` 패턴 상수 | 외부 입력 엄격 검증 |
| 수정 | `TraceIngestRequest` / `TraceIngestToolCall` | `extra="forbid"`, ID 문자셋, `session_id` UUID, 라벨 슬러그, 제어문자 금지 |
| 수정 | `llmops_trace_ingest()` | 라우트 데코레이터만 분리. 인자 이름·순서(`req`, `client`)와 403/503 동작 동일 |
| 수정 | rotate·revoke `client_id` | `Path(pattern=…)` 검증 추가(기존에는 500) |
| 유지 | `require_trace_ingest_client()`, `_bearer_token()`, 401/403/503 매핑, 자격증명 수명주기 3종 | 앞 커밋 확정분 그대로 |
| 삭제 | `llmops_trace_ingest(client=Depends(...))`의 기본값 | 라우트가 아니게 되어 의존성 기본값이 오해를 부름. 기존 호출부는 전부 `client=`를 명시 전달하므로 호출 호환성 영향 없음 |

### tests/unit/test_ohvis_trace_ingest.py

| 구분 | 대상 | 내용 |
| --- | --- | --- |
| 신규 | 미인증 malformed 본문 401, 인증 후 422 형태, 엄격 입력 10종 파라미터화 | 인증 순서·외부 입력 회귀 |
| 신규 | 해시 키 위조·`trace_id` 네임스페이스·제어문자 제거 | 충돌 회귀 |
| 신규 | `RecordingConn` | `record_trace` INSERT 인자 직접 확인용 대역 |
| 이관 | `test_record_trace_preserves_bounded_trace_key`, `test_record_trace_strict_mode_propagates_insert_failure` | `test_ohvis_llmops.py`에서 옮겨옴(아래 삭제 사유 참조). 검증 내용은 동일 |
| 유지 | 인증·스코프·재생·마스킹·마이그레이션 기존 테스트 | 삭제 없음 |
| 삭제 | 없음 | 커버리지 제거 없음 |

### tests/unit/test_ohvis_llmops.py

| 구분 | 대상 | 내용 |
| --- | --- | --- |
| 삭제 | `cfc02d62`가 추가한 테스트 2건 | **사유: 지시서 허용 경로 밖 파일이다.** 검증을 잃지 않도록 동일 내용을 허용 파일인 `tests/unit/test_ohvis_trace_ingest.py`로 이관했고, 이 파일은 `de80a95e` 상태로 원복했다 |

### 지시서 허용 경로 밖 변경 이력

- `app/main.py`(정확 경로 예외 1줄), `migrations/163_…sql`(`llmops_ingest_clients`
  추가)은 **`de80a95e`**에서 발생했다. 지시서는 그 커밋을 "정본 수신기, 유지하라"로
  규정하므로 되돌리지 않았고, 본 검수 반영분에서는 두 파일을 건드리지 않았다.
- 본 검수 반영분이 수정한 파일은 허용 4개 경로(+`HANDOVER.md` 추가 기입)뿐이다.

## GO100 bridge completion criteria

GO100 must store the provisioned token in its approved secret manager, send the
existing trace ID unchanged, retry with exponential backoff/outbox semantics,
and keep trading independent of AADS availability. AADS and GO100 must then
verify an authenticated send plus replay: one central trace row, the same
central trace ID, and no duplicate tool calls. This sender-side work is outside
the AADS active-project change in this release.
