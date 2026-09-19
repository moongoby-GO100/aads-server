# B-02 review infrastructure recovery and independent verdict

## Incident and root cause

`runner-cd3ff19a` and `runner-6e9171c4` repeatedly recorded HTTP 200 followed by
`REVIEW_MODEL_NO_RESPONSE`. The review boundary assumed every successful model
call returned a plain string. Structured OpenAI-compatible responses and
Anthropic content blocks therefore had no canonical extraction contract. Empty,
whitespace-only, truncated, and merely JSON-shaped responses were also not
distinguished with durable per-attempt evidence. A parseable but incomplete JSON
object could be scored from default values.

The recovery keeps model selection, deadlines, preservation gates, and
`code_reviews` storage intact. It routes calls through
`anthropic_client.call_llm_with_fallback()`, extracts supported response shapes,
requires a complete verdict and five bounded scores, and persists bounded
per-attempt shape/length/hash/preview or timeout/error evidence. Invalid output
remains fail-closed as `REVIEW_PARSER_FAILURE`; genuinely empty output remains
`REVIEW_MODEL_NO_RESPONSE`.

## Recurrence prevention

- The same review fixture is exercised as an OpenAI-compatible structured
  response and must produce a non-empty `APPROVE` verdict.
- Parseable but incomplete verdicts are rejected.
- Response evidence is bounded and hashed before being stored in review feedback.
- No legacy API-key environment-variable reference or direct external LLM REST
  call is introduced.

## Independent review of `1db5bade85775ae596769959105aa4a829f69b25`

Verdict: **REJECT / BLOCKED**.

The commit adds tenant dependencies to only a subset of the goal router. The
same mounted router still exposes tenant-unscoped auxiliary endpoints including
board, documents, session lookup, candidates, owners, approval policy, lead,
pause/resume/restart, halt/direct, milestone confirm/rewind, bulk advance,
task-status, reconcile, and release-evidence. Because these endpoints read or
mutate goal-linked data without requiring the tenant context, the claimed full
tenant isolation is not established.

The candidate-only blocking audit enumerated every mounted `/goals` route and
failed with 21 unscoped endpoints. It is not installed as a permanently failing
main-branch test; its findings are carried by the B-02R remediation milestone.
The reviewed SHA must not be accepted until an equivalent regression test passes
with tenant-scoped service/SQL behavior behind each endpoint; adding a parameter
without enforcing it in database predicates is insufficient.

## STEP 0 preservation classification

- 유지: model routing DB lookup, input validation, preservation/scope hard gate,
  deadline and fallback loop, weighted verdict calculation, `code_reviews` save.
- 수정: central model-call boundary, response extraction, structured verdict
  validation, failure evidence retention.
- 신규: response-shape fixture tests and the independent tenant-isolation
  rejection record.
- 삭제: 없음.

## Selected change set

Release SHA is intentionally not claimed here. The selected files are
`app/services/code_reviewer.py`,
`tests/unit/test_code_reviewer_flag_classification.py` and this postmortem.
