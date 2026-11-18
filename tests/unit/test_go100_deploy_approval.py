"""GO100 approval hashing, provenance and CLI failure boundaries."""
import os
os.environ.setdefault("JWT_SECRET_KEY", "go100-isolated-test-only-fixed-secret")

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.go100_deploy_approval import (
    ApprovalDenied, _match_run, _valid_snapshot, approve, snapshot_hash, source_tuple,
)


def fixture_values():
    tenant, session = uuid4(), uuid4()
    source = dict(job_id="job", project="GO100", tenant_id=str(tenant),
                  chat_session_id=str(session), commit_hash="a" * 40,
                  requested_by="admin", session_user_id="admin")
    now = datetime.now(timezone.utc)
    run = dict(id=1, project="GO100", runner_job_id="job", chat_session_id=session,
               requested_by="admin", release_sha="a" * 40, component="backend",
               target_env="test", status="awaiting_approval", request_payload={})
    snapshot = dict(id=uuid4(), run_id=1, approved_by="admin", tenant_id=tenant,
                    source_job_id="job", source_session_id=session, source_tuple=source,
                    project="GO100", component="backend", lane="api_only", release_sha="a" * 40,
                    target_env="test", issued_at=now, expires_at=now + timedelta(hours=1), schema_version=1)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    return run, snapshot, source


def test_hash_round_trip_and_tamper():
    _, snapshot, _ = fixture_values()
    wire = copy.deepcopy(snapshot)
    wire["source_tuple"] = json.dumps(wire["source_tuple"])
    assert snapshot_hash(wire) == snapshot["snapshot_hash"]
    _valid_snapshot(snapshot, snapshot["issued_at"])
    wire["approved_by"] = "forged"
    with pytest.raises(ApprovalDenied, match="hash_mismatch"):
        _valid_snapshot(wire, snapshot["issued_at"])
    with pytest.raises(ApprovalDenied, match="expired"):
        _valid_snapshot(snapshot, snapshot["expires_at"])


@pytest.mark.parametrize("field,value", [("release_sha", "b"*40), ("component", "frontend"),
                                            ("target_env", "prod"), ("runner_job_id", None),
                                            ("chat_session_id", uuid4()), ("requested_by", "forged")])
def test_source_rejects_mutation(field, value):
    run, snapshot, source = fixture_values()
    job = dict(job_id="job", project="GO100", tenant_id=snapshot["tenant_id"],
               chat_session_id=snapshot["source_session_id"], commit_hash="a"*40)
    session = dict(id=job["chat_session_id"], tenant_id=job["tenant_id"], user_id="admin")
    conn = SimpleNamespace(fetchrow=AsyncMock(side_effect=[job, session]))
    run[field] = value
    with pytest.raises(ApprovalDenied):
        if field in ("component", "target_env"):
            _match_run(run, snapshot, source)
        else:
            asyncio.run(source_tuple(conn, run))


@pytest.mark.parametrize("principal", [None, {}, {"user_id": "admin"}, {"is_internal_admin": True}])
def test_no_payload_authority(principal):
    with pytest.raises(ApprovalDenied, match="authenticated_admin"):
        asyncio.run(approve(None, 1, principal))


def test_payload_lane_cannot_expand_scope():
    run, snapshot, source = fixture_values()
    run["request_payload"] = {"approved": True, "actor": "admin", "lane": "batch"}
    with pytest.raises(ApprovalDenied, match="lane"):
        _match_run(run, snapshot, source)


def test_cli_denial_is_sanitized(monkeypatch, capsys):
    from scripts import go100_deploy_approval as cli
    async def fail(args):
        raise RuntimeError("postgresql://secret:credential@host")
    monkeypatch.setattr(cli, "verify", fail)
    assert cli.main(["--run-id", "1", "--owner-instance", "worker", "--owner-epoch", "1"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "reason": "database_validation_unavailable"}


def test_endpoint_injects_principal():
    import ast
    from pathlib import Path
    tree = ast.parse(Path("app/api/ops.py").read_text())
    endpoint = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "approve_deploy_run_api")
    assert "Depends(require_internal_admin)" in ast.unparse(endpoint.args)
    assert "principal=principal" in ast.unparse(endpoint)


def test_disable_preserves_fail_closed_boundary(monkeypatch):
    from app.services.go100_deploy_approval import validate_contract
    monkeypatch.setenv("AADS_GO100_DEPLOY_APPROVAL_ENABLED", "false")
    with pytest.raises(ApprovalDenied, match="disabled"):
        asyncio.run(approve(None, 1, {"user_id": "admin", "is_internal_admin": True}))
    with pytest.raises(ApprovalDenied, match="disabled"):
        asyncio.run(validate_contract(None, 1, "worker", "7"))


def test_other_project_approval_unchanged():
    from contextlib import asynccontextmanager
    from app.services.deploy_control import approve_deploy_run
    @asynccontextmanager
    async def transaction():
        yield
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={"id": 1, "project": "AADS", "status": "awaiting_approval"}),
                           execute=AsyncMock(), transaction=transaction)
    result = asyncio.run(approve_deploy_run(conn, 1, actor="existing-actor"))
    assert result["ok"] and result["status"] == "queued"
    assert "existing-actor" in result["detail"]
    assert conn.execute.await_count == 2


def test_actual_endpoint_passes_dependency_principal():
    """Separate interpreter isolates existing tests' optional dependency stubs."""
    import os
    import subprocess
    import sys
    script = '''
import asyncio
from unittest.mock import AsyncMock
from fastapi import FastAPI
import httpx
from app.api import ops
from app.auth import require_internal_admin
from app.services import deploy_control
principal = {"user_id": "server-admin", "is_internal_admin": True}
app = FastAPI()
app.include_router(ops.router)
async def authenticated():
    return principal
app.dependency_overrides[require_internal_admin] = authenticated
conn = type("Connection", (), {"close": AsyncMock()})()
ops._get_conn = AsyncMock(return_value=conn)
deploy_control.approve_deploy_run = AsyncMock(return_value={"ok": True, "status": "queued"})
async def exercise():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/ops/deploy/1/approve", json={"actor": "forged"})
        assert response.status_code == 200, response.text
asyncio.run(exercise())
assert deploy_control.approve_deploy_run.call_args.kwargs["principal"] == principal
assert conn.close.await_count == 1
'''
    env = {**os.environ, "JWT_SECRET_KEY": "go100-isolated-test-only-fixed-secret"}
    completed = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr


def test_invalid_job_session_is_explicit_denial():
    run, snapshot, _ = fixture_values()
    job = dict(job_id="job", project="GO100", tenant_id=snapshot["tenant_id"],
               chat_session_id="not-a-uuid", commit_hash="a"*40)
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=job))
    with pytest.raises(ApprovalDenied, match="source_session_invalid_uuid"):
        asyncio.run(source_tuple(conn, run))
    assert conn.fetchrow.await_count == 1


def test_integration_fixture_has_no_destructive_sql():
    import ast
    import re
    from pathlib import Path
    tree = ast.parse(Path("tests/integration/test_go100_deploy_approval_postgres.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not re.search(r"\bDROP\s+(?:SCHEMA|CONSTRAINT)\b|\bTRUNCATE\s+\w+", node.value, re.I)
    fixture = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "postgres")
    assert "scope='module'" not in ast.unparse(fixture)


def test_approved_run_field_drift_has_distinct_reason():
    from app.services.go100_deploy_approval import _match_run_fields
    run, snapshot, _ = fixture_values()
    run['release_sha'] = 'b' * 40
    with pytest.raises(ApprovalDenied, match='^run_release_sha_mismatch$'):
        _match_run_fields(run, snapshot)


@pytest.mark.parametrize('epoch', ['', None, 7])
def test_contract_rejects_invalid_owner_epoch_before_db(epoch):
    from app.services.go100_deploy_approval import validate_contract
    with pytest.raises(ApprovalDenied, match='^live_lease_required$'):
        asyncio.run(validate_contract(None, 1, 'worker', epoch))
