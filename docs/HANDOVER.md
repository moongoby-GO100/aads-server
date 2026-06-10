# AADS HANDOVER
최종 업데이트: 2026-06-10

## 2026-06-10
- Chat completed-execution terminal-only repair: session `efccec7c-0788-4564-a2cf-265c63d075f0` showed a completed execution (`0a5a3a4a-2164-4b1b-89fa-8c9a22a1cb3a`) whose visible assistant row remained `interrupted_partial`, so the chat bubble changed from completed to interrupted. `app/services/chat_service.py` now repairs completed executions by keeping one final assistant row and archiving duplicate `streaming_placeholder/interrupted_partial/interruption_notice` siblings as `_archived_partial` instead of deleting them.
- Live DB backfill: 26 completed executions that had only terminal placeholder/interrupted rows and no final assistant row were repaired; 5 sibling terminal rows were archived; follow-up verification returned `remaining_completed_execs_with_terminal_only=0`. Target session `efccec7c...` now has `assistant_message_id=47338bf2-d6b1-42c9-b570-03a37fcd79ab`, `intent=NULL`, `model_used=gpt-5.5`.
- Verification: `PYTHONPATH=/root/aads/aads-server JWT_SECRET_KEY=test-secret-key pytest tests/unit/test_chat_service.py -q` passed with 46 tests, and `python3 -m compileall app/services/chat_service.py app/routers/chat.py` passed.
- Chat auto-default routing follow-up: `auto-default-llm` and legacy `qwen-turbo` are now treated as automatic DB-default sentinels in both `chat_service.send_message_stream()` and `model_selector.call_stream()`, including when the frontend sends `model_override=auto-default-llm`.
- Verification: active API SSE test session `5697dc0c-8389-4668-a8a2-b462ef69ab4c` completed with stream model and DB assistant `model_used=GPT-5.5 (Codex CLI)`. Targeted regression tests passed: `tests/unit/test_chat_service.py::test_send_message_stream_applies_db_default_over_auto_routed_models` and the two DB-default model selector tests.
- Chat large-session follow-up: dashboard dormant `useChatSSE` stream-resume URL now uses the canonical `/api/v1` base without duplicating `/api/v1`, shared artifact API helpers accept bounded `limit/offset`, and chat artifact filtering/count calculations are memoized to reduce repeated work in large sessions.
- Verification: `npx eslint src/app/chat/page.tsx src/hooks/useChatSSE.ts src/services/chatApi.ts --quiet` passed, and `npx tsc --noEmit --pretty false` passed. Full targeted lint including `src/lib/api.ts` still fails on pre-existing `no-explicit-any` debt in that file.
- Chat window precision audit follow-up: historical same-execution final/interrupted duplicate bubbles were hidden with `intent='_deleted_duplicate'` under the safe condition that a final assistant message already existed for the same `execution_id`; verification returned `duplicate_executions=0` and `orphan_untagged_7d=0`. Dashboard lint blockers in `ChatInput` and `SessionSummaryCard` were fixed by removing synchronous state-reset effects and replacing Web Speech `any` types with local interfaces. `supervisord.conf` now routes `aads-api` stdout/stderr to `/dev/fd/1` and `/dev/fd/2` so structured `stream_producer_exit` and completion guard logs are visible through Docker logs after the next deploy/restart.
- Chat window audit follow-up: completed-execution interrupted sibling bubbles are now archived automatically after completion, and a DB backfill archived 5 historical sibling interruption bubbles where a final assistant message already existed for the same execution. Four historical `execution_id IS NULL` interrupted bubbles were not hidden because no final execution linkage could be proven; they were tagged with `quality_details.interruption_reason='orphan_placeholder_no_execution'` for traceability.
- Verification: `JWT_SECRET_KEY=test-secret-key pytest tests/unit/test_chat_service.py -q` passed with 42 tests, and `python3 -m compileall app/services/chat_service.py` passed. Follow-up DB checks returned 0 same-execution final/interrupted duplicates after the backfill.
- Chat large-session freeze mitigation: `/api/v1/chat/artifacts` now supports bounded `limit/offset` and defaults to 60 artifacts, while the dashboard chat page fetches at most 61 records and renders the latest 60. This prevents sessions such as `266ab3aa-b0fd-46bb-8c54-01e4852c956f` from loading hundreds of artifact payloads on every session entry or SSE completion refresh.
- Verification: `JWT_SECRET_KEY=test-secret-key pytest tests/unit/test_tenant_rbac_policy.py tests/unit/test_chat_service.py -q`, `python3 -m compileall app/routers/chat.py app/services/chat_service.py`, and `npx eslint src/app/chat/page.tsx` passed for the touched paths. Full dashboard `npm run lint` still fails on pre-existing unrelated lint debt.
- Pipeline Runner AI review git diff guard was synced into `scripts/pipeline-runner.sh.local`, not only the primary `scripts/pipeline-runner.sh`. Local/recovery runner launches now share the same zero-diff block, git diff shape precheck, `INVALID_GIT_DIFF` flag, and retry recommendation path.
- Regression coverage added in `tests/unit/test_pipeline_runner_script_guards.py` to keep the primary runner and local template byte-for-byte synchronized and to require the diff precheck guard strings in both files.
- Chat completion false-interrupt guard tightened: `app/services/output_validator.py` and `app/services/response_completion_contract.py` now allow a structured final report to end with a `→ 다음 단계` recommendation without being reclassified as `PROGRESS_ONLY_RESPONSE` / `final_report_missing`.
- Verification target: `bash -n scripts/pipeline-runner.sh scripts/pipeline-runner.sh.local`, `pytest -q tests/unit/test_pipeline_runner_script_guards.py`, and `pytest -q tests/unit/test_response_completion_contract.py tests/unit/test_output_validator.py`.

## 2026-05-19
- Pipeline Runner API stale guard 보강: `app/api/pipeline_runner.py`가 AADS 로컬 runner PID만 `/proc`로 검증하고, KIS/GO100/SF/NTV2 원격 runner PID는 로컬 dead PID로 오탐하지 않도록 분리했다.
- AADS 로컬 `running/claimed` 작업의 runner PID가 죽었고 120초 이상 지난 경우, 새 작업 제출 전 `process_died` error로 정리해 dedup/lock이 막히는 재발을 줄인다.
- Contabo sync 개선: `scripts/sync-to-contabo.sh` lockfile PID 재사용 오탐을 줄이고, dashboard blue-green deploy는 `AADS_DASHBOARD_QA_STRICT=true`로 실행해 QA `UNKNOWN/ERROR`가 단순 성공 sync로 기록되지 않게 했다.
- 운영 cron 정리: root crontab의 존재하지 않는 `scripts/launch-sync.sh` 호출을 제거했고, `/etc/cron.d/contabo-sync`의 실제 `sync-to-contabo.sh` 5분 주기만 남겼다.
- 검증: `python3 -m py_compile app/api/pipeline_runner.py`, `bash -n scripts/sync-to-contabo.sh`, `pytest -q tests/unit/test_pipeline_runner_reliability.py` 통과.

## 2026-05-14
- Codex CLI quota 안내 중 `You've hit your limit · resets 3am (Asia/Seoul)` 고정시각 패턴이 `_parse_quota_reset_seconds()`에서 다음 03:00 KST까지의 초 단위로 파싱되는지 회귀 테스트를 추가했다.
- 검증: `pytest tests/unit/test_model_selector_dynamic_routing.py -q` 20개 통과. 운영 컨테이너 `aads-server`에서도 `resets 3am (Asia/Seoul)`이 다음 03:00 KST 기준 복구 시간으로 파싱됨을 확인했다.

## 2026-05-13
- 2026-05-13 KST: CEO PC 로컬 양자화/멀티모달 모델 브릿지 준비를 추가했다. `scripts/local_model_install_queue.json`을 canonical queue로 두고 `app/services/local_model_manager.py`가 queue/status/single-item install-test 라우팅을 담당한다.
- PC Agent에 `local_model_queue_status`, `local_model_install_test`, `local_model_media_job` 안전 핸들러를 추가하고 `pc_agent/VERSION`을 `1.0.26`으로 올렸다. `local_model_install`과 `local_media_job` lease 동시성은 1로 제한해 대형 모델 병렬 설치를 막는다.
- `app/api/local_models.py` REST API와 채팅 도구 `local_model_queue_status`, `local_model_install_test`, `generate_music`, `generate_three_d_asset`, `media_job_status`를 추가했다. `local_image`, `local_video`, `local_music`, `local_3d`는 async media job으로만 준비되며 기본 채팅 모델 라우팅은 변경하지 않았다.
- DB 준비: `093_pc_ollama_quantized_model_pack.sql`, `094_pc_ollama_backend_correction.sql`, `095_local_multimodal_model_bridge.sql`을 추가했다. `095`는 `media_generation_jobs.kind`에 `music`, `model_3d`를 허용하고 `pc_local` 모델 레지스트리를 queued/prepared 상태로 seed한다.
- 운영 주의: PC Agent가 offline이거나 새 capability를 아직 받지 못한 경우 설치 완료로 간주하지 않는다. 응답은 queued/prepared 또는 offline/not-updated 상태로만 보고해야 한다.
- 2026-05-13 14:11 KST: PC Agent `1.0.27` 준비. Ollama `/api/chat`의 `think` 값을 top-level로 전달하도록 보정하고, 로컬 모델 smoke test가 `content`가 비어 있을 때 `thinking/reasoning`을 fallback으로 기록하도록 수정했다. `self_update` 명령은 성공했지만 이후 WebSocket 재연결이 아직 확인되지 않아 모델 설치/테스트 재개는 PC Agent 재실행 후 진행해야 한다.

## 2026-05-12
- 2026-05-12 15:20 KST: `app/services/chat_service.py`에 응답 누락 지적 속 인용 지시 승격 로직을 추가했다. `"마지막 대화버블에 '...조치하고 보고해'가 남아 있는데 왜 응답을 못하나"` 유형은 인용된 실제 CEO 지시를 이번 턴 실행 대상으로 시스템 프롬프트에 다시 붙인다.
- 검증: `python3 -m py_compile app/services/chat_service.py`, `.venv/bin/python -m py_compile app/services/chat_service.py`, `.venv/bin/python` 직접 헬퍼 호출 검증 통과. 현재 `.venv`에는 `pytest`가 없어 추가 단위 테스트 실행은 미수행.
- Browser Bridge 업무 키 전용 세션 매니저 적용: `ntv2-sinsang-registration`, `ntv2-china-sourcing-admin`, `ntv2-vvic-scrape` 같은 업무 키로 세션을 확보한다.
- `ntv2-sinsang-registration`은 보호 세션이다. 중국상품소싱 관리자 검수나 VVIC 수집 자동화는 이 세션을 공유하지 말고 `browser_work_key`를 지정해 별도 세션을 사용한다.
- 사용 예시: `browser_connect(action="ensure_work_session", work_key="ntv2-china-sourcing-admin")`, 이후 `browser_navigate(url="...", browser_work_key="ntv2-china-sourcing-admin")`.
- 상태 확인: `browser_connect(action="status")` 또는 `GET /api/v1/browser-bridge/work-sessions`에서 label, storage, leased, last_used_at, work_key/protected 매핑을 확인한다.
- AADS/vault 자동 로그인은 분리 세션(`browser_work_key` 또는 `browser_session_id` 명시)에서만 수행한다.
- 2026-05-12 13:43 KST: `app/services/model_selector.py`에서 Codex relay와 Claude CLI relay 재시도 정책을 기본 `2초 간격 x 30회`로 통일했다. 환경변수 `AADS_RELAY_RETRY_INTERVAL_SECONDS`, `AADS_RELAY_RETRY_MAX_RETRIES`로 조정 가능하다.
- Claude CLI relay는 partial 응답이 있으면 재시도 요청에 직전 assistant 초안을 붙여 같은 모델이 이어 쓰도록 보강했다. 명시적 quota/결제/인증/`You've hit your limit ... resets` 계열은 재시도하지 않는다.
- 검증: `python3 -m py_compile app/services/model_selector.py` 통과. `pytest -q tests/unit/test_model_selector_dynamic_routing.py::test_stream_cli_relay_retries_same_model_before_returning_done tests/unit/test_model_selector_dynamic_routing.py::test_stream_codex_relay_retries_same_model_before_returning_done tests/unit/test_model_selector_dynamic_routing.py::test_relay_retry_policy_defaults_to_two_seconds_thirty_retries` 3개 통과.

## 2026-05-11
- Pipeline Runner dashboard-target guard applied in `scripts/pipeline-runner.sh`: AADS jobs that reference `/root/aads/aads-dashboard`, `aads-dashboard`, or chat/dashboard frontend paths now run against `/root/aads/aads-dashboard` instead of the backend workdir.
- Runner now blocks `awaiting_approval` when the actual target repo has zero git diff, preventing read-only/tmp-copy failures from being reported as successful work.
- Approval/reject/deploy cleanup now resolves the same target workdir from the stored job instruction, so dashboard worktrees are committed, pushed, reverted, or removed in the dashboard repo rather than `aads-server`.
- AADS dashboard-target deploy skips backend reload/bluegreen and runs the dashboard deploy path; dashboard rollback also redeploys the dashboard path instead of the backend path.

## 2026-05-09
- Project Change Promoter implemented: completed runner jobs and raw `memory_facts` change events can now be promoted into `architecture_decision`, `feature_change`, `api_contract`, and `data_model_change` facts.
- `workspace_preloader` now prioritizes those strategic change categories in a `최근 중요 변경 자동 인지` block so future sessions can recognize important architecture/function/API/DB changes automatically.
- Canary applied on live DB: 4 CEO workspace changes were promoted with embeddings; duplicate prevention uses `project_change_promoter` source tags.
- Operational note: scheduler registration is coded as `project_change_promoter` every 30 minutes, but the running API process must be restarted/deployed before that scheduler is active.

## 2026-05-06
- Pipeline Runner audit/remediation applied: `db_exec()` now suppresses empty `UPDATE ... RETURNING` command tags, preventing `UPDATE 0` from being misread as job ids and producing repeated `invalid session_id` logs.
- Failure semantics hardened: runtime/watchdog/token/restart/shutdown/deploy-lock/rollback failures now use `status='error'` with explicit `phase/error_detail` instead of `cancelled/superseded`.
- Actual model trace hardened: runner now records Codex effective model after CLI normalization/fallback; API job detail now exposes `model`, `worker_model`, `actual_model`, `size`.
- Chat-direct AI review diff capture hardened: `tool_executor` now quotes git paths, strips `run_remote_command` wrappers, and falls back to local `git -C ... diff` for AADS when SSH/host bridge git diff capture fails.
- DB queue cleanup performed: 6 stale queued jobs, 1 stale rolling_back job, 1 invalid `failed` row, and 6 mismatched `rejected_done` phases normalized.
- Server sync: 68 and 114 runner scripts updated and restarted. 211 script updated on disk only; do not restart until active GO100 job `runner-d59beba6` finishes.
- Technical record added: `docs/reports/20260506_RUNNER_AUDIT_REMEDIATION.md`.

## 2026-05-04
- Android Agent Galaxy Z Fold6 remote-control follow-up: `runner-4f922625` code implementation is committed in `aads-server` as `05c7dc7`; follow-up sensor JSON hardening skips non-finite values during `JSONArray` serialization.
- Chat visibility fix: `runner_response` assistant messages are no longer hidden by backend/dashboard message filters, so saved AI review/status reports remain visible in the main chat timeline.
- Pipeline Runner docs/config: AADS per-project concurrency is documented/configured as `MAX_CONCURRENT_PER_PROJECT=6`; global limit remains 10.
- Technical record added: `docs/reports/20260504_ANDROID_AGENT_CHAT_VISIBILITY_TECHNICAL.md`.
- Caution: `runner-4f922625` has finalize/deploy timeout history. Future deploy-complete reports require fresh health/API/APK download verification.

## 2026-04-24
- Phase 1-C: claude_md_merger ready, /api/v1/ops/claude-md endpoint live
