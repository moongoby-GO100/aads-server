# Host Claude CLI vs container Agent SDK — AADS risk audit (2026-09-23)

## Finding

Do not switch production chat to the host CLI as an incidental fix for version
drift. The Agent SDK can run a custom CLI using `ClaudeAgentOptions(cli_path=...)`,
but the chosen path must exist in the SDK process's filesystem. The official
Python SDK bundles a CLI by default. AADS currently has multiple execution
paths, not one globally updated CLI.

## Effective AADS paths

| Path | Current implementation | Effect of host `/usr/bin/claude` update |
|---|---|---|
| Main chat relay | Host systemd relay launches `claude-docker-wrapper-active.sh`, which executes `/usr/local/bin/claude-aads` in the active container | None |
| Chat model-selector SDK fallback | `ClaudeAgentOptions(cli_path="/app/scripts/claude-oauth-wrapper.sh")`, ending at container `/usr/local/bin/claude-aads` | None |
| Direct-execution AgentSDKService | `ClaudeAgentOptions` has no `cli_path`, so the SDK chooses its bundled CLI by default | None |
| Local pipeline runner | Host systemd uses `RUNNER_CLAUDE_CLI_BIN`, currently the vendor binary, not `/usr/bin/claude` | None |

## Migration options and risks

1. **Read-only host binary mount into both chat slots, retaining SDK and relay.**
   This changes only executable sourcing, not the SDK architecture. It requires
   architecture/runtime compatibility, a stable versioned path, a precise
   read-only mount, explicit `cli_path`/wrapper routing, digest/version
   attestation, blue/green recreation, and rollback. A mutable mount bypasses
   the image checksum and can make the two slots run code that their image
   digests do not identify. It also couples chat availability to the host
   updater and removes the current image-level rollback guarantee. A single
   host file bind does not itself prove that an atomic host replacement becomes
   visible inside already-running containers; test that lifecycle before use.
2. **Run the relay's Claude CLI directly on the host.** This moves CLI process,
   cwd, settings and MCP execution outside the container's process/resource
   boundary. The relay service runs as root. The existing host credential
   wrapper defaults to a 6000-second refresh-lock window and unbounded `flock`,
   whereas the Docker wrapper uses a 600-second window and bounded waits. The
   host wrapper does not link the persistent `/tmp/.claude-sdk/.claude/projects`
   transcript store used by the Docker path. Without parity work, the switch
   risks slot-wide waits, failed `--resume`, different MCP Python dependencies
   or paths, and a larger filesystem blast radius. These are code-observed
   risks, not evidence of a failure in a tested host canary.
3. **Keep the pinned container CLI and gated updater (current approach).** This
   preserves checksum-pinned release provenance, the existing slot-auth and
   transcript behavior, and blue/green rollback. It requires explicit
   monitoring and a separate idle-safe runner update. The direct-execution
   `AgentSDKService` still needs an explicit pinned `cli_path` or a separately
   tested SDK update; otherwise it can remain on the bundled version.

## Recommendation and acceptance for any future switch

Keep option 3 now. If host sharing is required, canary option 1 before option 2:
verify both slot versions and exact-model receipts, slot refresh without token
collision, session resume across cutover, MCP tool receipts, 24-hour auth and
error rates, resource limits, and one-command rollback to the immutable image.
Do not change `CLAUDE_BIN`, service unit, mounts, or SDK options in production
solely to make versions appear equal.

## Sources

- [Anthropic Python Agent SDK README](https://github.com/anthropics/claude-agent-sdk-python): bundled CLI default and custom `cli_path`.
- [Anthropic Agent SDK hosting guide](https://code.claude.com/docs/en/agent-sdk/hosting): subprocess/container isolation, runtime dependencies, session persistence and multi-tenant settings.
- [Anthropic Python SDK reference](https://code.claude.com/docs/en/agent-sdk/python): `ClaudeAgentOptions.cli_path` and in-process tools/hooks.
- Local files: `scripts/claude_relay_server.py`, `scripts/claude-docker-wrapper.sh`, `scripts/claude-slot-credentials-wrapper.sh`, `app/services/model_selector.py`, `app/services/agent_sdk_service.py`, `docker-compose.prod.yml`.
