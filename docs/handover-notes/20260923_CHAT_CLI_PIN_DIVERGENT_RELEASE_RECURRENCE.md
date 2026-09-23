# Chat CLI pin regression from divergent release — 2026-09-23

## Observed

Chat `1fa84036-b12d-4497-97f5-076a32645a20` received HTTP 400 at
10:17 and 10:25 CEST: Claude Code 2.1.259 did not support the selected model,
which required 2.1.280 or newer. The host relay PID was 971577, started at
10:03 CEST; both blue/green API slots used image `aads-server:9106d5c8a4cc`.

## Verified cause

The release commit `9106d5c8` was not a descendant of compatible-CLI commit
`57bfeccf` (or release `c6f337e6`). Its Dockerfile had no checksum-pinned
`/usr/local/bin/claude-aads`, and its OAuth wrapper executed
`claude_agent_sdk/_bundled/claude` directly. The bundled binary reported
`2.1.259` in both running containers. Host-global and runner CLI versions
were `2.1.280`; those were not the chat execution path. This was not an
OAuth credential failure.

## Correction and prevention

Merge the divergent operating branch with canonical `origin/main` so the
image pin, authenticated wrapper, direct Agent SDK CLI path, and previously
released fixes coexist. Build from the clean merge commit. Before cutover,
`deploy.sh` must execute the candidate's pinned CLI and reject a missing or
pre-2.1.280 binary. Preserve the scoped fast-forward preflight for queued
API releases. Verify both slot digests, the exact chat-path CLI versions, a
read-only model canary, and five minutes without new P0/P1 errors.
