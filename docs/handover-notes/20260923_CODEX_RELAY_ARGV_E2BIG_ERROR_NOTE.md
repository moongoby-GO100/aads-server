# Codex relay long-prompt E2BIG and broken chunked response — 2026-09-23

## Symptom and impact

Release 5172 failed its five-minute P0/P1 monitor after two
`codex_relay_error: illegal chunk header` events. At 14:51:29 and 14:51:39
KST, the host `claude-relay.service` returned HTTP 500 for `/codex-stream`.
The affected chat attempted GPT-6 Astra and then GPT-5.6 Sol; both failed
before Codex started, then the route fell back to Claude. The prompt length
logged by the relay was 77,305 characters. This was neither a Codex model
availability failure nor a Claude CLI version failure.

## Root cause

The relay appended the entire prompt as a single `codex exec` argv element.
`asyncio.create_subprocess_exec` failed with OS error 7 (`E2BIG`, argument
list too long) while spawning `/usr/bin/timeout`. The relay had already
prepared an HTTP 200 chunked stream; its outer exception handler then tried
to return a new JSON HTTP 500, corrupting the chunk framing seen by `httpx`.

## Correction and prevention

Pass `-` as the Codex prompt argument and write the UTF-8 prompt to the
subprocess stdin pipe. This is the documented `codex exec` input mode:
[OpenAI Codex developer commands](https://developers.openai.com/codex/cli/reference).
If an exception occurs after the stream is prepared, send a typed NDJSON
error and close that stream instead of constructing a second HTTP response.
Regression tests use a 50,000-character Korean prompt and a simulated E2BIG
spawn failure; the relay suite passed 43 tests.

## Acceptance

Verify the running host relay uses the new code, then replay a long-prompt
canary without private conversation content, confirm no E2BIG/HTTP 500 or
`illegal chunk header`, and require a clean five-minute P0/P1 release monitor
with both chat slots on the same digest.
