import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from app.services import deploy_session_callback as callback

SESSION_ID = "11111111-1111-4111-8111-111111111111"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.rows

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1"


def _row(status: str, previous: str = "pending"):
    return {
        "id": 42,
        "project": "AADS",
        "component": "api",
        "release_sha": "a" * 40,
        "status": status,
        "phase": "completed",
        "error_summary": "candidate health failed",
        "chat_session_id": SESSION_ID,
        "session_notification_attempts": 1,
        "previous_notification_status": previous,
    }


def test_claim_uses_skip_locked_owner_fence_and_stale_recovery():
    conn = _Conn([_row("failed")])

    rows = asyncio.run(callback.claim_pending_notifications(conn, owner="slot:1", limit=3))

    assert rows[0]["id"] == 42
    sql, args = conn.fetch_calls[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "session_notification_claimed_at <" in sql
    assert "session_notification_owner = $4" in sql
    assert args[-1] == "slot:1"


def test_success_posts_report_without_triggering_ai(monkeypatch):
    conn = _Conn()
    reports = []

    async def fake_report(**kwargs):
        reports.append(kwargs)
        return SimpleNamespace(posted=True, skipped_reason="")

    monkeypatch.setattr(callback, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(callback, "post_session_report", fake_report)

    assert asyncio.run(callback.process_claimed_notification(_row("success"), owner="slot:1")) is True
    assert len(reports) == 1
    assert reports[0]["trigger_reaction"] is False
    assert conn.execute_calls[-1][1][2] == "notified"


def test_failure_posts_once_then_triggers_same_session(monkeypatch):
    conn = _Conn()
    reports = []
    reactions = []

    async def fake_report(**kwargs):
        reports.append(kwargs)
        return SimpleNamespace(posted=True, skipped_reason="")

    async def fake_reaction(session_id, prompt):
        reactions.append((session_id, prompt))

    fake_chat_service = ModuleType("app.services.chat_service")
    fake_chat_service.trigger_ai_reaction = fake_reaction
    monkeypatch.setitem(sys.modules, "app.services.chat_service", fake_chat_service)
    monkeypatch.setattr(callback, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(callback, "post_session_report", fake_report)

    assert asyncio.run(callback.process_claimed_notification(_row("failed"), owner="slot:1")) is True
    assert len(reports) == 1
    assert reactions[0][0] == SESSION_ID
    assert "배포 #42" in reactions[0][1]
    assert conn.execute_calls[-1][1][2] == "notified"


def test_reported_failure_retries_reaction_without_duplicate_report(monkeypatch):
    conn = _Conn()
    reactions = []

    async def fail_if_reported(**_kwargs):
        raise AssertionError("report must not be inserted twice")

    async def fake_reaction(session_id, prompt):
        reactions.append((session_id, prompt))

    fake_chat_service = ModuleType("app.services.chat_service")
    fake_chat_service.trigger_ai_reaction = fake_reaction
    monkeypatch.setitem(sys.modules, "app.services.chat_service", fake_chat_service)
    monkeypatch.setattr(callback, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(callback, "post_session_report", fail_if_reported)

    assert asyncio.run(
        callback.process_claimed_notification(_row("blocked", previous="reported"), owner="slot:1")
    ) is True
    assert len(reactions) == 1


def test_migration_and_startup_contracts_are_wired():
    root = Path(__file__).parents[2]
    migration = (root / "migrations/20260921_deploy_session_callbacks.sql").read_text()
    deploy = (root / "deploy.sh").read_text()
    main = (root / "app/main.py").read_text()

    assert "chat_session_id UUID REFERENCES chat_sessions" in migration
    assert "idx_deploy_runs_session_notification_pending" in migration
    assert "20260921_deploy_session_callbacks.sql" in deploy
    assert "deploy_session_callback_poller()" in main
