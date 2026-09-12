"""WP05 durable command lifecycle, idempotency, and generation fencing tests."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.models.chat import ChatCommandOut, ChatGenerationOut
from app.services import chat_commands, chat_protocol

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "176_chat_command_lifecycle_and_generations.sql"
)


# ── Test doubles matching the WP03/WP04 pool seam ───────────────────────────


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Conn:
    """Records every statement so fencing predicates can be asserted on."""

    def __init__(self, *, rows=None, schema_ready=True):
        self.rows = list(rows or [])
        self.schema_ready = schema_ready
        self.statements: list[tuple[str, tuple]] = []

    def transaction(self, **_kwargs):
        return _Transaction()

    async def fetchrow(self, query, *args):
        self.statements.append((query, args))
        if "to_regclass('chat_commands')" in query:
            return {
                "commands": self.schema_ready,
                "generations": self.schema_ready,
                "execution_generation_id": self.schema_ready,
            }
        return self.rows.pop(0) if self.rows else None

    async def fetch(self, query, *args):
        self.statements.append((query, args))
        return self.rows.pop(0) if self.rows else []

    def find(self, needle: str) -> tuple[str, tuple]:
        for query, args in self.statements:
            if needle in query:
                return query, args
        raise AssertionError(f"no statement contained {needle!r}")


def _command_row(**overrides):
    row = {
        "command_id": uuid4(),
        "tenant_id": uuid4(),
        "session_id": uuid4(),
        "command_type": "interrupt",
        "idempotency_key": "key-1",
        "request_fingerprint": chat_commands.command_fingerprint({"content": "hi"}),
        "status": "accepted",
        "execution_id": None,
        "generation_id": None,
        "owner_instance": None,
        "owner_epoch": 0,
        "attempt": 0,
        "result": None,
        "error": None,
        "created_at": None,
        "updated_at": None,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def _generation_row(**overrides):
    row = {
        "generation_id": uuid4(),
        "execution_id": uuid4(),
        "tenant_id": uuid4(),
        "session_id": uuid4(),
        "owner_instance": "aads-blue",
        "owner_epoch": 3,
        "attempt": 2,
        "command_id": None,
        "status": "active",
        "started_at": None,
        "ended_at": None,
        "max_owner_epoch": 3,
    }
    row.update(overrides)
    return row


@pytest.fixture
def patched_pool(monkeypatch):
    def _install(conn):
        monkeypatch.setattr(chat_commands, "_pool", lambda: _Pool(conn))
        return conn

    return _install


# ── Idempotency ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_submission_is_accepted_and_durable_before_side_effect(patched_pool):
    """The command row commits before any handler runs, so restart can recover it."""
    tenant_id, session_id = uuid4(), uuid4()
    conn = patched_pool(
        _Conn(rows=[_command_row(tenant_id=tenant_id, session_id=session_id)])
    )

    record, replayed = await chat_commands.begin_command(
        tenant_id=tenant_id,
        session_id=session_id,
        command_type="interrupt",
        idempotency_key="key-1",
        payload={"content": "hi"},
    )

    assert replayed is False
    assert record.status == "accepted"
    assert record.is_terminal is False
    insert, _args = conn.find("INSERT INTO chat_commands")
    # Durable acceptance is a single statement: unique key + tenant-scoped session.
    assert "ON CONFLICT ON CONSTRAINT uq_chat_commands_idempotency DO NOTHING" in insert
    assert "FROM chat_sessions" in insert


@pytest.mark.asyncio
async def test_replayed_key_returns_stored_result_without_rerunning(patched_pool):
    """A client retry of a settled command replays the result, never re-executes."""
    tenant_id, session_id = uuid4(), uuid4()
    stored = _command_row(
        tenant_id=tenant_id,
        session_id=session_id,
        status="succeeded",
        result=json.dumps({"queued": True}),
        completed_at="2026-09-13T00:00:00+00:00",
    )
    patched_pool(_Conn(rows=[None, stored]))

    record, replayed = await chat_commands.begin_command(
        tenant_id=tenant_id,
        session_id=session_id,
        command_type="interrupt",
        idempotency_key="key-1",
        payload={"content": "hi"},
    )

    assert replayed is True
    assert record.is_terminal is True
    assert record.result == {"queued": True}
    assert record.to_payload(replayed=True)["replayed"] is True


@pytest.mark.asyncio
async def test_inflight_replay_reports_progress_instead_of_duplicating(patched_pool):
    """SSE reconnect mid-command must not launch a second execution."""
    tenant_id, session_id = uuid4(), uuid4()
    stored = _command_row(tenant_id=tenant_id, session_id=session_id, status="running")
    patched_pool(_Conn(rows=[None, stored]))

    record, replayed = await chat_commands.begin_command(
        tenant_id=tenant_id,
        session_id=session_id,
        command_type="interrupt",
        idempotency_key="key-1",
        payload={"content": "hi"},
    )

    assert replayed is True
    assert record.status == "running"
    assert record.is_terminal is False


@pytest.mark.asyncio
async def test_key_reuse_with_different_body_fails_closed(patched_pool):
    """Reusing a key for a new body must not answer with the old command's result."""
    tenant_id, session_id = uuid4(), uuid4()
    stored = _command_row(
        tenant_id=tenant_id,
        session_id=session_id,
        status="succeeded",
        result=json.dumps({"queued": True}),
        completed_at="2026-09-13T00:00:00+00:00",
    )
    patched_pool(_Conn(rows=[None, stored]))

    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        await chat_commands.begin_command(
            tenant_id=tenant_id,
            session_id=session_id,
            command_type="interrupt",
            idempotency_key="key-1",
            payload={"content": "a completely different instruction"},
        )
    assert excinfo.value.code == "chat_idempotency_key_reuse_conflict"
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_unknown_session_fails_closed_rather_than_creating_a_command(patched_pool):
    patched_pool(_Conn(rows=[None, None]))
    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        await chat_commands.begin_command(
            tenant_id=uuid4(),
            session_id=uuid4(),
            command_type="interrupt",
            idempotency_key="key-1",
            payload={},
        )
    assert excinfo.value.code == "chat_command_session_not_found"
    assert excinfo.value.status_code == 404


def test_fingerprint_is_order_insensitive_but_value_sensitive():
    assert chat_commands.command_fingerprint(
        {"a": 1, "b": [1, 2]}
    ) == chat_commands.command_fingerprint({"b": [1, 2], "a": 1})
    assert chat_commands.command_fingerprint(
        {"a": 1}
    ) != chat_commands.command_fingerprint({"a": 2})
    # List order is semantic and must stay part of the identity.
    assert chat_commands.command_fingerprint(
        {"b": [1, 2]}
    ) != chat_commands.command_fingerprint({"b": [2, 1]})


@pytest.mark.parametrize(
    "key, code",
    [
        (None, "missing_chat_idempotency_key"),
        ("   ", "missing_chat_idempotency_key"),
        ("x" * 201, "invalid_chat_idempotency_key"),
    ],
)
def test_idempotency_key_validation_fails_closed(key, code):
    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        chat_commands.normalize_idempotency_key(key)
    assert excinfo.value.code == code


def test_unsupported_command_type_is_rejected():
    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        chat_commands.normalize_command_type("delete_everything")
    assert excinfo.value.code == "unsupported_chat_command_type"
    assert excinfo.value.status_code == 400


# ── Fencing ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transitions_are_owner_epoch_fenced(patched_pool):
    """Every lifecycle write carries the epoch predicate, not just a status check."""
    tenant_id = uuid4()
    conn = patched_pool(
        _Conn(rows=[_command_row(tenant_id=tenant_id, status="running", owner_epoch=4)])
    )

    await chat_commands.mark_command_running(
        command_id=uuid4(), tenant_id=tenant_id, owner_epoch=4
    )
    update, args = conn.find("UPDATE chat_commands")
    assert "owner_epoch <= $3" in update
    assert "status IN ('accepted', 'running')" in update
    assert 4 in args


@pytest.mark.asyncio
async def test_stale_writer_is_told_it_lost_the_fence(patched_pool):
    """A refused update is reported as fenced, never as a silent no-op."""
    tenant_id, command_id = uuid4(), uuid4()
    patched_pool(
        _Conn(
            rows=[
                None,  # fenced UPDATE matched nothing
                _command_row(
                    command_id=command_id,
                    tenant_id=tenant_id,
                    status="running",
                    owner_epoch=9,
                ),
            ]
        )
    )

    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        await chat_commands.complete_command(
            command_id=command_id,
            tenant_id=tenant_id,
            result={"queued": True},
            owner_epoch=2,
        )
    assert excinfo.value.code == "chat_command_fenced"
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_settling_a_terminal_command_twice_is_refused(patched_pool):
    tenant_id, command_id = uuid4(), uuid4()
    patched_pool(
        _Conn(
            rows=[
                None,
                _command_row(
                    command_id=command_id,
                    tenant_id=tenant_id,
                    status="succeeded",
                    completed_at="2026-09-13T00:00:00+00:00",
                ),
            ]
        )
    )

    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        await chat_commands.fail_command(
            command_id=command_id,
            tenant_id=tenant_id,
            code="whatever",
            message="late failure",
        )
    assert excinfo.value.code == "chat_command_already_terminal"


@pytest.mark.asyncio
async def test_missing_command_is_404_not_a_fence_error(patched_pool):
    patched_pool(_Conn(rows=[None, None]))
    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        await chat_commands.complete_command(
            command_id=uuid4(), tenant_id=uuid4(), result={}
        )
    assert excinfo.value.code == "chat_command_not_found"
    assert excinfo.value.status_code == 404


# ── Stable generation identity ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generation_identity_is_stable_for_one_owner_epoch(patched_pool):
    execution_id, tenant_id = uuid4(), uuid4()
    row = _generation_row(execution_id=execution_id, tenant_id=tenant_id, owner_epoch=3)
    conn = patched_pool(_Conn(rows=[row, dict(row)]))

    first = await chat_commands.resolve_generation(
        execution_id=execution_id, tenant_id=tenant_id, owner_epoch=3
    )
    second = await chat_commands.resolve_generation(
        execution_id=execution_id, tenant_id=tenant_id, owner_epoch=3
    )

    assert first.generation_id == second.generation_id
    assert first.owner_epoch == "3"
    assert first.superseded_by_epoch is None
    select, _args = conn.find("FROM chat_execution_generations g")
    assert "g.tenant_id = $2" in select


@pytest.mark.asyncio
async def test_superseded_generation_fails_closed_for_the_old_writer(patched_pool):
    """An epoch behind the newest one must not be handed a usable generation."""
    execution_id, tenant_id = uuid4(), uuid4()
    patched_pool(
        _Conn(
            rows=[
                _generation_row(
                    execution_id=execution_id,
                    tenant_id=tenant_id,
                    owner_epoch=2,
                    status="superseded",
                    max_owner_epoch=5,
                )
            ]
        )
    )

    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        await chat_commands.resolve_generation(
            execution_id=execution_id, tenant_id=tenant_id, owner_epoch=2
        )
    assert excinfo.value.code == "chat_generation_fenced"
    assert "superseded by epoch 5" in str(excinfo.value)


@pytest.mark.asyncio
async def test_latest_generation_lookup_does_not_fence_itself(patched_pool):
    """Reading without an epoch returns the newest generation rather than erroring."""
    execution_id, tenant_id = uuid4(), uuid4()
    patched_pool(
        _Conn(
            rows=[
                _generation_row(
                    execution_id=execution_id,
                    tenant_id=tenant_id,
                    owner_epoch=5,
                    max_owner_epoch=5,
                )
            ]
        )
    )
    record = await chat_commands.resolve_generation(
        execution_id=execution_id, tenant_id=tenant_id
    )
    assert record.owner_epoch == "5"
    assert record.status == "active"


@pytest.mark.asyncio
async def test_generation_settle_requires_being_the_newest_epoch(patched_pool):
    execution_id, tenant_id = uuid4(), uuid4()
    conn = patched_pool(
        _Conn(
            rows=[
                _generation_row(
                    execution_id=execution_id,
                    tenant_id=tenant_id,
                    owner_epoch=4,
                    status="completed",
                    max_owner_epoch=4,
                )
            ]
        )
    )
    record = await chat_commands.settle_generation(
        execution_id=execution_id,
        tenant_id=tenant_id,
        owner_epoch=4,
        status="completed",
    )
    assert record.status == "completed"
    update, _args = conn.find("UPDATE chat_execution_generations g")
    assert "peer.owner_epoch > g.owner_epoch" in update
    assert "g.status = 'active'" in update


@pytest.mark.asyncio
async def test_generation_settle_rejects_non_terminal_status(patched_pool):
    patched_pool(_Conn())
    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        await chat_commands.settle_generation(
            execution_id=uuid4(),
            tenant_id=uuid4(),
            owner_epoch=1,
            status="active",
        )
    assert excinfo.value.code == "unsupported_chat_generation_status"


# ── Schema / fail-closed ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_migration_fails_closed_with_503(patched_pool):
    patched_pool(_Conn(schema_ready=False))
    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        await chat_commands.get_command(command_id=uuid4(), tenant_id=uuid4())
    assert excinfo.value.code == "chat_command_migration_required"
    assert excinfo.value.status_code == 503


def test_unknown_stored_status_is_a_schema_mismatch_not_a_default():
    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        chat_commands._command_from_row(_command_row(status="half_done"))
    assert excinfo.value.code == "chat_command_status_unknown"
    assert excinfo.value.status_code == 500


def test_unreadable_stored_payload_is_surfaced_not_dropped():
    with pytest.raises(chat_commands.ChatCommandError) as excinfo:
        chat_commands._command_from_row(_command_row(result="{not json"))
    assert excinfo.value.code == "chat_command_payload_unreadable"


# ── Restart / slot-switch recovery ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_settles_orphans_by_execution_outcome(monkeypatch):
    """Restart recovery mirrors the execution: completed succeeds, anything else fails."""
    tenant_id = uuid4()
    completed_id, dead_id = uuid4(), uuid4()
    conn = _Conn(
        rows=[
            [
                {
                    "command_id": completed_id,
                    "tenant_id": tenant_id,
                    "session_id": uuid4(),
                    "owner_epoch": 1,
                    "execution_id": uuid4(),
                    "execution_status": "completed",
                },
                {
                    "command_id": dead_id,
                    "tenant_id": tenant_id,
                    "session_id": uuid4(),
                    "owner_epoch": 1,
                    "execution_id": None,
                    "execution_status": None,
                },
            ]
        ]
    )
    monkeypatch.setattr(chat_commands, "_pool", lambda: _Pool(conn))

    settled: list[tuple[UUID, str]] = []

    async def _complete(**kwargs):
        settled.append((kwargs["command_id"], "succeeded"))

    async def _fail(**kwargs):
        settled.append((kwargs["command_id"], kwargs["code"]))

    monkeypatch.setattr(chat_commands, "complete_command", _complete)
    monkeypatch.setattr(chat_commands, "fail_command", _fail)

    result = await chat_commands.recover_orphaned_commands(limit=10, grace_seconds=60)

    assert result["scanned"] == 2
    assert result["recovered_succeeded"] == 1
    assert result["recovered_failed"] == 1
    assert (completed_id, "succeeded") in settled
    assert (dead_id, "chat_command_owner_lost") in settled
    candidates, args = conn.find("FROM chat_commands c")
    # A live, still-leased execution must never be swept.
    assert "te.lease_expires_at <= NOW()" in candidates
    assert "c.status IN ('accepted', 'running')" in candidates
    assert 60 in args


@pytest.mark.asyncio
async def test_recovery_is_a_no_op_on_the_inactive_blue_green_slot(monkeypatch):
    from app.services import chat_service as svc

    monkeypatch.setattr(svc, "_is_local_active_api_slot", lambda: False)
    result = await chat_commands.recover_orphaned_commands()
    assert result["inactive_slot_skipped"] == 1
    assert result["scanned"] == 0


# ── Contract / migration shape ──────────────────────────────────────────────


def test_capabilities_advertise_wp05_without_activating_v2(monkeypatch):
    monkeypatch.delenv("AADS_CHAT_WP05_MIGRATION_READY", raising=False)
    baseline = chat_protocol.chat_protocol_capabilities()
    assert baseline["production_ready"] is False
    assert baseline["snapshot"]["generation_identity"] == "unavailable"
    assert baseline["command_lifecycle"]["migration_ready"] is False
    # WP03/WP04 advertisements must survive untouched.
    assert "chat.explicit_fenced_repair.v2" in baseline["capabilities"]
    assert "chat.read_model.v2" in baseline["capabilities"]
    assert "stable_generation_identity" in baseline["activation_requires"]

    monkeypatch.setenv("AADS_CHAT_WP05_MIGRATION_READY", "true")
    ready = chat_protocol.chat_protocol_capabilities()
    assert ready["snapshot"]["generation_identity"] == "stable_per_owner_epoch"
    assert ready["command_lifecycle"]["migration_ready"] is True
    # The operational v2 flag stays closed regardless of WP05 readiness.
    assert ready["production_ready"] is False
    assert ready["command_lifecycle"]["production_ready"] is False
    assert "chat.command_lifecycle.v2" in ready["capabilities"]
    assert "chat.generation_identity.v2" in ready["capabilities"]
    assert chat_protocol.negotiate_contract_version(None) == 1


def test_migration_is_additive_idempotent_and_documents_rollback():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "CREATE INDEX CONCURRENTLY" not in sql
    assert "ROLLBACK PATH" in sql
    # Idempotent: no bare CREATE/ALTER that would fail on a second run.
    assert "CREATE TABLE IF NOT EXISTS chat_commands" in sql
    assert "CREATE TABLE IF NOT EXISTS chat_execution_generations" in sql
    assert "ADD COLUMN IF NOT EXISTS generation_id UUID" in sql
    assert sql.count("CREATE OR REPLACE FUNCTION") == 4
    for trigger in (
        "trg_chat_execution_a_generation",
        "trg_chat_generation_fence",
        "trg_chat_command_transition",
    ):
        assert f"DROP TRIGGER IF EXISTS {trigger}" in sql
        assert f"CREATE TRIGGER {trigger}" in sql

    # Stable identity is keyed by the WP03 fence, not a new ownership contract.
    assert "UNIQUE (execution_id, owner_epoch)" in sql
    assert "UNIQUE (tenant_id, session_id, command_type, idempotency_key)" in sql
    # Fail-closed fencing, not silent skips.
    assert "chat_generation_epoch_reversal" in sql
    assert "chat_command_epoch_reversal" in sql
    assert "chat_command_terminal_immutable" in sql
    assert "chat_generation_terminal_immutable" in sql
    # WP04's checkpoint owner fence is preserved verbatim.
    assert "WHERE chat_execution_checkpoints.owner_epoch <= EXCLUDED.owner_epoch" in sql


def test_wp04_and_wp03_writers_are_preserved():
    """WP05 extends the existing fenced writers; it does not replace them."""
    from app.services import chat_repair, chat_service

    assert hasattr(chat_repair, "repair_completed_execution_projection")
    assert hasattr(chat_service, "_claim_execution_lease")
    assert hasattr(chat_service, "_heartbeat_execution_lease")
    assert hasattr(chat_service, "_EXECUTION_OWNER_INSTANCE")
    assert hasattr(chat_protocol, "get_stream_snapshot")
    # Generation assignment is a DB trigger precisely so these signatures are
    # unchanged; assert the lease claim still takes only its original arguments.
    import inspect

    params = inspect.signature(chat_service._claim_execution_lease).parameters
    assert list(params) == ["conn", "execution_id", "status", "error_message"]


def test_transport_models_round_trip_the_command_and_generation_payloads():
    record = chat_commands.ChatCommandRecord(
        command_id=uuid4(),
        tenant_id=uuid4(),
        session_id=uuid4(),
        command_type="interrupt",
        idempotency_key="key-1",
        request_fingerprint="f" * 64,
        status="succeeded",
        owner_epoch="7",
        attempt=1,
        result={"queued": True},
    )
    out = ChatCommandOut(**record.to_payload(replayed=True))
    assert out.terminal is True
    assert out.replayed is True
    assert out.owner_epoch == "7"

    generation = chat_commands.ChatGenerationRecord(
        generation_id=uuid4(),
        execution_id=uuid4(),
        session_id=uuid4(),
        owner_epoch="2",
        attempt=2,
        status="active",
    )
    assert ChatGenerationOut(**generation.to_payload()).attempt == 2


def test_router_exposes_additive_wp05_routes_only():
    from app.routers import chat as chat_router

    paths = {
        (route.path, tuple(sorted(route.methods)))
        for route in chat_router.router.routes
        if hasattr(route, "methods")
    }
    assert ("/chat/sessions/{session_id}/commands", ("POST",)) in paths
    assert ("/chat/sessions/{session_id}/commands/{command_id}", ("GET",)) in paths
    assert ("/chat/executions/{execution_id}/generation", ("GET",)) in paths
    assert ("/chat/commands/recover", ("POST",)) in paths
    # Pre-existing command routes are untouched.
    assert ("/chat/sessions/{session_id}/interrupt", ("POST",)) in paths
    assert ("/chat/sessions/{session_id}/resume", ("POST",)) in paths
    assert ("/chat/sessions/{session_id}/stop", ("POST",)) in paths
    assert ("/chat/sessions/{session_id}/repair-projection", ("POST",)) in paths
    assert chat_router._DURABLE_COMMAND_HANDLERS == {"interrupt", "stop", "resume"}
