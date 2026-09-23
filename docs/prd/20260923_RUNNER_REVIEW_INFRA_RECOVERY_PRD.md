# Runner review infrastructure recovery — PRD and design

Status: implementation target, 2026-09-23. Scope: AADS runner review transport, durable asynchronous review, review-hold recovery, and review model configuration. Code-quality rejection remains fail-closed.

## Incident evidence

- 2026-09-21 and 09-22 KST: 79 and 38 `REVIEW_MODEL_NO_RESPONSE` review-hold transitions. Stored attempts show Codex timeouts and Claude weekly-limit errors. Review model order was changed on 09-23 09:21 KST; this reduced the no-response pattern but left an unsupported `claude-opus-5-5` first candidate.
- 09-23 09:35:48–09:35:58 KST: `runner-09ea1681` received HTTP `000` on three review enqueues while blue (`127.0.0.1:8100`) was being synchronized and green (`8102`) was active. The runner uses a fixed blue URL; the review-hold sweeper already uses local nginx's active route.
- 09-23 10:05:17–10:14:20 KST: `runner-57d9247e` received 202, but the durable review stayed `running` beyond the runner's 540-second polling budget. The originating process left no per-attempt verdict. The later sweeper replayed the ID with a different payload (original `files_changed` length 3; replay 0), received 409, then succeeded with a new request. Fourteen old `running` rows demonstrate missing crash recovery.
- Both affected jobs eventually received genuine `REQUEST_CHANGES` verdicts. This document does not relabel those code findings as infrastructure faults or authorize approval.

## Product and safety goals

1. A runner always sends review traffic through the active local API route, not a hard-coded blue or green slot. Remote runners keep an explicitly configured AADS URL.
2. A 202 review request either reaches a durable terminal verdict or can be resumed from its original stored payload after worker loss. A request ID never aliases a changed payload.
3. Server execution finishes within a hard deadline; a slow model cannot indefinitely retain `running`. Reclaimed attempts are fenced so a late old worker cannot overwrite the new result.
4. Review-hold recovery reuses the original persisted request and its exact payload. If no request was stored, it creates a fresh ID. HTTP 000 must not consume model retry budget. Code rejection cannot become approval through this recovery path.
5. Review candidates do not include a model the live relay rejects. Model selection stays CLI-only and operator-owned.

## Design

- Runner and sweeper use `http://127.0.0.1` local nginx by default. Release/cutover checks must verify the host route maps to the active API. A configured remote URL remains an override. Transport logs preserve HTTP status and request ID.
- `code_review_requests` is the source of truth for request payload and verdict. POST `/code-diff/requests` remains idempotent: same ID + same hash returns the existing state; same ID + different hash returns 409 without mutating it.
- Add POST `/code-diff/requests/{request_id}/resume`. It does not accept new review text. `completed` remains immutable; `queued` is scheduled; `failed` is requeued; `running` is requeued only after the stale threshold exceeds the server hard deadline. An attempt counter fences completion/failure writes from earlier workers. Duplicate schedules are safe because claim is `queued`-only.
- Wrap the model review in an outer hard deadline. Timeout is stored as a failed, retryable request, never an APPROVE verdict. A process killed before persistence leaves a stale `running` row recoverable through `/resume`.
- Sweeper first GETs the saved request ID and polls/resumes it. Only a 404 (no durable request) permits creation of a fresh ID; it must then persist that ID on the job. The sweeper does not reconstruct a POST with an existing ID. A completed verdict is read from DB before any new model call.
- Remove `claude-opus-5-5` from current `AI_REVIEW` order until relay contract and live behavior agree. Keep at least two supported CLI candidates and preserve the prior DB value for rollback.

## Acceptance tests

| ID | Scenario | Required result |
|---|---|---|
| AC01 | Blue standby restart while green active | Runner's review POST reaches active nginx; no HTTP 000 caused by fixed 8100. |
| AC02 | Same ID and payload retried | One durable row and at most one active claimed attempt. |
| AC03 | Same ID with altered diff, instruction, or files | 409; original row/state unchanged. |
| AC04 | Worker dies after claim | Resume uses stored payload, increments attempt, reaches a terminal state; old attempt cannot overwrite it. |
| AC05 | Model exceeds hard deadline | Request becomes failed/retryable within runner polling budget; no indefinite `running`. |
| AC06 | Sweeper receives 202/running, completed, failed, or 404 | Poll/resume original, consume completed, recover failed, or create a new ID respectively. No 409 from reconstructed content. |
| AC07 | Review returns REQUEST_CHANGES or preservation FLAG | Remains blocked; only a valid APPROVE can move to approval queue. |
| AC08 | Blue/green release and replay monitoring | Both slots same image, routed health passes, no new review-infra event during five-minute observation. |

## Rollout and rollback

Implement and test in a clean isolated worktree. Commit and push an immutable release SHA, then blue/green deploy API via `deploy.sh` only when deployment gates and active streams allow it. Synchronize standby from the same image. Install runner script only through an idle-safe runner restart; never interrupt active jobs. Change the DB review model list by a compare-and-swap transaction after recording its prior value. If routed health or review smoke tests fail, roll back routing and restore the prior model list. Do not delete historical requests; reconcile or mark stale records only after ownership checks.
