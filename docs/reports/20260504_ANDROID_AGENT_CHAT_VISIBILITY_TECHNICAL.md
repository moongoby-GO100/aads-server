# 2026-05-04 Android Agent + Chat Visibility Technical Record

## Summary

This record closes the 2026-05-03 Android remote-control implementation review and the follow-up chat visibility defect where saved AI review/status responses were present in DB but hidden from the chat timeline.

## Scope

| Area | Files | Result |
|------|-------|--------|
| Android agent command surface | `android_agent/app/src/main/java/kr/newtalk/aads/agent/CommandDispatcher.java`, `AndroidCommandHandlers.java`, service/manifest files from `05c7dc7` | Galaxy Z Fold6 remote-control handlers are registered and implemented. |
| Android sensor JSON hardening | `android_agent/app/src/main/java/kr/newtalk/aads/agent/AndroidCommandHandlers.java` | Non-finite sensor values are skipped when building JSON arrays so `NaN`/`Infinity` cannot break command responses. |
| Chat assistant response visibility | `app/services/chat_service.py`, `app/routers/chat.py`, `aads-dashboard/src/app/chat/page.tsx` | `runner_response` is no longer globally filtered out of user-visible assistant messages. System triggers and auto-reaction logs remain hidden. |
| Pipeline Runner concurrency documentation | `scripts/pipeline-runner.sh`, `scripts/aads-pipeline-runner.service`, `docs/pipeline-runner/*`, `docs/knowledge/CTO-SYSTEM-MAP.md` | AADS per-project concurrency is documented and configured as 6, with global concurrency 10. |

## Android Agent Implementation

The main implementation was committed as `05c7dc7` in `aads-server`. The dispatcher now registers 57 command aliases, covering:

- Contacts and phone data: `contacts_list`, `contacts_search`, `call_log`, `sms_inbox`
- Media and app inventory: `photo_gallery`, `app_list`, `app_launch`
- Device connectivity/status: `bluetooth_status`, `sensor_data`, `notification_read`
- Accessibility UI control: `screen_tap`, `screen_swipe`, `screen_long_press`, `screen_text`, `screen_scroll`, `find_and_click`, `key_input`, `global_action`
- Device administration and display: `device_lock`, `device_wipe`, `device_admin_status`, `screen_brightness`, `screen_timeout`
- Media capture: `screenshot`, `audio_record`

The follow-up hardening in this commit changes sensor serialization to skip `NaN` or infinite float values. This prevents `org.json.JSONArray.put(float)` from failing the whole command result when Android sensor APIs emit non-finite values.

## Chat Visibility Fix

Observed issue:

- Recent AI review/status responses were saved to `chat_messages` with `intent='runner_response'`.
- Backend message listing and last-response queries filtered `runner_response` alongside system automation messages.
- Dashboard system-message classification also treated `runner_response` as log-only.
- Result: DB contained the response, but the main chat window could omit it.

Applied behavior:

- `pipeline_c`, `ai_review_warning`, `system_trigger`, `auto_reaction`, and content-pattern Pipeline Runner logs remain excluded from the main chat.
- `runner_response` assistant messages are retained in the normal assistant timeline so CEO-visible status/review reports do not disappear after session reload.

## Operational Notes

- The Pipeline Runner jobs `runner-36c5b7bc` and `runner-4f922625` produced repeated AI review warnings because the review input pipeline sent non-`diff --git` content in some review paths. That issue affects review trust but is separate from the Android command implementation.
- `runner-4f922625` was approved for code quality, then later timed out in finalize/deploy. The code changes were committed, but Runner job status should not be reported as clean deploy success unless verified by a later health/API/APK download check.
- APK and deployment state must be measured with filesystem/container/API checks before any future deployment-complete report.

## Verification Commands

Recommended verification for future handoff:

```bash
python3 -m py_compile app/routers/chat.py app/services/chat_service.py
cd android_agent && ./gradlew assembleDebug
cd /root/aads/aads-dashboard && npx eslint src/app/chat/page.tsx
```

Deployment-complete verification, if deploy is requested:

```bash
docker ps --filter name=aads-server --format '{{.Names}} {{.Status}}'
curl -fsS https://aads.newtalk.kr/api/v1/health
curl -fsS -I https://aads.newtalk.kr/api/v1/android-agent/apk
```
