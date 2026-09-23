# Agent SDK PreToolUse hook wire-contract mismatch — 2026-09-23

## Symptom and scope

The direct-execution Agent SDK service registers `pre_tool_use_hook` to deny
dangerous Bash and sensitive-file writes. The hook returned
`{"behavior":"deny","message":"..."}`. That shape belongs to a `can_use_tool`
permission callback, not to the installed SDK's `PreToolUse` hook output.
The CLI may ignore its unknown fields; thus the safety claim based on this
hook was not supported by the return contract. The issue predates the
Claude CLI version fix; this review exposed it before declaring that path
verified. No particular unsafe tool execution was observed or inferred.

## Root cause and correction

The code conflated `PermissionResultDeny` with `HookJSONOutput`. The installed
SDK's `SyncHookJSONOutput` defines
`hookSpecificOutput={hookEventName:"PreToolUse", permissionDecision:"deny",
permissionDecisionReason:"..."}` for this event. Return that shape for all
deny branches, and the corresponding `allow` shape for safe tools. Regression
tests cover dangerous Bash, SQL, local/remote sensitive writes, remote
shutdown, and a safe read.

## Verification and limits

- Installed container SDK type definitions and official Anthropic SDK source
  both confirm the hook-specific contract.
- New hook contract plus CLI model contract suite: 81 passed.
- Legacy `tests/test_agent_sdk.py` still has seven pre-existing failures due
  obsolete `block` assertions, removed diff-preview behavior and an old
  routing marker; these tests do not establish live hook effectiveness.
- A true tool-denial integration canary and five-minute production monitoring
  remain required before calling the direct-execution SDK path certified.
