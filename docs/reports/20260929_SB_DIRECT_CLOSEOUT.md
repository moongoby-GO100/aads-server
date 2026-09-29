# Smart Browser direct repair — candidate, not operational completion

Goal: `1a463229-5bbe-4c7e-b020-71b157acc241`.
Base: `f232ce1f`; branch: `fix/sb-direct-closeout-20260929`.
Canonical handover: AADS / verification / `sb-direct-closeout-20260929`.

## Verified scope

Existing shared-main dirty files were preserved. Failed runner candidates were
inspected against their original instructions and actual code, rather than
automatically approving or resubmitting their scores. Changes are isolated in
`/root/aads/wt/sb-direct-closeout-20260929`.

| Milestone | Direct action | Remaining operational evidence |
|---|---|---|
| M1 observation | Fence WebSocket replacement, record connection identity from accept, preserve failed/legacy/offline status, bound owner-scoped observation cache; real PostgreSQL regression | Windows observe-only overhead and sustained observation |
| M2 ownership | Reject remote legacy-scope adoption; require matching process/profile/work key/tenant/chat on recovery and commands; propagate verified scope through API and adapter | Local legacy-session scope recovery, PC canary, two real Windows chats |
| M3 two lanes | Bind chat live commands to authenticated tenant/chat; request fields cannot override binding | Authenticated server/PC streaming, clicks, input, scroll, reconnect and screenshots |
| M4 reclaim | Reviewed `3048210d`: creation-failure close and activity-check/close race remain; dangerous candidate not integrated | M1–M3 evidence and explicit canary/reclaim gate before any resource close |
| M5 release | Canonical POSIX file ordering and ZIP platform metadata; new 1.0.77 candidate and regenerated manifest | No PC rollout; M4 prerequisite, Windows canary/ZIP verification, 24-hour observation and API release certification |
| M6 Vault/egress | Exact tenant/origin/work-key match, reject ambiguous or wrong account, preserve configured login path, check actual redirect origin before input, report actual egress IP without treating access as login success | Selected-lane real login and exact account/store screen evidence |
| M7 fragments | Fix actual recorder so generic login-submit click/press cannot downgrade to READ; preserve stronger risks | Trusted screen evidence, fragment registration/approval and other-session replay; rejected disconnected helper candidate not integrated |
| M8 sales | Reviewed approved `48bc187a`: SIGALRM main-thread requirement conflicts with API threadpool; unexpected errors can discard existing records; no approval/integration | Correct timeout/data-preservation contract, M5/M7 evidence, original-screen totals and replay |
| M9 bank login | Salvage `f7148efd` after runner terminal failure; narrow financial detection, retain normal retry/error contracts, fail closed without authenticated actor, redact financial outputs and retain verifiable receipts | Approved exact-domain recipe, correct Vault identity and PC session, real masked-account login evidence; no bank actions executed |
| M10 personal login | Existing work-key/connector inspected; no successful split-recipe evidence | M3/M6, exact personal credential, separate recipe and real login |
| M11/M12 transactions | Prior `done` runner outputs explicitly report no implementation/verification; not counted complete | M9/M10 first; original statement count/sum/balance reconciliation |
| M13 reuse | Prior reviewed `7fefa10e` preserved; executor has another session's dirty ledger, so not overwritten | M9–M12/M5, trusted registration, other-session skill replay |

## Evidence and limitations

- `scripts/run_unit_tests.sh` executes candidate sources using the operational
  runtime image in disposable test containers. Logs:
  `/root/aads/verification/sb-direct-20260929/`.
- Initial integrated observation/ownership/Vault/recorder/live suite: 413 passed.
  Final scope-propagation, financial receipt/privacy, recipe registration, and
  PC packaging suite: 333 passed. Bank/finance regression: 319 passed. Release-only suite: 29 passed. Suites overlap
  and must not be summed. Logs are retained in this directory and the DB handover. No fixture is operational evidence.
- `tests/unit/test_pc_agent_event_query_postgres.py`: real disposable PostgreSQL
  test passed. It requires `PC_AGENT_TEST_POSTGRES_DSN`; ordinary suite runs skip
  it when no isolated DB is supplied. No service DB writes were used for this test.
- `/chat` server-Playwright screenshot:
  `https://aads.newtalk.kr/screenshots/screenshot_20260929_130425_305a27.png`.
  This shows a rejected login form, not an authenticated chat or two-lane E2E.
  HTTP `/chat` returned 307; direct `/api/v1/health` returned status=ok;
  both observed API containers were healthy. These are baseline checks, not
  candidate deployment verification.
- Four connected PCs were returned by `device_list`; the CEO PC `system_info`
  command completed. No update, restart, tab closure, reclaim, transfer, payment,
  real-bank login or transaction collection was performed.
- Current DB includes an active `bizbank.shinhan.com` Vault entry. The old claim
  that no Shinhan credential exists is stale. Its suitability for the requested
  login and the recipe's exact origin remains unverified; do not substitute it.
- Milestone DB states were 11 blocked and 2 pending. No self-approval or direct
  milestone status update was performed. No full-goal completion is claimed. During final verification, M3
  `runner-05b52c9c` became active; overlapping work is kept on this separate
  candidate branch and not merged into its working tree.

## Rollout boundary

This is a reviewable candidate branch. Do not promote it solely because unit
tests pass. PC scope migration/canary and required predecessor evidence must be
established first. API release, if authorized after those checks, uses
`deploy.sh bluegreen` with immutable SHA image, candidate health, short nginx
lock, fenced ownership, same-digest standby and five-minute monitoring.
Before deployment, revert the relevant commit to discard this candidate. After
deployment, use the prior certified release and normal blue/green rollback.
Costs are unmeasured.
