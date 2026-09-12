# AADS-CHATMOD-WP04-SERVER-R5 구현 기록

- 기준 시각: 2026-09-13 KST
- 작업 기준: 로컬 `HEAD`와 로컬 `origin/main` 참조 모두
  `97481d1c771ee3f069265b69dcfdb4f398689e8c`
- 작업 위치: 기존 main 작업 디렉터리와 분리된
  `/tmp/aads-wt-runner-2d903d0b`
- 원격 fetch: 상위 필수 규칙의 "파일 생성/수정/삭제만 허용"에 따라 실행하지 않았다.
  편집 전 로컬 `origin/main` 참조와 `HEAD`가 요청된 정확한 SHA와 일치하고 작업 디렉터리가
  clean인 것을 확인했다.
- 범위: WP04 서버만. DB migration은 어떤 DB에도 적용하지 않았다.

## STEP 0 기존 구현 조사 및 분류

| 기존 항목 | 분류 | WP04 처리 |
|---|---|---|
| `chat.get_messages`, `chat.get_workspace_session_messages`, `_get_messages_payload` | 수정 | 무버전 v1 배열/legacy cursor 계약을 유지하고 HTTP GET에서는 `read_only=True`; 명시적 v2에서만 signed composite cursor read model 사용 |
| `chat.get_streaming_status` | 수정 | 기존 response projection을 유지하되 GET의 execution/message/session repair DML 제거; 동일 조회 projection을 새 read-only query service로 이동 |
| `chat.get_last_response` | 수정 | stale execution 정리·message 승격을 GET에서 제거하고 `repair_required`를 포함할 수 있는 순수 projection으로 변경 |
| `chat.get_chat_capabilities` | 수정 | v1 기본값을 유지하며 read model/cursor/revision/repair capability와 활성화 gate를 additive하게 광고 |
| `chat.get_stream_snapshot` route | 유지 | 권한·v2 negotiation·공개 symbol 유지; service가 migration gate 이후 atomic checkpoint를 읽도록 개선 |
| `chat_protocol.get_stream_snapshot` | 수정 | gate 전 WP03 query/shape 유지; gate 후 checkpoint의 content/coverage 쌍과 monotonic revision 사용 |
| `chat_protocol.negotiate_contract_version`, SSE parser/adapter | 유지 | WP03 v1/v2 transport 동작을 변경하지 않음 |
| `chat_protocol._version_from_datetime` | 유지 | legacy snapshot 호환 fallback으로 그대로 보존 |
| `chat_service.list_messages`, `list_messages_cursor` | 수정 | 기본 service writer 호환은 유지; HTTP read-only 호출에서 duration/flag/placeholder를 메모리 projection만 수행 |
| `chat_service.get_message` | 수정 | duration lazy hydration의 GET DB backfill을 제거하고 payload hydration만 수행 |
| `_hydrate_message_response_durations`, `_repair_completed_execution_message_flags` | 수정 | 기존 호출자 기본 writer 동작을 보존하는 `persist=True` 기본값 추가; GET은 `persist=False` 사용 |
| `_promote_inactive_streaming_placeholders` | 유지 | 기존 symbol/writer를 삭제하지 않고 explicit writer 호환을 위해 보존 |
| `_resume_interrupted_streams_legacy_disabled` | 유지 | preservation hard gate에 따라 그대로 보존 |
| `_stale_placeholder_cleanup_loop` | 수정 | GET에서 분리한 completed projection repair를 active-slot fenced worker로 명시 호출 |
| 기존 session/message/artifact/execution DB 접점 | 수정 | 기존 테이블을 대체하지 않고 revision/outbox/checkpoint trigger와 message metadata를 additive migration으로 확장 |
| v2 view/changes API, cursor codec, read-only query service | 신규 | tenant/user/session/filter/projection/direction scope와 TTL이 서명된 `(created_at,id)` keyset 제공 |
| admin repair endpoint와 fenced repair service | 신규 | active slot, terminal execution, owner instance/epoch/lease 조건으로 claim·repair·release |
| 삭제 항목 | 삭제 없음 | 기존 공개/비공개 symbol과 legacy 구현은 삭제하지 않음 |

## 구현 결과

### Read-only 및 repair 분리

- 신규 query module은 모든 DB transaction에 `readonly=True`를 지정한다.
- v1 message GET도 response duration, completed execution flag, inactive placeholder를 DB에
  쓰지 않고 동일한 표시 projection으로 계산한다.
- streaming-status와 last-response의 기존 repairing 구현은 별도 legacy symbol로 보존하되
  등록된 GET route에서는 호출하지 않는다.
- explicit admin command와 scheduled active worker만 completed projection repair를 수행한다.
  writer는 completed execution을 새 `owner_epoch`로 claim하고 동일 owner/epoch로만 lease를
  release한다.

### v2 read model과 cursor

- `GET /api/v1/chat/sessions/{session_id}/view?contract_version=2`
- `GET /api/v1/chat/sessions/{session_id}/changes?after_revision=...`
- 기존 message list의 명시적 `contract_version=2`, `direction=before|after`
- 기존 message detail의 명시적 `contract_version=2`는 content version/completeness와
  projection-bound private ETag/304를 제공한다.
- cursor payload는 HMAC-SHA256, TTL, schema version과 tenant/user/session/projection/
  include-streaming/direction scope를 검증한다. 정렬과 경계 조건은 모두
  `(created_at,id)`를 사용하며 cursor는 post-fetch dedupe 전 원본 경계에서 만든다.
- 무버전 요청은 계속 contract v1이며 기존 `sort=desc` 배열과 timestamp cursor response를
  유지한다.

### Revision/outbox/checkpoint migration

- `174_chat_read_model_expand.sql`: transaction 안에서 additive columns/tables/backfill/
  trigger를 구성한다.
- `chat_session_revisions`는 `(tenant_id,session_id)` PK이고 visible session/message/artifact/
  execution mutation과 같은 transaction에서 session revision row를 lock하며 monotonic counter를
  증가시킨다.
- 같은 trigger transaction에서 changed-ID/tombstone outbox event를 기록한다.
- execution 및 assistant message mutation은 한 helper로 checkpoint를 갱신하여 content,
  content version, tool state, coverage cursor를 같은 transaction의 한 row에 저장한다.
- `streaming_placeholder`의 content/tool-only token flush는 SSE ledger가 담당하므로 revision/outbox를
  생성하지 않고, recovery checkpoint는 최대 초당 1회로 제한한다. intent/status가 바뀌는 terminal
  write는 제한 없이 최종 revision과 checkpoint를 기록한다.
- `175_chat_read_model_indexes_concurrently.sql`: `BEGIN/COMMIT` 없이 top-level
  `CREATE INDEX CONCURRENTLY`만 수행하도록 분리했다.

### 활성화 gate

- dedicated `AADS_CHAT_CURSOR_HMAC_SECRET`이 32 bytes 미만이면 v2 message/view가 503으로
  fail closed한다.
- WP04 `read_model.production_ready`는 feature enable, migration ready, dedicated secret,
  cross-version verification이 모두 참일 때만 참이다. 전체 protocol `production_ready`는
  후속 browser/generation gate가 남아 있어 false를 유지한다. 예시 환경 기본값은 모두
  false/empty다.
- checkpoint migration flag 전에는 WP03 legacy snapshot query를 유지한다.

## 지시서에 파일명이 명시되지 않은 변경 사유

- `.env.example`: cursor secret/TTL과 migration/cross-version/feature gate의 안전한 기본값을
  문서화하기 위해 수정.
- `app/models/chat.py`: Pydantic/OpenAPI typed v2 response 및 repair command schema 추가.
- `app/routers/chat.py`: additive endpoints, v1 default negotiation, GET read-only routing,
  explicit admin writer 연결을 위해 수정.
- `app/services/chat_protocol.py`: capability 및 atomic checkpoint snapshot 계약 연결을 위해 수정.
- `app/services/chat_service.py`: 기존 GET 경로의 숨은 DML을 opt-out하고 worker repair를 연결하기 위해 수정.
- `app/services/chat_read_model.py`, `app/services/chat_repair.py`: query/writer import graph를
  분리하기 위해 신규.
- `migrations/174_*`, `migrations/175_*`: transactional schema와 non-transactional concurrent
  index를 분리하기 위해 신규.
- `tests/unit/test_chat_modernization_wp04.py`,
  `tests/integration/test_chat_modernization_wp04_db.py`: T10/T11/T19/T35/T36/T43 집중 검증을 위해 신규.
- 이 구현 기록 파일: STEP 0 조사, 변경 범위, gate와 검증 증거를 남기기 위해 신규.

## 검증 결과

- WP04 + WP00/WP03 + status/chat-service: `127 passed, 1 skipped`
  - skip 1건은 `AADS_WP04_TEST_DATABASE_URL`이 없는 경우에만 skip되는 실제 PostgreSQL
    read-only integration test다.
- 최종 detail/ETag 보강 후 WP04 집중 재검증: `6 passed`
- 최종 WP00/WP03/WP04 + status/receipt/lease/resume/watchdog 묶음: `80 passed`
- receipt/lease/resume/watchdog regressions: `35 passed`
- legacy status/last-response source contract: `2 passed`
- tenant route guard: `1 passed`
- Python source compile sweep: `357` files 통과
- Ruff critical (`E9,F63,F7,F82`) on all changed Python/test files: 통과
- registered chat GET handler AST direct DML scan: `0` calls
- `git diff --check`: 통과
- OpenAPI generation: OpenAPI `3.1.0`, 전체 `643` paths/`342` schemas; WP04 관련 5 paths와
  message/page/view/changes/repair typed schemas 5개 확인
- 실제 PostgreSQL integration: 전용 test DSN 미설정으로 skip; production DB 접근/변경 없음

## 호출처 영향과 롤백

- 삭제가 없어 삭제 호출처 영향은 없다.
- 무버전 및 `contract_version=1` caller는 기존 response 형태를 유지한다.
- v2는 additive query/route이며 secret 또는 migration이 준비되지 않으면 명시적 503으로
  fail closed한다.
- 코드 롤백 시 v2 route/query/worker 연결을 이전 코드로 되돌려도 additive DB columns,
  tables와 outbox data는 보존할 수 있다. 먼저 `AADS_CHAT_V2_READ_MODEL_ENABLED=false`로
  v2 활성화를 닫고, 기존 v1 read projection을 계속 사용할 수 있다.

## 타임아웃 산출물 회수 검증

Runner 타임아웃 뒤 보존된 worktree를 2026-09-13 KST에 직접 재검수했다.

- WP00/WP03/WP04와 receipt/resume/retry/chat-service/status/lease/watchdog 회귀 묶음:
  `163 passed, 1 warning`.
- WP03+WP04 집중 묶음: `22 passed`.
- 변경 Python 파일 `py_compile`: 통과.
- 변경 Python 파일 Ruff critical (`E9,F63,F7,F82`): 통과.
- `git diff --check`: 통과.
- 실제 PostgreSQL migration 적용 및 브라우저 E2E는 미실행이다. migration과 v2 기능은
  활성화 플래그 기본값이 false이므로 해당 검증과 배포 인수 전까지 production-ready로
  판정하지 않는다.
