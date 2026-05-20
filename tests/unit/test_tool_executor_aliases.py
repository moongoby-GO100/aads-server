import pytest


@pytest.mark.asyncio
async def test_query_db_alias_dispatches_to_query_database(monkeypatch):
    from app.services.tool_executor import ToolExecutor

    async def fake_query_database(self, inp):
        return {"query": inp.get("sql") or inp.get("query")}

    monkeypatch.setattr(ToolExecutor, "_query_database", fake_query_database)

    result = await ToolExecutor()._dispatch("query_db", {"sql": "SELECT 1"})

    assert result == {"query": "SELECT 1"}
