# Chat interruption: process-wide Claude reaper safety fix

Date: 2026-09-08 KST

## Incident

The API log recorded `orphan_claude_reaper` sending SIGTERM to three Claude CLI
processes while chat executions were still running. The affected model calls
then exited with status 143 and entered the retry/interruption path.

## Root cause

The reaper compared process count only with `agent_sdk_service._active_iterators`.
Chat calls launched directly by `model_selector._run_agent_sdk_with_key` are not
registered in that map. When three such calls were active, the reaper observed
zero iterators and treated every matching Claude CLI process as an orphan.

## Fix

- The process-wide reaper is disabled by default and requires the explicit
  `AADS_ORPHAN_CLAUDE_REAPER_ENABLED=true` operator opt-in.
- The cleanup function itself also fails closed when the opt-in is absent.
- If enabled, the periodic sweep skips cleanup while the current API instance
  owns any live DB-fenced chat execution or an in-memory SDK iterator.
- Stream-local cleanup in `AgentSDKService.execute_stream(...).finally` remains
  enabled and continues to terminate only processes created by that stream.

## Verification

- Unit tests cover default-off, accepted opt-in values, and absence of SIGTERM
  when the feature is disabled.
- Existing chat ownership/resume regression tests remain required before release.
- Production certification requires Blue/Green same-digest slots, routed health,
  and five minutes with no new P0/P1 interruption or reaper-kill log events.

## Rollback

Revert the release commit and redeploy the previous immutable API image. Do not
enable the process-wide reaper as a rollback shortcut.
