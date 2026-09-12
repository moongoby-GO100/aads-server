from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_async_review_api_persists_before_scheduling_and_polls_db():
    source = (ROOT / "app" / "api" / "code_review.py").read_text(encoding="utf-8")

    insert = source.index("INSERT INTO code_review_requests")
    schedule = source.index("_schedule_review_request(request_id)", insert)
    poll_route = source.index('@router.get("/code-diff/requests/{request_id}")')

    assert insert < schedule < poll_route
    assert "status_code=status.HTTP_202_ACCEPTED" in source
    assert "WHERE request_id=$1 AND status='queued'" in source
    assert "SET status='completed'" in source
    assert "stale async review recovered" in source


def test_async_review_migration_is_durable_and_idempotent():
    migration = (ROOT / "migrations" / "177_code_review_async_requests.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS code_review_requests" in migration
    assert "request_id UUID PRIMARY KEY" in migration
    assert "ADD COLUMN IF NOT EXISTS review_request_id UUID" in migration
