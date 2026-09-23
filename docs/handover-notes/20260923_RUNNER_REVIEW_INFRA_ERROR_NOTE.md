# Error note — runner review infrastructure, 2026-09-23

Observed signatures: `review_infra_failed ... http=000 ... REVIEW_API_UNAVAILABLE`, `review_infra_failed ... http=202 ... REVIEW_TIMEOUT`, `request_id가 다른 payload에 이미 사용되었습니다`, and persistent `code_review_requests.status=running`.

Confirmed causes:

1. Runner default `AADS_API_URL=http://127.0.0.1:8100` pins blue. At 09:35 KST blue was starting while the active API was green:8102; all three enqueues returned HTTP 000. The sweeper already routes through local nginx.
2. An accepted review remained `running` past the runner's 540-second wait. The exact underlying model delay is not proved by the retained DB record. The background task has no outer hard timeout, persistent lease, or automatic exact-payload restart after process loss; 14 old `running` rows were observed.
3. Sweeper reused an existing UUID while building a different JSON payload from `pipeline_jobs`; the original request had three `files_changed` entries and the replay had none. The API correctly rejected this with 409, but recovery spent a retry and left the original request running.
4. Current `AI_REVIEW` configuration starts with `claude-opus-5-5` although the live Claude relay rejects that exact ID with `unsupported_claude_model` 400. Recent successful reviews then fell back to `codex:gpt-6-sol`.

Do not infer infrastructure failure from `review_failed: verdict=REQUEST_CHANGES`; the two affected jobs were later legitimately rejected on code/scope grounds. Review-hold is a fail-closed intermediate state, not approval.

Prevention contract: active-route review transport; immutable request-ID/payload pairing; server-side hard deadline and fenced attempt; exact-payload resume; CLI model list checked against live relay; regression tests for blue restart, 202 timeout, 409, and old worker completion. Fix commit and production verification are recorded separately when available.
