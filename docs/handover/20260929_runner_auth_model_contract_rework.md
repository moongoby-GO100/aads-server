# Runner authentication and model contract rework (2026-09-29)

Task: `AADS-RUNNER-AUTH-MODEL-CONTRACT-P0-20260929`. Clean worktree base: `6d20a6ebb80dd2145700347fa598da68db4ecca2`. Reworked artifact: `ec6e100219a8`. This candidate has not been released. The original runner evidence below predates the direct commit recovery described at the end.

## Scope and local evidence

- No active runner process was visible in this sandbox. The job database could not be queried because `psql` is unavailable here, so exact live job ownership remains unverified. No job was claimed or modified.
- JINAH and MAIN `auth.json` files exist, parse as JSON objects, are root owned, and have mode `0600`. Their contents were not printed. A JINAH read-only Codex model probe exited 1 without the exact receipt. The sanitized probe result did not establish a 401 cause or a provider response. No credential rotation was justified or performed. The previous Vault credential version reference is unchanged and unavailable here.
- `codex-cli 0.158.0` and Claude Code `2.1.280` are installed. The local contract maps `claude-opus` to `claude-opus-5-5`; live relay entitlement for that exact model remains unverified. Automatic runner and AI review cycles exclude the canonical ID and aliases. The runner adds `codex:gpt-5.6-luna` and `codex:gpt-5.6-sol` as distinct alternatives even when one Claude model consumes two slots.
- JINAH selection requires a fresh successful probe receipt tied to the credential file mtime. Any failed probe, including a CLI launch error or timeout, atomically replaces an existing receipt with a quarantine marker. Auth state reads and updates share a file lock. Invalid rate limit values, including NaN, fail closed.

## Review corrections

1. CLI errors and timeouts now revoke a still-valid JINAH receipt; a regression test exercises both exceptions.
2. A shared lock covers auth health reads and read-modify-write updates; concurrent quarantine regression coverage checks that both account entries survive.
3. The runner counts distinct Codex alternatives rather than total attempts; a two-Claude-slot test runs the actual shell segment under `set -e`.
4. Nonfinite `rate_limited_until_epoch` values are ineligible; a NaN regression test covers this case.

## Verification and outstanding external evidence

- `pytest -q tests/unit/test_runner_auth_model_recovery.py`: 13 passed.
- Bash syntax for both runner variants, Python compilation of touched modules, and `git diff --check`: passed.
- Extended test collection was blocked by missing `fastapi`; a separate Claude contract selection had 54 passed, 9 failed due to missing `asyncpg`, and 12 deselected. These are environment dependency failures, not passes.
- No live runner smoke reached a provider model call. A JINAH read-only CLI probe reached the CLI but returned no verified receipt. Runtime `MODEL_CONTRACT_REJECTED` and repeated 401 counts therefore cannot be certified from this sandbox.
- The canonical error book is database backed; the task forbids database modification, and the production 401 root cause is unverified. No entry was registered.
- At the original runner handoff, commit, push, API release, routed health, standby digest, and five-minute P0/P1 monitoring had no evidence in this worktree.

## Rollback references

- Credential: no rotation; retain the previous Vault credential version reference.
- Model routing: restore the model-slot order at base SHA `6d20a6ebb80dd2145700347fa598da68db4ecca2`.
- Routing: no deployment or cutover occurred; a later release must retain its prior routed digest as rollback reference.

## Direct commit recovery

- A host process check found no process with this worktree as its current directory. The staged patch was backed up before changing the detached worktree to `candidate/runner-auth-model-recovery-20260929-ef1f3198`.
- A normal commit reproduced the failure: `scripts/dup_guard.py` rejected the repeated account setup in the failed-probe and probe-exception tests. Python validation and import checks had passed. The known error-book entry is `runner.approval_commit_blocked_by_dup_guard`.
- Extracted that setup into `_seed_verified_jinah_accounts`; both tests retain their original assertions. The focused suite passed again: `bash scripts/run_unit_tests.sh tests/unit/test_runner_auth_model_recovery.py` reported 13 passed. Both runner scripts passed `bash -n`, and `git diff --check` passed.
- This recovery preserves a review candidate. It does not certify independent review, a live account/provider response, deployment, or runtime recovery. Hook bypass flags were not used.
