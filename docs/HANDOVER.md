# AADS HANDOVER
최종 업데이트: 2026-05-13

## 2026-05-13
- 2026-05-13 KST: CEO PC 로컬 양자화/멀티모달 모델 브릿지 준비를 추가했다. `scripts/local_model_install_queue.json`을 canonical queue로 두고 `app/services/local_model_manager.py`가 queue/status/single-item install-test 라우팅을 담당한다.
- PC Agent에 `local_model_queue_status`, `local_model_install_test`, `local_model_media_job` 안전 핸들러를 추가하고 `pc_agent/VERSION`을 `1.0.26`으로 올렸다. `local_model_install`과 `local_media_job` lease 동시성은 1로 제한해 대형 모델 병렬 설치를 막는다.
- `app/api/local_models.py` REST API와 채팅 도구 `local_model_queue_status`, `local_model_install_test`, `generate_music`, `generate_3d_asset`, `media_job_status`를 추가했다. `local_image`, `local_video`, `local_music`, `local_3d`는 async media job으로만 준비되며 기본 채팅 모델 라우팅은 변경하지 않았다.
- DB 준비: `093_pc_ollama_quantized_model_pack.sql`, `094_pc_ollama_backend_correction.sql`, `095_local_multimodal_model_bridge.sql`을 추가했다. `095`는 `media_generation_jobs.kind`에 `music`, `model_3d`를 허용하고 `pc_local` 모델 레지스트리를 queued/prepared 상태로 seed한다.
- 운영 주의: PC Agent가 offline이거나 새 capability를 아직 받지 못한 경우 설치 완료로 간주하지 않는다. 응답은 queued/prepared 또는 offline/not-updated 상태로만 보고해야 한다.

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
