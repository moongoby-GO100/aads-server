# Claude CLI version convergence and gated auto-update — PRD

Status: implementation, 2026-09-23. Scope: AADS chat relay, Agent SDK wrapper, and local pipeline runner. Remote runners and Codex CLI are not in scope.

## Verified baseline

- The root crontab schedules `scripts/update_claude_all_servers.sh` at 04:00 KST, but that file is mode 0644. `/var/log/claude_update.log` contains repeated `Permission denied`; the scheduled updater has not been executing.
- Even if executable, that updater changes global host npm/Python packages (and Codex/remote hosts), not the release-image SDK bundle or `/root/aads/vendor/claude-cli/claude` selected by the local runner. The migration document itself recorded different host and Docker CLI versions.
- On 2026-09-23 the host `/usr/bin/claude` was 2.1.280 while both the container SDK bundle and runner vendor copy were 2.1.259. `claude-opus-5-5` requires CLI 2.1.280, so chat returned a deterministic HTTP 400. The emergency release embeds the official 2.1.280 artifact with its manifest checksum.

## Goals

1. Report the *effective* CLI path and version for host, active/standby chat slots, and local runner; do not equate installed versions with executed versions.
2. Detect new official stable CLI versions daily, verify manifest version, size, SHA256 and binary identity, then prepare a clean, committed candidate from the current `origin/main`. Never use mutable `latest` in the runtime image.
3. Run tests, immutable-image blue/green gates, an exact-model chat smoke, same-digest standby check, and five-minute P0/P1 monitoring before marking an update applied. A busy slot or missing evidence defers rather than forces the update.
4. Move the local runner to the same verified version only after the chat candidate is healthy and the runner has zero active jobs. Never kill a job for a CLI update.
5. Keep a known-good prior image/version for rollback. If any gate fails, leave the current route and runner binary unchanged; alert with the failing gate and SHA.

## Design

- Replace the broken broad cron entry with an AADS-specific updater timer. Do not reactivate `update_claude_all_servers.sh` as-is: it also updates Codex and remote hosts outside this request.
- A read-only audit command checks the live execution paths and official manifest and emits machine-readable drift/availability status. Missing tools, network failure, or unverified checksum are `unknown`, not "up to date".
- A prepare command uses an isolated worktree at freshly fetched `origin/main`, changes only the pinned CLI artifact version/checksum/test fixture, runs relevant tests, commits with hooks, and pushes by fast-forward only. It refuses unsupported major/minor version jumps, a dirty worktree, a changed base, or a checksum mismatch.
- An apply command delegates to `deploy.sh bluegreen` from that clean worktree. It verifies the resulting active image, model execution receipt, standby image digest, and monitor interval. On uncertain result it does not advance the runner. If the release script defers standby, the updater remains pending and retries certification without replacing live streams.
- A verified versioned host artifact is installed under `/root/aads/vendor/claude-cli/auto-update/versions/<version>/claude`; an atomic systemd drop-in switches the local runner's effective CLI path only after chat certification and an idle-safe runner check. The effective service path and version are re-read after restart. The original vendor binary is retained for rollback.
- Serial execution uses a dedicated flock and persistent state (candidate SHA, target version, phase). A second timer tick resumes verification of the same candidate, never creates a duplicate commit or release.

## Acceptance

| ID | Check |
|---|---|
| AU01 | A dry-run detects the old 2.1.259 effective CLI despite a 2.1.280 host global CLI. |
| AU02 | Official manifest checksum mismatch, unsupported version jump, or network failure causes no commit, push, or deployment. |
| AU03 | Candidate image includes the exact official binary; relay and SDK wrappers execute it. |
| AU04 | `claude-opus-5-5` produces a verified model receipt or a classified account/capacity result, not CLI-version HTTP 400. |
| AU05 | Busy chat slot or runner job causes deferral with no stream interruption. |
| AU06 | Both slots have the same digest and the local runner has the same effective version before completion. |
| AU07 | Timer, log and error-book record distinguish detected, staged, deployed, certified and blocked; a failed update retains the previous version. |

## Rollback and boundaries

The automatic lane is limited to patch releases within the currently pinned major.minor version. A major/minor transition or changed CLI protocol requires review. Do not update remote runners, Codex, OAuth credentials, GO100 trading policy, or model aliases as part of this workflow. The existing manual blue/green rollback contract remains the authority for an unhealthy cutover.
