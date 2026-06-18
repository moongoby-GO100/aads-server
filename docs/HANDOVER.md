# AADS HANDOVER
최종 업데이트: 2026-06-18

## 신규 러너 시작점
- 1차: `docs/knowledge/AADS-3STEP-SYSTEM-INDEX.md`
- 2차: `docs/knowledge/CTO-SYSTEM-MAP.md`
- 3차: 루트 `HANDOVER.md`
- 작업 전에는 인덱스의 금지사항과 읽기 순서를 먼저 확인한다.

## 2026-06-18
- AADS Voice Command MVP 기획 보고를 문서화했다. 현재 본체에는 `audio` 모델 라우팅 후보만 있고 실제 STT/TTS API, 대시보드 마이크 UI, 답변 TTS 재생 UI는 미구현 상태다.
- 신규 문서: `docs/plans/AADS-VOICE-COMMAND-MVP.md`.
- `runner-9dae6d37` 상태를 확인했다. 결과는 `error`이며 root 환경에서 `--dangerously-skip-permissions` 사용 차단으로 Claude Code 실행이 실패했다. `git_diff`는 비어 있어 코드 산출물은 없다.
- 권장 구현 흐름: `/api/v1/voice/transcribe`, `/api/v1/voice/speech`, `/api/v1/voice/health` 추가, 기존 JWT/tenant 인증 재사용, 음성 STT 결과는 기존 chat send 경로로 넘겨 도구 실행 정책을 유지한다.
- 구현은 아직 수행하지 않았다. 재투입 시 특정 Claude worker model 강제보다 Runner 기본 모델 설정 사용 또는 root 권한 플래그 문제 선조치가 필요하다.
- 검증: `date`, `git status --short`, `rg` 기반 구현 흔적 확인, `pipeline_runner_status(job_id=runner-9dae6d37, scope=all)` 확인.

## 2026-06-15
- AADS docs/static contract closeout: 법인 설립 서류 7종, 열정국밥 중화점 법인 부동산임대차계약서, 임대인동의서, 영업양수도계약서를 `app/static/docs/contracts/`에 공개 산출물로 정리했다.
- Generation scripts added/kept for reproducibility: `scripts/gen_incorporation_all.py`, `scripts/generate_yeoljeong_landlord_and_corp_lease.py`, `scripts/fix_duplicate_accumulate.py`.
- `app/services/self_evaluator.py` 중복 `_accumulate_experience()` 정의와 중복 호출 블록을 제거해 Experience Memory 축적 로직이 1회만 실행되도록 정리했다.
- Verification at 2026-06-15 13:36 KST: `python3 -m py_compile app/services/self_evaluator.py scripts/gen_incorporation_all.py scripts/fix_duplicate_accumulate.py scripts/generate_yeoljeong_landlord_and_corp_lease.py` passed. `curl -fsS http://127.0.0.1:8100/health` returned `status=ok`. Static DOCX URL for `주식회사 윤희에프엔비_정관.docx` returned HTTP 200 with DOCX content type.
- Public URL follow-up: Cloudflare returned 403 for percent-encoded Korean DOCX paths, while local static serving returned 200. ASCII aliases were added for all 10 contract files, including `yhfnb_articles.docx`, `yhfnb_incorporation_minutes.docx`, `yeoljeong_corp_lease_junghwa.docx`, `yeoljeong_landlord_consent_junghwa.docx`, and `yeoljeong_transfer_junghwa.docx`; public `https://aads.newtalk.kr/static/docs/contracts/yhfnb_articles.docx` returned HTTP 200.
- Commit scope note: `.active_container` and `.active_port` are runtime state files and must remain unstaged unless explicitly changing blue/green active slot metadata.

## 2026-06-12
- Codex usage bar recovery: `/api/v1/ops/codex-usage` now returns a stable `codex_cli` fallback limit when the Codex app-server relay responds with `ok=false` or `limits=[]`.
- Root cause: the chat `UsageBar` renders Codex only when `codex.ok` and `limits[0]` exist; the live relay currently returns HTTP 200 with an empty limits array, so the GPT/Codex bar disappeared while Claude still rendered.
- Fallback source: `oauth_usage_log` Codex/GPT-5.x rows for 5h/7d windows. When no Codex rows exist, it returns 0% usage with `fallback_reason=relay_empty_limits` so the UI remains visible and debuggable.
- Verification: `python3 -m py_compile app/api/ops.py` passed before commit/deploy.
- SaaS admin/customer isolation audit: operating DB has 1 active internal tenant, 34 active customer tenants, 33 active customer users, and 0 active non-deleted users without `default_tenant_id`. Core chat tables (`chat_workspaces`, `chat_sessions`, `chat_messages`, `chat_artifacts`, `e2e_credentials`) are tenant-scoped.
- Admin API lockdown: `/api/v1/admin/users/overview` and `/api/v1/admin/design/projects/AADS/screens` were verified as 403 for a customer token and 200 for an internal admin token after commit `e986cde`.
- Additional P0 exposure fixed: `/api/v1/llm-keys`, `/api/v1/image/*`, and `/api/v1/chat/drive*` could expose shared/internal resources to customer tokens. `app/api/llm_keys.py` and `app/api/image.py` now require `require_internal_admin`; chat drive routes now require tenant membership and verify the target workspace tenant before list/upload/download/delete.
- Verification: `python3 -m py_compile app/api/llm_keys.py app/api/image.py app/routers/chat.py app/services/chat_service.py` and `git diff --check` passed before deployment. Post-deploy customer-token HTTP checks must confirm `llm-keys=403`, `image/gallery=403`, internal workspace `chat/drive=404/403`, and own customer workspace `chat/drive=200`.
- Remaining SaaS hardening: add direct `tenant_id` or enforced join constraints for legacy link tables (`chat_files`, `chat_drive_files`, `chat_todo_items`, `compiled_prompt_provenance`, `media_generation_jobs`), resolve 19 orphan `compiled_prompt_provenance` rows, and split media jobs into tenant-scoped customer media vs internal gallery.

## 2026-06-11
- SaaS customer chat isolation closeout: active customer tenants missing chat workspaces were backfilled with default `[WORK] {tenant}` workspaces (`project_key=CUSTOMER`, `allowed_roles=["GeneralAssistant"]`, `role_routing_enabled=false`). Customer tenant login/onboarding/invite acceptance now ensures a default workspace exists.
- Customer users are forced to `GeneralAssistant` in customer workspaces, including when the client sends an internal role key, and internal project mentions are ignored for customer tenants so AADS/KIS/GO100/SF/NTV2/NAS context does not bleed into customer chats.
- Blueshop verification: `objgood@naver.com` in tenant `블루샵` now sees one workspace, one available role (`GeneralAssistant`), can create a session, and an API smoke message completed with SSE `done=true`; the user and assistant messages were stored only under the Blueshop tenant.
- Sleep-Time Agent auth routing fix: `app/core/memory_gc.py` no longer calls `get_client().messages.create()` directly for project insights or quality optimization. It now uses `call_llm_with_fallback()` so invalid Anthropic credentials fall through the central R-AUTH chain instead of repeatedly logging `invalid x-api-key`.
- Yeoljeong transfer contract refresh: `scripts/generate_yeoljeong_transfer_contract.py` now explicitly states `사업자등록 완료 전 폐업신고 금지` in Article 5 and the cooperation table. Regenerated `영업양수도계약서_열정국밥_중화점.docx` in both `exports/contracts/` and `app/static/docs/contracts/`.
- Download fix: regenerated DOCX initially inherited SELinux `admin_home_t` and returned 403; static copy was corrected to `httpd_sys_content_t`. Verified URL `https://aads.newtalk.kr/static/docs/contracts/영업양수도계약서_열정국밥_중화점.docx?v=20260611-active-coop4` returns HTTP 200, size 45,208 bytes, SHA256 `fef45709bcd56cc2e717764ac1e7980b3515ec60171d9566bda7727962dd9ed4`.
- AI review git diff classification DB closeout: Pipeline Runner scripts already had synchronized `pre_exec_sha` committed/uncommitted diff capture, zero-diff approval blocking, and `INVALID_GIT_DIFF` guards; Chat-Direct review capture already had AADS local `git diff` fallback in `app/services/tool_executor.py`.
- Live DB gap fixed: `migrations/041_code_review_flag_classification.sql` was applied to add `code_reviews.flag_category`, `failure_stage`, and `needs_retry`; `checkpoint_migrations(v=41)` is now recorded.
- Verification: `POST /api/v1/review/code-diff` with `fatal: not a git repository` returned `FLAG`, `GIT_DIFF_FAILURE`, `git_diff_capture`, and `needs_retry=true`. `bash -n scripts/pipeline-runner.sh scripts/pipeline-runner.sh.local`, `pytest -q tests/unit/test_pipeline_runner_script_guards.py tests/unit/test_code_reviewer_flag_classification.py`, and `python3 -m py_compile app/services/tool_executor.py app/services/code_reviewer.py app/api/ceo_chat_tools.py` passed.
- No code deploy/restart was needed for this closeout; only the live DB migration and HANDOVER docs changed. Existing unrelated dirty files were left untouched.
- Blue-green deploy reporting correction: `deploy.sh` now removes the duplicated pre-switch active-stream drain wait and reports `active 전환 완료` separately from old-slot standby sync. Standby rebuild remains a background drain-following task and emits its own complete/skip log, so final reports no longer imply standby sync is finished when it is only scheduled.
- Verification: `bash -n deploy.sh` passed. Runtime check at 2026-06-11 10:37 KST showed `.active_port=8102`, `.active_container=aads-server-green`, both `8100` and `8102` health endpoints OK, and both API containers healthy. Existing unrelated dirty/static files were left untouched.

- NTV2/V1 신뉴톡 `pick.newtalk.kr/root/members` AADS AI chat 운영 검수: 실제 운영 경로 `/home/newpigup3` 기준으로 관리자 공통 head include, `v1_new` service routing, `/aads-chat/config`, `/aads-chat/session`, `/aads-chat/sessions/{id}/messages` 흐름을 검증했다.
- V1 신뉴톡 프록시 `application/controllers/Aads_chat.php`의 message payload가 `response_mode='fast'`로 AADS 자동 라우팅을 우회하던 문제를 `response_mode='auto'`로 수정했다. 2026-06-11 12:08 KST 관리자 세션 E2E에서 `members_status=200`, `data-service="v1_new"`, `config_status=200`, `session_status=201`, message `status=200`, stream `model=GPT-5.5 (Codex CLI)`, `errors=[]`를 확인했다.
- 검증 보안: 비로그인 `/aads-chat/config?service=v1_new` 호출은 `Unauthenticated`로 차단됐고, `auth_code=80` 테스트 세션의 `/aads-chat/config?service=v1_new`는 `403 Forbidden`으로 차단됐다. 임시 `PHPSESSIDaads*` 테스트 세션은 검증 후 삭제했고 잔여 0건을 확인했다.
- 운영 주의: `/home/newpigup3` 레거시 저장소는 기존 대량 dirty 상태이며, `routes.php`/`head.php` diff에는 이번 AADS chat 외 기존 입점상담/푸시/카카오 가드 변경이 섞여 있어 선별 커밋은 보류했다. `application/config/aads_chat.php`는 토큰 가능성이 있어 커밋 대상에서 제외해야 한다.

## 2026-06-12
- External Chat Gateway 범용화: 뉴톡 전용 `Literal["newtalk"]` / `v1_old|v1_new|v2` 제한을 제거하고, `AADS_EXTERNAL_CHAT_ALLOWED_SERVICES` CSV 또는 `AADS_EXTERNAL_CHAT_SERVICE_REGISTRY` JSON으로 외부 서비스별 `provider:service`를 등록할 수 있게 했다. 미등록 서비스는 `external_chat_service_not_allowed`로 403 차단된다.
- 등록 서비스별 워크스페이스명, 시스템 프롬프트, 세션 제목 prefix, 색상, 아이콘, `admin_only` override를 지원한다. 기본 뉴톡 3개 서비스는 기존 호환성을 유지한다.
- API 보강: `/api/v1/external/chat/services`가 토큰/HMAC 인증 후 등록된 외부 서비스 목록을 반환하고, `/config`, `/sessions`, `/messages`는 범용 provider/service 문자열을 받되 안전한 key 패턴과 등록 검증을 통과해야 한다.
- 검증: `python3 -m py_compile app/services/external_chat_gateway.py app/api/external_chat.py`, `python3 -m pytest tests/unit/test_external_chat_gateway.py -q` 12개 통과. 운영 컨테이너 `aads-server-green`에서 `sf:ops` 등록 config import 검증 통과. 컨테이너 내부 pytest는 tests 디렉터리 미마운트 상태라 기존 컨테이너 내 8개 테스트만 실행됨.

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
