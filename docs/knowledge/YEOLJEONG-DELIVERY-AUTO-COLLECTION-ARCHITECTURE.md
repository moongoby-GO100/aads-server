# Yeoljeong delivery auto-collection challenge policy

## Challenge handling

`app/services/auth_challenge_orchestrator.py` deterministically classifies a
portal observation as `captcha_required`, `otp_required`, `login_required`,
`portal_error`, `collectable_page`, or `unknown`. An optional provider may be
added later, but its JSON output is accepted only when the state is in this
allowlist; invalid output becomes `unknown`.

CAPTCHA solving, OCR/image interpretation, OTP retrieval, external solvers,
stealth bypass, and rate-limit avoidance are prohibited. A challenge creates
an `action_required` ledger row with a masked screenshot reference when
available. The same `business_id x branch x service` work key and browser
session are retained in diagnostics with a non-secret resume reference.

An operator may complete the challenge in the connected PC Agent browser.
Challenge values are accepted by the server only with explicit operator
approval, are transient, and are never written to the ledger, logs, fixtures,
or documentation. Reclassification and collection then continue in the same
session. Challenge attempts are capped at three and the challenge observation
expires after 20 minutes. Late results cannot reopen a terminal run.

## Session and cleanup policy

Portal work keys are deterministic per service, business, and branch. Browser
sessions are reused for a work key; session recreation is bounded to the
existing retry policy. `close_portal_browser_on_complete` defaults to true,
while an explicit debug override may keep the browser open. Completion, error,
timeout, and terminal challenge paths retire the session and close its tabs
when the close policy is enabled, preventing unbounded browser-window growth.

## Completion criteria

The `/yeoljeong-finance/completion-matrix` endpoint reports each registered
account x channel x data type (`sales`, `settlements`, `reviews`, `ads`). A
type is complete only when a ledger row exists for that exact account scope;
row existence elsewhere does not satisfy it. Missing data is `incomplete`,
and a latest portal challenge is `action_required`.
