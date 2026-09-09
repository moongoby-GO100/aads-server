# OHVIS authenticated trace-ingest contract v1.0

Status: AADS receiver hardened and ready for release; GO100 sender connection pending.

## Endpoint

`POST /api/v1/ohvis/llmops/trace-ingest`

The endpoint is exempted from the user JWT middleware by exact path only and
performs its own mandatory service credential verification. Use
`Authorization: Bearer <one-time-provisioned-ingest-token>` and
`Content-Type: application/json`.

Authentication runs before request-body validation. A request without a valid
service credential therefore returns `401` even when its JSON body is missing
or malformed; unauthenticated callers cannot use validation responses to probe
the ingest schema.

## Request v1.0

Required fields:

- `schema_version`: exactly `"1.0"`
- `project`: exactly `"GO100"`
- `external_trace_id`: the original GO100 trace ID, 1..200 printable characters

Optional fields are `graph_run_id`, `session_id`, `run_type`, `status`, `model`,
`input_summary`, `output_summary`, `latency_ms`, `cost_usd`, `quality_score`,
`error`, `tags`, `metadata`, and up to 50 `tool_calls`. The serialized request
is limited to 65,536 bytes. Summary fields are clipped by the canonical LLMOps
store and sensitive metadata keys are recursively redacted.

## Response v1.0

The successful response contains `accepted`, `deduplicated`,
`external_trace_id`, central `trace_id`, `project`, `schema_version`, and
`ingested_at`. A replay uses the deterministic key
`external:{project}:{external_trace_id}`, returns the same central trace ID,
and does not insert duplicate tool calls. Short composite keys remain readable;
keys over 200 characters use a deterministic SHA-256 suffix. Locking, lookup,
and storage use that same bounded key while `external_trace_id` retains the
complete original identifier.

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

## GO100 bridge completion criteria

GO100 must store the provisioned token in its approved secret manager, send the
existing trace ID unchanged, retry with exponential backoff/outbox semantics,
and keep trading independent of AADS availability. AADS and GO100 must then
verify an authenticated send plus replay: one central trace row, the same
central trace ID, and no duplicate tool calls. This sender-side work is outside
the AADS active-project change in this release.
