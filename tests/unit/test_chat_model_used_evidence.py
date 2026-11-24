"""들어오는 모델 값과 placeholder 최종 저장 회귀시험."""
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.services import chat_service
from tests.unit.test_turn_model_observability import _Pool, _Tx

async def _run_final_save(exec_row_extra, **save_kwargs):
    execution_id = uuid.uuid4()
    placeholder_id = uuid.uuid4()
    conn = AsyncMock()
    conn.transaction = lambda: _Tx()
    conn.fetchrow = AsyncMock(return_value={
        "status": "running", "completed_at": None,
        "owner_instance": chat_service._EXECUTION_OWNER_INSTANCE,
        "owner_epoch": 1, "generation_id": None,
        **exec_row_extra,
    })
    conn.fetchval = AsyncMock(return_value=placeholder_id)
    conn.execute = AsyncMock()
    answer = "수행 내역: 완료했습니다.\n검증 결과: 통과.\n남은 리스크: 없습니다."
    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service._rewrite_incomplete_final_report_once", new=AsyncMock(return_value=answer)),
        patch("app.services.chat_service._apply_todo_completion_gate", new=AsyncMock(return_value=(answer, {"all_completed": True}))),
        patch("app.services.chat_service._execution_has_newer_user_message", new=AsyncMock(return_value=False)),
        patch("app.services.chat_service._archive_interrupted_siblings_for_completed_execution", new=AsyncMock()),
        patch("app.services.chat_service._mark_execution_interrupted", new=AsyncMock()),
        patch("app.services.chat_service._extract_artifacts", new=AsyncMock()),
    ):
        await chat_service._save_and_update_session(
            uuid.uuid4(), answer, execution_id=execution_id, expected_owner_epoch=1, **save_kwargs,
        )
    completed = [
        c for c in conn.execute.await_args_list
        if "UPDATE chat_turn_executions" in c.args[0] and "status = 'completed'" in c.args[0]
    ]
    assert len(completed) == 1
    message_updates = [
        c for c in conn.execute.await_args_list
        if c.args[0].lstrip().startswith("UPDATE chat_messages") and "model_used" in c.args[0]
    ]
    return conn, message_updates



@pytest.mark.asyncio
@pytest.mark.parametrize("incoming,actual,expected", [
    (None, "gpt-6-astra", "gpt-6-astra"),
    ("codex", "gpt-6-astra", "gpt-6-astra"),
    ("recovered_from_redis", "gpt-6-astra", "recovered_from_redis"),
    ("recovered", "gpt-6-astra", "recovered"),
    ("stopped", "gpt-6-astra", "stopped"),
    ("interrupted", "gpt-6-astra", "interrupted"),
    (None, None, None),
    ("", None, None),
])
async def test_placeholder_final_save_uses_incoming_evidence(incoming, actual, expected):
    conn, updates = await _run_final_save(
        {"requested_model": "gpt-6-astra", "actual_model": None, "fallback_chain": "[]"},
        model_used=incoming, actual_model=actual,
    )
    assert len(updates) == 1
    sql = updates[0].args[0]
    # A streaming placeholder must be overwritten, without preserving its old label.
    assert "model_used = $3" in sql
    assert "CASE" not in sql
    assert updates[0].args[3] == expected
    if actual is None:
        records = [json.loads(c.args[2])["model_record"]
                   for c in conn.execute.await_args_list
                   if "quality_details" in c.args[0]
                   and "model_record" in str(c.args[2])]
        assert records == [{"reason": "actual_model_unconfirmed"}]


@pytest.mark.parametrize("state", sorted(chat_service._RECOVERY_DEDUPE_MODEL_USED))
def test_recovery_dedupe_preserved_after_normalization(state):
    rec = chat_service._normalize_turn_model_record(
        requested_model="gpt-6-astra", actual_model="gpt-6-astra", model_used=state,
    )
    first = {"role": "assistant", "model_used": rec["model_used"], "content": "복구 응답" * 20}
    second = {**first, "content": first["content"] + " 완료"}
    assert chat_service._dedupe_recovery_like_messages([first, second]) == [second]
