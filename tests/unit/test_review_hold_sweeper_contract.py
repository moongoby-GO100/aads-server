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
