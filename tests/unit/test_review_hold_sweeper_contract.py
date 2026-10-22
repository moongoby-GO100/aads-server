from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_review_hold_sweeper_stops_batch_without_spending_retry_budget_on_outage():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    circuit_gate = script.index('if [[ "$http_code" != "202" || -z "$verdict" ]]')
    retry_increment = script.index("next_retry=$((retry_count + 1))")

    assert circuit_gate < retry_increment
    assert "retry budget preserved; batch stopped" in script
    assert "REVIEW_MODEL_NO_RESPONSE" in script[circuit_gate:retry_increment]
    assert "break" in script[circuit_gate:retry_increment]


def test_review_hold_sweeper_persists_then_polls_async_request():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    existing_lookup = script.index('existing_http_code=$(curl')
    resume = script.index('/api/v1/review/code-diff/requests/${request_id}/resume')
    enqueue = script.index('/api/v1/review/code-diff/requests"')
    poll = script.index('poll_code=$(curl', enqueue)
    verdict = script.index("verdict=$(jq -r '.verdict // empty'")

    assert "review_request_id UUID" in script
    assert existing_lookup < resume < enqueue < poll < verdict
    assert 'elif [[ "$existing_http_code" == "404" ]]; then' in script
    assert 'if [[ -z "$request_id" ]]; then' in script
    assert "request_status" in script[poll:verdict]
    assert "REVIEW_MONITOR_HEADER='X-Monitor-Key: internal-review-hold-sweeper'" in script
    assert script[enqueue:verdict].count('-H "$REVIEW_MONITOR_HEADER"') == 2
    assert "retry budget preserved" in script


def test_review_hold_sweeper_prioritizes_small_diffs():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    assert "ORDER BY length(COALESCE(git_diff,'')) ASC, updated_at ASC" in script
    assert "-- 큰 diff 한 건이 복구 창을 독점하지 않도록" in script
    assert "rows=$(db_query \"$select_sql\") || rows=\"\"" not in script
    assert "review_hold 대상 조회 실패" in script


def test_review_hold_sweeper_service_uses_active_bluegreen_route():
    service = (ROOT / "scripts" / "aads-review-hold-sweeper.service").read_text(
        encoding="utf-8"
    )

    assert "Environment=AADS_API_URL=http://127.0.0.1\n" in service
    assert "AADS_API_URL=http://127.0.0.1:8100" not in service


def test_review_hold_sweeper_unreachable_enqueue_does_not_spend_retry_count():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    gate = script.index('if [[ "$unreachable" == "1" ]]; then')
    update_stmt = script.index("db_exec ", gate)
    update_end = script.index("\n", update_stmt)
    update_line = script[update_stmt:update_end]

    assert "review_retry_last_at=NOW()" in update_line
    assert "review_retry_count" not in update_line
    assert "ENQUEUE_UNREACHABLE" in script[gate:update_end + 400]
    assert "retry 미차감" in script[gate:update_end + 400]


def test_review_hold_sweeper_skips_terminal_job_before_approve_promotion():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    approve = script.index('if [[ "$verdict" == "APPROVE" ]]; then')
    terminal_guard = script.index('if [[ "$current_status" == "done" || "$current_status" == "error" ]]; then', approve)
    promote = script.index("SET status='awaiting_approval'", terminal_guard)

    assert approve < terminal_guard < promote
    assert 'log "  SWEEPER_SKIP_DONE job=${job_id} status=${current_status}"' in script[terminal_guard:promote]


def test_review_hold_sweeper_http_500_still_spends_retry_count():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    infra_retry_start = script.index("infra_retry() {")
    infra_retry_end = script.index("\n}", infra_retry_start)
    block = script[infra_retry_start:infra_retry_end]

    assert "review_retry_count=${nxt}" in block


def test_review_hold_sweeper_stops_after_three_consecutive_unreachable():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    assert 'if [[ "$consec_unreachable" -ge 3 ]]; then' in script
    assert "API 도달 불가 — 이번 스위프 중단" in script
    gate = script.index('if [[ "$unreachable" == "1" ]]; then')
    circuit = script.index('if [[ "$consec_unreachable" -ge 3 ]]; then', gate)
    stop_log = script.index("API 도달 불가 — 이번 스위프 중단", circuit)
    stop_break = script.index("break", stop_log)
    assert gate < circuit < stop_log < stop_break


def test_review_hold_sweeper_excludes_twice_failed_model_from_next_review():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    assert "COUNT(*) = 2 AND MIN(model_used) = MAX(model_used)" in script
    assert "REVIEW_MODEL_NO_RESPONSE','REVIEW_PARSER_FAILURE" in script
    assert "ORDER BY created_at DESC" in script
    assert "[REVIEW_EXCLUDE_MODELS: %s]" in script
    assert "MODEL_EXCLUDED" in script


def test_exhausted_review_is_handed_to_origin_session_once_with_bound_evidence():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")
    fn = script.split("enqueue_origin_adjudication() {", 1)[1].split("\n}\n", 1)[0]

    assert "JOIN chat_sessions s" in fn
    assert "s.tenant_id = j.tenant_id" in fn
    assert "lower(j.commit_hash) AS commit_sha" in fn
    assert "encode(digest(j.git_diff, 'sha256'), 'hex') AS diff_sha256" in fn
    assert "pipeline_review_adjudicate" in fn
    assert "q.status IN ('pending','claimed')" in fn
    assert "review_origin_adjudication_pending" in fn
    assert "ORIGIN_ADJUDICATION_RETRY_THRESHOLD" in script
    assert "REVIEW_ORIGIN_ADJUDICATION_RETRY_THRESHOLD:-3" in script
    assert "handoff_rows=$(db_query" in script
    assert "error_detail,'') <> 'review_origin_adjudication_pending'" in script
    pre_handoff = script.split("handoff_rows=$(db_query", 1)[1].split('if ! rows=$(db_query', 1)[0]
    assert 'if [[ "$DRY_RUN" == "1" ]]' in pre_handoff
    assert pre_handoff.index('if [[ "$DRY_RUN" == "1" ]]') < pre_handoff.index(
        'enqueue_origin_adjudication "$handoff_job" "$handoff_project"'
    )
    # infra_retry: 상한 도달 시 종결 전 이관 1회 + 임계치 도달 시 이관 1회
    assert script.count('enqueue_origin_adjudication "$jid" "$proj"') == 2
    # 모델 재검수 루프: 상한 도달 시 종결 전 이관 1회 + 임계치 도달 시 이관 1회
    assert script.count('enqueue_origin_adjudication "$job_id" "$project"') == 2
    # exhausted 스테이지도 표식 없는 상한 도달 작업은 종결 전에 이관을 먼저 시도한다.
    exhausted = script[script.index("exhausted_rows=$(db_query"):script.index("handoff_rows=$(db_query")]
    assert exhausted.index('enqueue_origin_adjudication "$exhausted_job"') < exhausted.index(
        'terminate_review_hold "$exhausted_job" "review_retry_exhausted"'
    )


def test_expired_origin_adjudication_is_terminal_and_cancels_deferred_reaction():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    terminal = script[script.index("terminate_review_hold() {"):script.index(
        "# dirty 워크트리 판정"
    )]
    assert "UPDATE chat_deferred_reactions" in terminal
    assert "status='failed'" in terminal
    assert "review-adjudication:${job_id}:%" in terminal
    assert "status IN ('pending','claimed')" in terminal

    expired_sweep = script[script.index("expired_origin_rows=$(db_query"):script.index(
        "# 이전 주기에 이미 임계치를 넘긴 작업"
    )]
    assert "error_detail='review_origin_adjudication_expired'" in expired_sweep
    # 만료는 바로 종결이 아니라 CTO 승계 helper 로 간다.
    assert 'expire_origin_adjudication "$expired_job" "$expired_project"' in expired_sweep
    assert 'terminate_review_hold "$expired_job" "review_origin_adjudication_expired"' not in expired_sweep

    timeout_branch = script[script.index('if [[ "$error_detail" == "review_origin_adjudication_pending" ]]'):]
    assert timeout_branch.index('expire_origin_adjudication "$job_id" "$project"') < timeout_branch.index("continue")
    assert 'terminate_review_hold "$job_id" "review_origin_adjudication_expired"' not in timeout_branch.split(
        "total=$((total + 1))", 1
    )[0]


def test_origin_adjudication_tool_and_state_machine_are_hash_and_session_bound():
    root = ROOT
    api = (root / "app/api/pipeline_runner.py").read_text(encoding="utf-8")
    registry = (root / "app/services/tool_registry.py").read_text(encoding="utf-8")
    executor = (root / "app/services/tool_executor.py").read_text(encoding="utf-8")
    chat_tools = (root / "app/api/ceo_chat_tools.py").read_text(encoding="utf-8")

    endpoint = api.split("async def adjudicate_review_from_origin_session", 1)[1]
    assert 'is_origin = str(row["chat_session_id"] or "").lower() == req.caller_session_id' in endpoint
    # CTO 승계 경로 — pending 표식 + 같은 tenant + role_key=CTO + 증거에 묶인 미소비 요청이 모두 필요하다.
    body = endpoint.split("# ─── AADS-211", 1)[0]
    assert "_CTO_ADJUDICATION_PENDING" in body
    assert 'caller_session["role_key"] != "CTO"' in body
    assert 'caller_session["tenant_id"] != row["tenant_id"]' in body
    assert 'f"review-adjudication-cto:{job_id}:{commit_sha}:{diff_sha256}"' in body
    assert "status IN ('pending', 'claimed')" in body
    assert "FOR UPDATE" in body.split("FROM chat_deferred_reactions", 1)[1]
    assert "cto_review_adjudicated" in body
    # 판정 API 자체는 push/deploy/빌드를 호출하지 않는다.
    for forbidden in ("subprocess", "create_subprocess", "deploy_job", "git push", "docker "):
        assert forbidden not in body
    assert 'row["tenant_id"] != row["session_tenant_id"]' in endpoint
    assert 'hashlib.sha256(diff_text.encode("utf-8")).hexdigest()' in endpoint
    assert "commit_sha != req.expected_commit_sha" in endpoint
    assert "diff_sha256 != req.expected_diff_sha256" in endpoint
    assert "status='awaiting_approval'" in endpoint
    assert "status='error', phase='review_failed'" in endpoint
    assert "review_adjudication_unknown" in endpoint
    assert "origin_review_adjudicated" in endpoint
    # asyncpg cannot infer values passed only through jsonb_build_object.
    # Keep every bound audit field explicitly typed to prevent a runtime 500.
    for parameter in range(6, 11):
        assert f"${parameter}::text" in endpoint
    for source in (registry, executor, chat_tools):
        assert "pipeline_review_adjudicate" in source


# ── AADS-REVIEWHOLD-DIRTY-STRAND-P0 ──────────────────────────────────────


def _sweeper() -> str:
    return (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")


def _runner() -> str:
    return (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """주석 줄을 뺀 실행 줄만 — 금지 문자열 단언이 설명문에 걸리지 않게."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_sweeper_never_creates_a_revision_itself():
    """계약 6 — 확정은 러너의 커밋 경로만 한다. 스위퍼가 직접 만들면 pre-commit
    훅(API 키 탐지·ruff·단위테스트)을 거치지 않은 리비전이 승인 큐로 올라간다."""
    script = _sweeper()

    assert "git commit" not in script
    assert "--no-verify" not in script
    assert "--allow-empty" not in script
    assert "git -C \"$worktree_dir\" add" not in script


def test_dirty_match_records_recovery_flag_without_promoting():
    """계약 2 — 같으면 표시만 남긴다. 승인 큐(awaiting_approval)로 직접 올리지 않는다."""
    script = _sweeper()
    fn_start = script.index("review_hold_dirty_recovery() {")
    fn_end = script.index("# 재시도 추적 컬럼", fn_start)
    body = script[fn_start:fn_end]

    handoff = body.index("error_detail='review_hold_recovery_pending'")
    assert "review_flag_category=NULL" in body[:handoff + 400]
    # 재검수 대상 select 는 review_flag_category 로 거른다 — 비워야 무한 재검수가 멈춘다.
    assert "status='awaiting_approval'" not in body
    assert body.index("return 10") > handoff


def test_diff_drift_is_terminal_and_excluded_from_re_review():
    """계약 3 — 다르면 error/review_hold_diff_drift 로 종결하고 재검수에서 뺀다."""
    script = _sweeper()

    assert "review_hold_diff_drift" in script
    terminate = script[script.index("terminate_review_hold() {"):script.index(
        "# dirty 워크트리 판정"
    )]
    assert "status='error'" in terminate
    assert "error_detail=${error_reason}" in terminate
    assert 'error_reason=$(sql_escape "${detail}: ${reason}")' in terminate
    assert "review_flag_category=NULL" in terminate
    assert "review_needs_retry=FALSE" in terminate


def test_needs_retry_is_selected_by_existing_sweeper():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")
    selection = script.split('select_sql="', 1)[1].split('"\n\ntotal=', 1)[0]
    assert "status='review_hold'" in selection
    assert "OR review_needs_retry IS TRUE" in selection
    assert 'terminate_review_hold "$missing_diff_job" "review_hold_no_artifact"' in script


def test_each_completed_review_consumes_exactly_one_attempt():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")
    assert "local nxt=$((prev + 1))" in script
    assert "next_retry=$((retry_count + 1))" in script
    assert "review_retry_count=${nxt}" in script
    assert "review_retry_count=${next_retry}" in script
    claim = script.split("claimed=$(db_query", 1)[1].split('if [[ "$claimed"', 1)[0]
    assert "SET review_retry_last_at=" in claim
    assert "SET review_retry_count=" not in claim


def test_exhausted_retry_becomes_terminal_with_reason():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")
    assert 'REVIEW_RETRY_MAX:-${SWEEP_MAX_RETRY:-3}' in script
    assert 'terminate_review_hold "$exhausted_job" "review_retry_exhausted"' in script
    assert 'terminate_review_hold "$job_id" "review_retry_exhausted"' in script
    assert "error_detail=${error_reason}" in script


def test_two_sweepers_cannot_claim_the_same_snapshot():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")
    claim = script.split("claimed=$(db_query", 1)[1].split('if [[ "$claimed"', 1)[0]
    assert "UPDATE pipeline_jobs" in claim
    assert "review_retry_count=${retry_count}" in claim
    assert "review_retry_last_at IS NOT DISTINCT FROM ${last_at_sql}" in claim
    assert "RETURNING job_id" in claim


def test_no_artifact_is_structurally_terminal():
    """계약 4 — 산출물이 없으면 몇 번을 다시 검수해도 결과가 같다. terminal 로 끝낸다."""
    script = _sweeper()
    helper = script[script.index("ensure_review_hold_commit() {"):script.index(
        "terminate_review_hold() {"
    )]

    no_artifact = helper.index("REVIEW_HOLD_NO_ARTIFACT")
    terminate = helper.index('terminate_review_hold "$job_id" "review_hold_no_artifact"', no_artifact)
    assert no_artifact < terminate < helper.index("return 11", terminate) + 1


def test_preserved_behaviours_survive_the_recovery_rework():
    """계약 5 — 기존 보존 항목이 리팩터에 쓸려나가지 않았는지 한 자리에서 본다."""
    script = _sweeper()

    # persisted 40자 SHA 우선
    assert "persisted_sha\" =~ ^[0-9a-f]{40}$" in script
    # clean HEAD 승격
    assert "commit_hash='${current_sha}'" in script
    # 2회 연속 실패 모델 제외
    assert "[REVIEW_EXCLUDE_MODELS: %s]" in script
    # http=000 은 재시도 예산을 쓰지 않는다
    assert "retry 미차감" in script


def test_runner_claims_recovery_flag_and_uses_existing_commit_path():
    """계약 2 — 재진입은 러너의 기존 커밋 경로(commit_job_worktree_for_approval)로만."""
    script = _runner()
    claim = script.index("claim_review_hold_recovery_job() {")
    worker = script.index("recover_review_hold_job() {", claim)
    body = _code_only(script[worker:script.index("# ── 작업 실행 ", worker)])

    assert "error_detail='review_hold_recovery_pending'" in script[claim:worker]
    assert "error_detail='review_hold_recovery_committing'" in script[claim:worker]
    assert "FOR UPDATE SKIP LOCKED" in script[claim:worker]
    # 커밋은 기존 경로가 만든다 — 복구 함수가 직접 리비전을 만들지 않는다.
    assert 'commit_job_worktree_for_approval "$job_id"' in body
    assert "git -C \"$worktree_dir\" commit" not in body
    assert "--no-verify" not in body
    # 성공하면 승인 대기로만 간다. push 는 기존 승인 경로(deploy_job)가 한다.
    assert "status='awaiting_approval'" in body
    assert "git push" not in body


def test_runner_recovery_claim_is_wired_into_the_main_loop():
    script = _runner()
    main_start = script.index("\nmain() {")
    main_body = script[main_start:script.index("\n_reap_bg_jobs() {", main_start)]

    assert 'hold_recovery=$(claim_review_hold_recovery_job "$project_filter"' in main_body
    assert 'recover_review_hold_job "$job_id" "$project" "$session_id" &' in main_body
    # 유휴 판정에도 포함돼야 복구 직후 즉시 재폴링한다.
    assert '-n "$hold_recovery"' in main_body


def test_runner_terminates_stalled_recovery_instead_of_waiting_forever():
    """러너가 복구 도중 재시작되면 표시만 남는다 — 영원히 기다리지 않는다."""
    script = _runner()
    stuck = script.index("_recover_stuck_jobs() {")
    body = script[stuck:script.index("\n_cleanup_old_artifacts() {", stuck)]

    assert "review_hold_recovery_stalled" in body
    assert "error_detail='review_hold_recovery_committing'" in body
    assert "INTERVAL '30 minutes'" in body


def test_recovery_keeps_phase_review_hold_for_the_dashboard_board():
    """대시보드 보드 상태(_TASK_BOARD_STATUS_SQL)는 phase='review_hold' 로 이 칸을
    판정한다. 복구 대기 중에 phase 를 바꾸면 멀쩡한 잡이 보드에서 error 로 보인다."""
    admin = (ROOT / "app" / "api" / "admin.py").read_text(encoding="utf-8")
    assert "\"WHEN phase = 'review_hold'" in admin

    script = _sweeper()
    fn_start = script.index("review_hold_dirty_recovery() {")
    body = _code_only(script[fn_start:script.index("# 재시도 추적 컬럼", fn_start)])
    assert "phase='review_hold_recovery'" not in body
    assert "SET phase=" not in body


# ── AADS-REVIEW-NORESP-SUCCESSION-M3: 상한 도달 → 원 세션 이관 → CTO 승계 ──────

import subprocess  # noqa: E402


def _function_source(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


def _run_bash(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, timeout=30, check=False
    )


def _infra_retry_harness(origin_rc: int) -> str:
    script = _sweeper()
    return f"""
set -eo pipefail
SWEEP_MAX_RETRY=3; ORIGIN_ADJUDICATION_RETRY_THRESHOLD=3
retried=0; consec_infra=0; terminated=0
sql_escape() {{ printf '%s' "$1"; }}
db_exec() {{ :; }}
log() {{ echo "LOG $*"; }}
enqueue_origin_adjudication() {{ echo "ENQUEUE_ORIGIN $1 $2"; return {origin_rc}; }}
terminate_review_hold() {{ echo "TERMINATE $2"; }}
{_function_source(script, "infra_retry")}
"""


def test_third_no_response_hands_over_to_origin_instead_of_terminating():
    """(a) 재시도 3회째 무응답 — 두 임계값 기본이 같아도 이관이 종결보다 먼저다."""
    out = _run_bash(
        _infra_retry_harness(0)
        + 'infra_retry runner-aaaaaa01 AADS 2 "category=REVIEW_MODEL_NO_RESPONSE"; echo "terminated=$terminated"'
    )
    assert out.returncode == 0, out.stderr
    assert "ENQUEUE_ORIGIN runner-aaaaaa01 AADS" in out.stdout
    assert "TERMINATE" not in out.stdout
    assert "terminated=0" in out.stdout


def test_third_no_response_without_origin_session_terminates_as_before():
    """(b) 이관 불가(원 세션 없음·commit/diff 없음) — 기존처럼 review_failed 종결."""
    out = _run_bash(
        _infra_retry_harness(1)
        + 'infra_retry runner-aaaaaa01 AADS 2 "category=REVIEW_MODEL_NO_RESPONSE"; echo "terminated=$terminated"'
    )
    assert out.returncode == 0, out.stderr
    assert "ENQUEUE_ORIGIN runner-aaaaaa01 AADS" in out.stdout
    assert "TERMINATE review_failed" in out.stdout
    assert "terminated=1" in out.stdout


def test_below_cap_retry_does_not_terminate_or_change_backoff_path():
    out = _run_bash(
        _infra_retry_harness(0)
        + 'infra_retry runner-aaaaaa01 AADS 0 "category=REVIEW_MODEL_NO_RESPONSE"; echo "terminated=$terminated"'
    )
    assert out.returncode == 0, out.stderr
    assert "TERMINATE" not in out.stdout
    assert "ENQUEUE_ORIGIN" not in out.stdout


def test_origin_enqueue_happens_before_terminate_in_infra_retry_source():
    block = _function_source(_sweeper(), "infra_retry")
    assert block.index("enqueue_origin_adjudication") < block.index("terminate_review_hold")


def _expire_harness(cto_rc: int, age_out: str = "130") -> str:
    script = _sweeper()
    return f"""
set -eo pipefail
ORIGIN_ADJUDICATION_TIMEOUT_MIN=120; terminated=0
db_query() {{ case "$1" in *"SELECT age_min FROM marked"*) printf '%s' '{age_out}';; *) printf '%s' 'review_origin_adjudication_pending';; esac; }}
db_exec() {{ echo "DB_EXEC"; }}
log() {{ echo "LOG $*"; }}
enqueue_cto_adjudication() {{ echo "ENQUEUE_CTO $1 $2"; return {cto_rc}; }}
terminate_review_hold() {{ echo "TERMINATE $2"; }}
{_function_source(script, "expire_origin_adjudication")}
"""


def test_origin_expiry_hands_over_to_cto_once_instead_of_terminating():
    """(c) 원 세션 판정 만료 — 종결이 아니라 CTO 독립판정 이관."""
    out = _run_bash(_expire_harness(0) + 'expire_origin_adjudication runner-aaaaaa01 AADS; echo "terminated=$terminated"')
    assert out.returncode == 0, out.stderr
    assert "ENQUEUE_CTO runner-aaaaaa01 AADS" in out.stdout
    assert "TERMINATE" not in out.stdout
    assert "terminated=0" in out.stdout


def test_origin_expiry_without_cto_session_falls_back_to_fail_closed():
    out = _run_bash(_expire_harness(1) + 'expire_origin_adjudication runner-aaaaaa01 AADS; echo "terminated=$terminated"')
    assert out.returncode == 0, out.stderr
    assert "TERMINATE review_origin_adjudication_expired" in out.stdout
    assert "terminated=1" in out.stdout


def test_origin_expiry_closes_unconsumed_origin_reaction_before_cto_handoff():
    block = _function_source(_sweeper(), "expire_origin_adjudication")
    assert "ohvis_task_id LIKE 'review-adjudication:${jid}:%'" in block
    assert block.index("UPDATE chat_deferred_reactions") < block.index("enqueue_cto_adjudication")


def test_cto_expiry_is_fail_closed_terminal():
    """(d) CTO 판정도 제한시간 안에 없으면 fail-closed 종결."""
    script = _sweeper()
    stage = script[script.index("expired_cto_rows=$(db_query"):script.index("# 산출물이 없는 needs_retry")]
    assert "error_detail='review_cto_adjudication_pending'" in stage
    assert "ORIGIN_ADJUDICATION_TIMEOUT_MIN" in stage
    assert 'terminate_review_hold "$cto_job" "review_cto_adjudication_expired"' in stage
    # 종결 시 CTO 요청도 함께 닫는다.
    terminal = script[script.index("terminate_review_hold() {"):script.index("# dirty 워크트리 판정")]
    assert "review-adjudication-cto:${job_id}:%" in terminal


def test_cto_pending_is_not_reselected_nor_terminated_as_exhausted():
    script = _sweeper()
    select = script.split('select_sql="', 1)[1].split('"\n\ntotal=', 1)[0]
    assert "<> 'review_cto_adjudication_pending'" in select
    exhausted = script[script.index("exhausted_rows=$(db_query"):script.index("handoff_rows=$(db_query")]
    assert "'review_origin_adjudication_pending', 'review_cto_adjudication_pending'" in exhausted
    handoff = script[script.index("handoff_rows=$(db_query"):script.index('if ! rows=$(db_query')]
    assert "'review_cto_adjudication_pending'" in handoff


def test_cto_enqueue_is_read_only_same_tenant_other_session_and_idempotent():
    """(e) 멱등 키 — 같은 증거의 pending/claimed 요청이 있으면 재삽입하지 않는다."""
    fn = _function_source(_sweeper(), "enqueue_cto_adjudication")
    assert "s.role_key = 'CTO'" in fn
    assert "s.tenant_id = t.tenant_id" in fn
    assert "s.id::text <> COALESCE(t.origin_session, '')" in fn
    assert "'review-adjudication-cto:' || j.job_id || ':' || lower(j.commit_hash) || ':' ||" in fn
    assert "encode(digest(j.git_diff, 'sha256'), 'hex') AS dedupe_key" in fn
    assert "q.status IN ('pending','claimed')" in fn
    assert "review_cto_adjudication_pending" in fn
    assert "pipeline_review_adjudicate" in fn
    assert "REVIEW_CTO_ADJUDICATION_SESSION_ID" in _sweeper()
    assert "s.updated_at DESC" in fn
    # 원 세션 만료 표식이 있을 때만 승계한다 — 임의 작업을 CTO 로 보내지 않는다.
    assert "IN ('review_origin_adjudication_expired', 'review_cto_adjudication_pending')" in fn


def _cto_enqueue_harness(deferred: str, state: str) -> str:
    script = _sweeper()
    return f"""
set -eo pipefail
handed=0; CTO_ADJUDICATION_SESSION_ID=""
log() {{ echo "LOG $*"; }}
db_query() {{ case "$1" in *"RETURNING id::text"*) printf '%s' '{deferred}';; *) printf '%s' '{state}';; esac; }}
{_function_source(script, "enqueue_cto_adjudication")}
"""


def test_cto_enqueue_duplicate_key_does_not_count_as_new_handoff_but_stays_pending():
    out = _run_bash(
        _cto_enqueue_harness("", "review_cto_adjudication_pending")
        + 'rc=0; enqueue_cto_adjudication runner-aaaaaa01 AADS || rc=$?; echo "rc=$rc handed=$handed"'
    )
    assert out.returncode == 0, out.stderr
    assert "rc=0 handed=0" in out.stdout
    assert "CTO_ADJUDICATION_DEDUPED_OR_BLOCKED" in out.stdout


def test_cto_enqueue_without_target_reports_not_handed_over():
    out = _run_bash(
        _cto_enqueue_harness("", "review_origin_adjudication_expired")
        + 'rc=0; enqueue_cto_adjudication runner-aaaaaa01 AADS || rc=$?; echo "rc=$rc handed=$handed"'
    )
    assert out.returncode == 0, out.stderr
    assert "rc=1 handed=0" in out.stdout


def test_cto_enqueue_new_request_counts_once():
    out = _run_bash(
        _cto_enqueue_harness("0f0f0f0f-aaaa-bbbb-cccc-111122223333", "review_cto_adjudication_pending")
        + 'rc=0; enqueue_cto_adjudication runner-aaaaaa01 AADS || rc=$?; echo "rc=$rc handed=$handed"'
    )
    assert out.returncode == 0, out.stderr
    assert "rc=0 handed=1" in out.stdout
    assert "CTO_ADJUDICATION_ENQUEUED" in out.stdout
