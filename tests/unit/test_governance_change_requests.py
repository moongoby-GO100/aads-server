from __future__ import annotations

import pytest

from app.api import governance


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _TxCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _GovernanceConn:
    def __init__(self):
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _TxCtx()

    async def fetch(self, query, *args):
        if "information_schema.columns" in query:
            table_name = args[0]
            known = {
                "prompt_assets",
                "prompt_asset_versions",
                "session_blueprints",
                "prompt_change_requests",
                "cr_approvals",
                "compiled_prompt_provenance",
            }
            if table_name in known:
                return [{"column_name": "id"}]
            return []
        if "FROM prompt_change_requests" in query:
            return [
                {
                    "id": 7,
                    "target_type": "prompt_asset",
                    "target_ref": "system.intent_classifier",
                    "title": "Classifier tuning",
                    "proposed_patch": {"content": "new"},
                    "proposed_by": "qa_agent",
                    "confidence": 0.91,
                    "priority": 10,
                    "status": "pending",
                    "reviewer": None,
                    "reviewed_at": None,
                    "expires_at": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        return []

    async def fetchval(self, query, *args):
        if "SELECT COUNT(*)::int FROM" in query:
            table_name = query.split("FROM", 1)[1].strip().split()[0]
            counts = {
                "prompt_assets": 2,
                "prompt_asset_versions": 3,
                "session_blueprints": 1,
                "prompt_change_requests": 4,
                "cr_approvals": 2,
                "compiled_prompt_provenance": 5,
            }
            return counts[table_name]
        return None

    async def fetchrow(self, query, *args):
        if "INSERT INTO prompt_change_requests" in query:
            return {
                "id": 9,
                "target_type": args[0],
                "target_ref": args[1],
                "title": args[2],
                "proposed_patch": args[3],
                "proposed_by": args[4],
                "confidence": args[5],
                "priority": args[6],
                "status": "pending",
                "reviewer": None,
                "reviewed_at": None,
                "expires_at": None,
                "created_at": None,
                "updated_at": None,
            }
        if "SELECT id" in query and "FROM prompt_change_requests" in query:
            return {"id": args[0]}
        if "UPDATE prompt_change_requests" in query:
            return {
                "id": args[0],
                "target_type": "prompt_asset",
                "target_ref": "system.intent_classifier",
                "title": "Classifier tuning",
                "proposed_patch": {"content": "new"},
                "proposed_by": "qa_agent",
                "confidence": 0.91,
                "priority": 10,
                "status": args[1],
                "reviewer": args[2],
                "reviewed_at": None,
                "expires_at": None,
                "created_at": None,
                "updated_at": None,
            }
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))


@pytest.mark.asyncio
async def test_get_governance_registry_reports_table_counts(monkeypatch):
    conn = _GovernanceConn()
    monkeypatch.setattr(governance, "get_pool", lambda: _Pool(conn))

    result = await governance.get_governance_registry()

    assert result["ready_tables"] == 6
    assert result["registry"]["prompt_assets"]["count"] == 2
    assert result["registry"]["compiled_prompt_provenance"]["count"] == 5


@pytest.mark.asyncio
async def test_create_prompt_change_request(monkeypatch):
    conn = _GovernanceConn()
    monkeypatch.setattr(governance, "get_pool", lambda: _Pool(conn))

    body = governance.PromptChangeRequestCreateRequest(
        target_type="prompt_asset",
        target_ref="system.intent_classifier",
        title="Classifier tuning",
        proposed_patch={"content": "new"},
        proposed_by="qa_agent",
        confidence=0.91,
        priority=10,
    )
    result = await governance.create_prompt_change_request(body)

    assert result["id"] == 9
    assert result["status"] == "pending"
    assert result["target_ref"] == "system.intent_classifier"


@pytest.mark.asyncio
async def test_review_prompt_change_request_records_approval(monkeypatch):
    conn = _GovernanceConn()
    monkeypatch.setattr(governance, "get_pool", lambda: _Pool(conn))

    body = governance.PromptChangeRequestReviewRequest(
        action="approved",
        approver="ceo",
        comment="looks good",
    )
    result = await governance.review_prompt_change_request(7, body)

    assert result["status"] == "approved"
    assert result["reviewer"] == "ceo"
    assert conn.executed
    assert conn.executed[0][1][0] == 7
