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

    enqueue = script.index('/api/v1/review/code-diff/requests"')
    poll = script.index('/api/v1/review/code-diff/requests/${request_id}')
    verdict = script.index("verdict=$(jq -r '.verdict // empty'")

    assert "review_request_id UUID" in script
    assert enqueue < poll < verdict
    assert "request_status" in script[poll:verdict]
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
    assert script.count('enqueue_origin_adjudication "$jid" "$proj"') == 1
    assert script.count('enqueue_origin_adjudication "$job_id" "$project"') == 1


def test_origin_adjudication_tool_and_state_machine_are_hash_and_session_bound():
    root = ROOT
    api = (root / "app/api/pipeline_runner.py").read_text(encoding="utf-8")
    registry = (root / "app/services/tool_registry.py").read_text(encoding="utf-8")
    executor = (root / "app/services/tool_executor.py").read_text(encoding="utf-8")
    chat_tools = (root / "app/api/ceo_chat_tools.py").read_text(encoding="utf-8")

    endpoint = api.split("async def adjudicate_review_from_origin_session", 1)[1]
    assert 'str(row["chat_session_id"] or "").lower() != req.caller_session_id' in endpoint
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
    assert "error_detail='${detail}'" in terminate
    assert "review_flag_category=NULL" in terminate
    assert "review_needs_retry=FALSE" in terminate


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
