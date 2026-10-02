"""채팅 턴 최종 기록의 모델·폴백 정규화 (기록 시점)."""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import chat_service
from app.services.llm_metric_normalization import classify_fallback_chain

_norm = chat_service._normalize_turn_model_record


def test_unrecorded_switch_gets_minimal_chain():
    rec = _norm(requested_model="claude-opus-5-5", actual_model="claude-sonnet-5-5")
    chain = rec["fallback_chain"]
    assert chain["from"] == "claude-opus-5-5"
    assert chain["to"] == "claude-sonnet-5-5"
    assert chain["reason"] == "model_switch_unrecorded"
    assert chain["stages"] == []
    assert classify_fallback_chain(json.dumps(chain)) == "fallback_succeeded"
    assert rec["actual_to_store"] == "claude-sonnet-5-5"


def test_display_name_actual_is_stored_as_slug_and_raw_is_kept():
    rec = _norm(requested_model="claude-opus-5-5", actual_model="GPT-6 Astra (Codex CLI)")
    assert rec["actual_to_store"] == "gpt-6-astra"
    assert rec["raw_actual_model"] == "GPT-6 Astra (Codex CLI)"
    assert rec["fallback_chain"]["to"] == "gpt-6-astra"
    assert rec["fallback_chain"]["raw_actual_model"] == "GPT-6 Astra (Codex CLI)"


def test_same_model_after_normalization_writes_no_chain():
    rec = _norm(requested_model="claude-opus-5-5", actual_model="claude-opus-5-5")
    assert rec["fallback_chain"] is None

    rec = _norm(requested_model="codex:gpt-6-astra", actual_model="GPT-6 Astra (Codex CLI)")
    assert rec["fallback_chain"] is None
    assert rec["actual_to_store"] == "gpt-6-astra"
    assert rec["raw_actual_model"] == "GPT-6 Astra (Codex CLI)"


def test_existing_fallback_info_takes_precedence():
    info = [{"from": "claude-opus-5-5", "to": "claude-sonnet-5-5", "reason": "429", "kind": "retry"}]
    rec = _norm(requested_model="claude-opus-5-5", actual_model="gpt-6-astra", fallback_info=info)
    assert rec["fallback_chain"] is info


def test_existing_row_chain_is_not_overwritten():
    existing = json.dumps([{"from": "a", "to": "b", "reason": "x"}])
    rec = _norm(requested_model="claude-opus-5-5", actual_model="gpt-6-astra", existing_chain=existing)
    assert rec["fallback_chain"] is None


def test_state_values_keep_raw_actual_and_write_no_chain():
    for state in ("unverified", "stopped", "mixture", "unknown"):
        rec = _norm(requested_model="claude-opus-5-5", actual_model=state)
        assert rec["actual_to_store"] == state
        assert rec["fallback_chain"] is None
        assert rec["raw_actual_model"] is None


def test_non_llm_response_labels_are_not_model_switches():
    for label in ("semantic_cache", "discussion-orchestrator", "loop_handler"):
        rec = _norm(requested_model="claude-opus-5-5", actual_model=label)
        assert rec["fallback_chain"] is None
        assert rec["actual_to_store"] == label


def test_missing_actual_is_not_a_switch():
    rec = _norm(requested_model="claude-opus-5-5", actual_model=None)
    assert rec["fallback_chain"] is None
    assert rec["actual_to_store"] is None


def test_provider_only_model_used_is_filled_with_actual_slug():
    rec = _norm(requested_model="gpt-6-astra", actual_model="GPT-6 Astra (Codex CLI)", model_used="codex")
    assert rec["model_used"] == "gpt-6-astra"


def test_concrete_model_used_is_untouched():
    rec = _norm(requested_model="claude-opus-5-5", actual_model="claude-sonnet-5-5", model_used="GPT-6 Astra (Codex CLI)")
    assert rec["model_used"] == "GPT-6 Astra (Codex CLI)"


def test_provider_only_model_used_without_real_actual_stays():
    rec = _norm(requested_model="claude-opus-5-5", actual_model="codex", model_used="codex")
    assert rec["model_used"] == "codex"


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


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
    return completed[0].args, message_updates


@pytest.mark.asyncio
async def test_final_save_records_unrecorded_switch_and_slug():
    args, _ = await _run_final_save(
        {"requested_model": "claude-opus-5-5", "actual_model": None, "fallback_chain": "[]"},
        model_used="GPT-6 Astra (Codex CLI)",
    )
    # args: sql, id, msg_id, requested, actual, owner, epoch, chain_json, last_event_id
    assert args[4] == "gpt-6-astra"
    chain = json.loads(args[7])
    assert chain["reason"] == "model_switch_unrecorded"
    assert chain["from"] == "claude-opus-5-5"
    assert chain["to"] == "gpt-6-astra"


@pytest.mark.asyncio
async def test_final_save_same_model_leaves_chain_untouched():
    args, _ = await _run_final_save(
        {"requested_model": "claude-opus-5-5", "actual_model": None, "fallback_chain": "[]"},
        model_used="claude-opus-5-5",
    )
    assert args[7] is None


@pytest.mark.asyncio
async def test_final_save_prefers_explicit_fallback_info_and_fills_provider_model_used():
    info = [{"from": "claude-opus-5-5", "to": "gpt-6-astra", "reason": "429", "kind": "retry"}]
    args, message_updates = await _run_final_save(
        {"requested_model": "claude-opus-5-5", "actual_model": "gpt-6-astra", "fallback_chain": "[]"},
        model_used="codex",
        fallback_info=info,
    )
    assert json.loads(args[7]) == info
    assert message_updates
    # UPDATE chat_messages SET content, intent, model_used, ...
    assert message_updates[0].args[3] == "gpt-6-astra"
