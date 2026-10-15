"""기한 지난 승인 대기 카드를 expired 로 닫는 주기 작업 (approval-system-hardening FR-9)."""
import asyncio
import pathlib
import re

from app.services import approval_expiry

REPO = pathlib.Path(__file__).resolve().parents[2]


class _FakePool:
    """UPDATE 의 WHERE 를 흉내 내지 않는다 — 넘어간 SQL 과 인자만 기록한다."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


def _sql() -> str:
    return re.sub(r"\s+", " ", approval_expiry._EXPIRE_SQL)


def test_only_overdue_pending_rows_are_selected():
    sql = _sql()
    assert "WHERE decision = 'pending' AND expires_at < now()" in sql
    # approved/rejected/notified/acknowledged 는 조건에 걸리지 않는다.
    assert "approved" not in sql and "rejected" not in sql


def test_sets_expired_with_audit_fields():
    sql = _sql()
    assert "SET decision = 'expired'" in sql
    assert "reason = $1" in sql
    assert "decided_at = now()" in sql
    assert approval_expiry.EXPIRED_REASON == "expired_without_decision"


def test_goal_workflow_cards_are_left_alone():
    # 그 결정 경로가 expires_at 을 보지 않아, 닫으면 카드와 change_set 이 어긋난다.
    assert "gate_source <> 'goal_workflow'" in _sql()


def test_returns_closed_count_and_passes_reason():
    pool = _FakePool([{"gate_source": "next_step"}, {"gate_source": "next_step"},
                      {"gate_source": "live_trading"}])
    n = asyncio.run(approval_expiry.expire_stale_pending_approvals(pool))
    assert n == 3
    _, args = pool.calls[0]
    assert args[0] == "expired_without_decision"


def test_nothing_to_close_returns_zero():
    pool = _FakePool([])
    assert asyncio.run(approval_expiry.expire_stale_pending_approvals(pool)) == 0


def test_scheduled_in_main_on_existing_scheduler():
    src = (REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert 'id="approval_expiry"' in src
    assert "_run_approval_expiry" in src
    assert "from app.services.approval_expiry import expire_stale_pending_approvals" in src


def test_expired_is_not_counted_as_waiting_by_readers():
    """대기로 세는 쪽은 모두 decision='pending' 을 명시한다 — 'expired' 는 걸리지 않는다."""
    src = (REPO / "app" / "api" / "project_docs.py").read_text(encoding="utf-8")
    assert "decision = 'pending' AND tier = 'approve'" in src
    assert 'r["decision"] == "pending" and r["active"]' in src
    # 결정 프롬프트는 approved 가 아닌 것(거절·만료)을 실행하지 않을 항목으로 분류한다.
    assert "skipped = [r for r in rows if r not in approved]" in src
