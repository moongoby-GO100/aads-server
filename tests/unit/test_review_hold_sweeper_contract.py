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


# AADS-SWEEPER-COMMITHASH-P0: 재검수를 통과해 awaiting_approval 로 승격되는
# 잡은 commit_hash 가 채워져 있어야 승인 API(app/api/pipeline_runner.py:1967)를
# 통과할 수 있다. 승격 직전에 ensure_review_hold_commit 이 그 값을 보장한다.


def test_review_hold_sweeper_promotion_is_gated_on_commit_hash():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")

    approve = script.index('if [[ "$verdict" == "APPROVE" ]]')
    recover = script.index('ensure_review_hold_commit "$job_id"', approve)
    recover_fail = script.index("continue", recover)
    promote = script.index("SET status='awaiting_approval'", recover)

    assert approve < recover < recover_fail < promote
    assert "commit_hash='${current_sha}'" in script


def test_review_hold_sweeper_refuses_promotion_without_worktree():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")
    helper = script[script.index("ensure_review_hold_commit() {"):script.index(
        "# 재시도 추적 컬럼"
    )]

    no_worktree = helper.index('[[ ! -d "$worktree_dir" ]]')
    reason = helper.index('reason="워크트리 없음', no_worktree)
    refuse = helper.index("return 1", reason)

    assert no_worktree < reason < refuse
    assert "review_feedback" in helper[reason:]


def test_review_hold_sweeper_refuses_promotion_when_worktree_dirty():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")
    helper = script[script.index("ensure_review_hold_commit() {"):script.index(
        "# 재시도 추적 컬럼"
    )]

    dirty_check = helper.index('git -C "$worktree_dir" status --porcelain')
    dirty_reason = helper.index('reason="워크트리 dirty', dirty_check)

    assert dirty_check < dirty_reason
    assert 'git -C "$worktree_dir" commit -m' not in helper
    assert 'git -C "$worktree_dir" add -A' not in helper


def test_review_hold_sweeper_uses_existing_head_when_worktree_clean():
    script = (ROOT / "scripts" / "review-hold-sweeper.sh").read_text(encoding="utf-8")
    helper = script[script.index("ensure_review_hold_commit() {"):script.index(
        "# 재시도 추적 컬럼"
    )]

    head_read = helper.index('git -C "$worktree_dir" rev-parse HEAD')
    persist = helper.index("commit_hash='${current_sha}'", head_read)
    recovered = helper.index('RECOVERED_COMMIT_SHA="$current_sha"', persist)

    assert head_read < persist < recovered
