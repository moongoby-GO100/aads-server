from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import goals
from app.services import goal_manager
from app.services.milestone_idempotency import (
    MilestoneConflict, MilestoneGoalNotFound, create_milestone, fingerprint,
)

TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GOAL = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("key", ["", " ", "\t\n", "x" * 201])
def test_invalid_key(key):
    with pytest.raises(ValidationError):
        goals.MilestoneCreateRequest(title="C0", sequence=1, idempotency_key=key)


def test_optional_key_and_exact_key_identity():
    assert goals.MilestoneCreateRequest(title="C0", sequence=1).idempotency_key is None
    assert goals.MilestoneCreateRequest(title="C0", sequence=1, idempotency_key=" key ").idempotency_key == " key "


def test_fingerprint_canonical_object_order_and_payload_changes():
    assert fingerprint({"a": 1, "b": [2, 3]}) == fingerprint({"b": [2, 3], "a": 1})
    assert fingerprint({"a": None}) != fingerprint({"a": ""})
    assert fingerprint({"b": [2, 3]}) != fingerprint({"b": [3, 2]})


@pytest.mark.parametrize("field,value", [("description", ""), ("description", "설명"), ("epics", []), ("epics", [{"title": "C0"}])])
def test_unsupported_fields_rejected_before_db(field, value):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.add_milestone(GOAL, goals.MilestoneCreateRequest(
            title="C0", sequence=1, **{field: value}), {"tenant": {"id": TENANT}}))
    assert exc.value.status_code == 422


class Connection:
    def __init__(self):
        self.fetchrow = AsyncMock(return_value={"id": GOAL, "project": "AADS"})

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.parametrize("error,status", [(None, None), (MilestoneConflict(), 409), (MilestoneGoalNotFound(), 404)])
def test_route_passes_authenticated_tenant_and_maps_errors(monkeypatch, error, status):
    from app.core import db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: Connection())
    service = AsyncMock(return_value={"milestone_id": "first", "status": "pending"}, side_effect=error)
    monkeypatch.setattr(goal_manager.goal_state_machine, "add_milestone", service)
    # Unknown client tenant never overrides authentication context.
    req = goals.MilestoneCreateRequest(title="C0", sequence=1, idempotency_key="key", tenant_id="untrusted")
    call = goals.add_milestone(GOAL, req, {"tenant": {"id": TENANT}})
    if status:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call)
        assert exc.value.status_code == status
    else:
        assert asyncio.run(call)["milestone_id"] == "first"
    assert service.call_args.kwargs["tenant_id"] == TENANT
    assert service.call_args.kwargs["idempotency_key"] == "key"


def test_keyed_service_requires_tenant_before_pool_access():
    with pytest.raises(ValueError, match="tenant_id"):
        asyncio.run(create_milestone(None, tenant_id=None, goal_id=GOAL, key="k", payload={}))


def test_keyed_manager_delegates_complete_effective_payload(monkeypatch):
    from app.services import milestone_idempotency
    machine = goal_manager.GoalStateMachine()
    monkeypatch.setattr(machine, "_pool", AsyncMock(return_value="pool"))
    monkeypatch.setattr(machine, "_trace", AsyncMock())
    create = AsyncMock(return_value={"milestone_id": "first"})
    monkeypatch.setattr(milestone_idempotency, "create_milestone", create)
    assert asyncio.run(machine.add_milestone(GOAL, "C0", 1, tenant_id=TENANT, idempotency_key="k")) == {"milestone_id": "first"}
    assert create.call_args.kwargs["payload"] == {
        "title": "C0", "sequence": 1, "completion_criteria": None,
        "auto_advance": True, "owner_session_id": None, "owner_role_key": None,
    }


def test_nested_key_rejected_without_changing_goal_registration():
    req = goals.GoalCreateRequest(project="AADS", title="goal", milestones=[
        goals.MilestoneCreateRequest(title="C0", sequence=1, idempotency_key="nested")])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.create_goal(req, {"tenant": {"id": TENANT}, "user": {"user_id": "test"}}))
    assert exc.value.status_code == 422
    assert exc.value.detail == "milestone_idempotency_requires_existing_goal"


@pytest.mark.parametrize("replay,conflict", [(False, False), (True, False), (True, True)])
def test_service_reservation_and_replay_contract(replay, conflict):
    import json
    payload = dict(title="C0", sequence=1, completion_criteria=None, auto_advance=True,
                   owner_session_id=None, owner_role_key=None)
    saved = dict(milestone_id="original", status="pending", owner_session_id=None, owner_role_key=None)
    conn = Connection()
    conn.transaction = lambda: conn
    conn.fetchval = AsyncMock(return_value=None if replay else "reserved")
    conn.execute = AsyncMock()
    conn.fetchrow.side_effect = [{"id": GOAL}, {
        "payload_fingerprint": fingerprint({**payload, "title": "different"}) if conflict else fingerprint(payload),
        "response": json.dumps(saved),
    }]
    call = create_milestone(conn, tenant_id=TENANT, goal_id=GOAL, key="key", payload=payload)
    if conflict:
        with pytest.raises(MilestoneConflict):
            asyncio.run(call)
    else:
        result = asyncio.run(call)
        if replay:
            assert result == saved
        else:
            assert result["status"] == "pending"
    assert conn.execute.await_count == (0 if replay else 1)
    assert "ON CONFLICT" in conn.fetchval.call_args.args[0]
    assert "FOR SHARE" in conn.fetchrow.call_args_list[0].args[0]


def test_missing_tenant_goal_does_not_reserve_key():
    conn = Connection()
    conn.transaction = lambda: conn
    conn.fetchrow.return_value = None
    conn.fetchval = AsyncMock()
    payload = dict(title="C0", sequence=1, completion_criteria=None, auto_advance=True,
                   owner_session_id=None, owner_role_key=None)
    with pytest.raises(MilestoneGoalNotFound):
        asyncio.run(create_milestone(conn, tenant_id=TENANT, goal_id=GOAL, key="k", payload=payload))
    conn.fetchval.assert_not_awaited()


def _postgres_exercise():
    import runpy
    from pathlib import Path
    return runpy.run_path(str(Path(__file__).parents[1] / "integration/test_milestone_idempotency_postgres.py"))["exercise"]


@pytest.mark.parametrize("url", [None, "postgresql://localhost/production", "not-a-url", "postgresql://localhost/a_test/production", "postgresql://localhost/a_test?database=production"])
@pytest.mark.parametrize("required", [False, True])
def test_database_acceptance_configuration_gates(monkeypatch, url, required):
    import asyncpg
    monkeypatch.setenv("AADS_MILESTONE_DB_REQUIRED", "1" if required else "0")
    if url is None:
        monkeypatch.delenv("AADS_MILESTONE_TEST_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("AADS_MILESTONE_TEST_DATABASE_URL", url)
    connect = AsyncMock()
    monkeypatch.setattr(asyncpg, "connect", connect)
    outcome = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(outcome, match="isolated"):
        asyncio.run(_postgres_exercise()())
    connect.assert_not_awaited()


def test_required_database_connection_failure_is_redacted(monkeypatch):
    import asyncpg
    monkeypatch.setenv("AADS_MILESTONE_DB_REQUIRED", "1")
    monkeypatch.setenv("AADS_MILESTONE_TEST_DATABASE_URL", "postgresql://localhost/isolated_test")
    connect = AsyncMock(side_effect=OSError("secret-connection-details"))
    monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(pytest.fail.Exception, match="connection failed") as exc:
        asyncio.run(_postgres_exercise()())
    assert "secret-connection-details" not in str(exc.value)
    connect.assert_awaited_once()


def test_postgres_failure_preserves_schema_and_closes_connection(monkeypatch, capsys):
    import asyncpg
    monkeypatch.setenv("AADS_MILESTONE_DB_REQUIRED", "1")
    monkeypatch.setenv("AADS_MILESTONE_TEST_DATABASE_URL", "postgresql://localhost/isolated_test")
    admin = AsyncMock()
    admin.execute.side_effect = [None, RuntimeError("setup failed")]
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=admin))
    with pytest.raises(RuntimeError, match="setup failed"):
        asyncio.run(_postgres_exercise()())
    statements = [call.args[0] for call in admin.execute.await_args_list]
    assert len(statements) == 2
    assert statements[0].startswith('CREATE SCHEMA "milestone_idempotency_test_')
    assert statements[1].startswith("SET search_path")
    admin.close.assert_awaited_once()
    evidence = capsys.readouterr().out
    schema = statements[0].split('"')[1]
    assert evidence.strip() == f"milestone_test_schema={schema} result=failed preserved=true"
