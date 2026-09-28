# Smart Browser direct repair and verification

Goal: `1a463229-5bbe-4c7e-b020-71b157acc241`.
Scope: repair existing M1 candidate, preserve user resources and unrelated edits,
and verify prerequisites without claiming operational milestone completion.

## M1 implementation

Preserve existing authentication, ownership, routing and lease functions. Modify
observation/stream state functions in `app/services/pc_agent_manager.py`, heartbeat
and offline metadata in `app/api/pc_agent.py`, and telemetry in `pc_agent/agent.py`.
New helpers format persisted observations and expire/control stream ACKs. No route,
table, or existing function is deleted. PC version advances 1.0.74 to 1.0.76.

Stream requests must be registered before socket send; a frame cannot acknowledge
start/stop. Missing ACK expires after ten seconds, even with continuing frames.
Late ACK and old-connection ACK cannot revive success. Failed sends restore state.
Only one unacknowledged stream control is allowed per PC; start callers retry after
ACK or timeout. Stop waits for ACK/deadline so last-subscriber cleanup is retained.
Numeric bounds are checked before float conversion so enormous
JSON integers cannot disconnect a PC's heartbeat.
Windows probes run outside the WebSocket event loop. Offline samples retain their
absolute timestamp; empty resource payloads cannot refresh a sample. OS event count
is a bounded System log sample in a sixty-second window, with a lower-bound flag.

Regression evidence must include failures on candidate `81541b13` and passing
tests on the repair. Operational completion additionally requires real Windows
canary samples and before/after collection overhead. No process termination.

## Remaining milestone gates

| Stage | Required operational evidence |
| --- | --- |
| M2 | M1 accepted; two chat work keys, restart recovery and cross-tenant denial |
| M3 | M2 accepted; authenticated chat, two lanes, interaction ACK and reconnect captures |
| M4 | M1–M3 accepted; owned expired resources only, dry run and ownership revalidation |
| M5 | M4 accepted; every PC version/hash, offline PC reconnect, Windows 24-hour observation |
| M6 | M3 accepted; explicit lane/egress and correctly scoped Vault login |
| M7 | M3/M6 accepted; Coupang Eats login fragments, approved replay from another session |
| M8 | M5/M7 accepted; sales source reconciliation and replay |
| M9/M10 | M3/M6 accepted; corporate/personal Shinhan credentials and actual login evidence |
| M11/M12 | M9/M10 accepted; transaction source reconciliation, isolation, deduplication |
| M13 | M5/M9–M12 accepted; approved skills and actual cross-session replay |

No milestone is completed by unit tests, HTTP 200, or a Runner terminal state.
No payments, transfers, account changes, user-window closure or bulk Chrome exit.
Release: reviewed committed SHA, isolated clean source, canonical bluegreen gates;
failed routed health rolls back routing. PC rollout requires known-good ZIP rollback.

## Observed readiness

Direct tool inspection found two online PCs and one offline PC. The first PC's
RAM was 91.8% at a point sample; this is pressure evidence, not proof of a freeze.
The installed VERSION on that PC was 1.0.74. E2E Vault listing exposed no Shinhan
entry; this is not evidence that other vault stores contain none. The approved
recipe list contained only the Coupang Eats login-page fragment for these workflows.
Dashboard login failed after bridge retry; ARIA confirmed the login page, and
public login/API health returned 200. Both API containers were healthy.
The same-tenant Agent Vault separately contains an active corporate Shinhan
credential; the E2E Vault result must not be treated as its absence.

Windows exact-function observe-only probe on PC `62405e70-e98`: before cold
53.32ms, after cold 221.67ms, after cached 0.02ms; resource collection 172.0ms.
Single sample, not a statistically representative benchmark or installed rollout.
Evidence: `/root/aads/qa_reports/smartbrowser-direct-20260929/windows-observe.json`.
