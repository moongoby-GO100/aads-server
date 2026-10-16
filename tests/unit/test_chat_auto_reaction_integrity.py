from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import chat_service


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_current_system_trigger_is_reinserted_once_as_last_user_turn():
    message_id = uuid.uuid4()
    history = [
        {"id": str(uuid.uuid4()), "role": "user", "content": "old question"},
        {"id": str(uuid.uuid4()), "role": "assistant", "content": "old answer"},
    ]

    bound = chat_service._bind_current_turn_to_history(
        history,
        user_message_id=message_id,
        content="목표 등록 후 PRD와 마일스톤을 작성하고 진행해",
    )
    status = chat_service._current_turn_binding_status(bound, message_id)

    assert bound[-1] == {
        "id": str(message_id),
        "role": "user",
        "content": "목표 등록 후 PRD와 마일스톤을 작성하고 진행해",
    }
    assert status == {
        "current_user_included": True,
        "current_user_occurrences": 1,
        "current_user_is_last": True,
    }


def test_current_turn_binding_deduplicates_and_moves_existing_row_to_end():
    message_id = uuid.uuid4()
    bound = chat_service._bind_current_turn_to_history(
        [
            {"id": str(message_id), "role": "user", "content": "persisted safe content"},
            {"id": str(uuid.uuid4()), "role": "assistant", "content": "stale answer"},
            {"id": str(message_id), "role": "user", "content": "duplicate"},
        ],
        user_message_id=message_id,
        content="exact current instruction",
    )

    assert sum(item.get("id") == str(message_id) for item in bound) == 1
    assert bound[-1]["content"] == "exact current instruction"


def test_current_turn_is_verified_in_final_llm_messages():
    messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "exact current instruction"}],
        },
    ]

    assert chat_service._current_turn_llm_binding_status(
        messages,
        "exact current instruction",
    ) == {
        "current_user_in_llm": True,
        "current_user_is_last_llm": True,
    }


def test_task_local_epoch_cannot_be_replaced_by_global_reclaim_epoch():
    execution_id = uuid.uuid4()
    id_token = chat_service._current_execution_id.set(str(execution_id))
    epoch_token = chat_service._current_execution_owner_epoch.set(1)
    chat_service._execution_owner_epochs[str(execution_id)] = 2
    try:
        assert chat_service._expected_execution_write_epoch(execution_id) == 1
        assert chat_service._expected_execution_write_epoch(execution_id, 7) == 7
    finally:
        chat_service._current_execution_owner_epoch.reset(epoch_token)
        chat_service._current_execution_id.reset(id_token)
        chat_service._execution_owner_epochs.pop(str(execution_id), None)


@pytest.mark.asyncio
async def test_interim_save_fences_old_producer_instead_of_adopting_new_epoch():
    session_id = str(uuid.uuid4())
    execution_id = uuid.uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "status": "running",
        "completed_at": None,
        "owner_instance": chat_service._EXECUTION_OWNER_INSTANCE,
        "owner_epoch": 2,
        "lease_expires_at": object(),
    })
    state = {
        "content": "old generation output",
        "tool_count": 0,
        "last_tool": "",
        "tool_events": [],
        "execution_id": str(execution_id),
        "owner_epoch": 1,
    }

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service._heartbeat_execution_lease", new=AsyncMock()) as heartbeat,
        patch("app.services.chat_service._archive_competing_stream_placeholder", new=AsyncMock()) as archive,
    ):
        await chat_service._interim_save_streaming(session_id, state, force=True)

    assert state["_terminal_execution_closed"] is True
    assert state["_producer_incomplete_exit"] == "execution_lease_epoch_mismatch"
    assert state["owner_epoch"] == 1
    heartbeat.assert_not_awaited()
    archive.assert_not_awaited()


class _SessionModelConn:
    """chat_sessions.current_model 조회만 흉내 낸다. 값이 예외면 던진다."""

    def __init__(self, values):
        self.values = list(values)
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if isinstance(value, BaseException):
            raise value
        return value


async def _run_internal_reaction(session_id, conn):
    """내부 자동 반응을 돌려 (결과, wrapper/send 호출 기록)을 돌려준다."""
    execution_id = str(uuid.uuid4())
    calls = []

    async def fake_send_message_stream(**kwargs):
        calls.append(("send", kwargs))
        yield "data: {}\n\n"

    async def fake_wrapper(stream, wrapped_session_id):
        calls.append(("wrapper", wrapped_session_id))
        async for item in stream:
            yield item
        chat_service._streaming_state[session_id] = {"execution_id": execution_id}

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service.send_message_stream", new=fake_send_message_stream),
        patch("app.services.chat_service.with_background_completion", new=fake_wrapper),
        patch("app.services.chat_service._heartbeat_asyncio.sleep", new=AsyncMock()),
    ):
        try:
            result = await chat_service._consume_internal_reaction_stream(
                session_id,
                "PRD를 작성하고 배포까지 진행해",
            )
        finally:
            chat_service._streaming_state.pop(session_id, None)
    return result, execution_id, calls


@pytest.mark.asyncio
async def test_internal_auto_reaction_uses_background_completion_wrapper():
    session_id = str(uuid.uuid4())
    conn = _SessionModelConn(["codex:gpt-6.1-sol"])

    result, execution_id, calls = await _run_internal_reaction(session_id, conn)

    assert result == execution_id
    assert calls[0] == ("wrapper", session_id)
    assert calls[1][0] == "send"
    assert calls[1][1]["session_id"] == session_id
    assert calls[1][1]["intent_override"] == "auto_reaction"
    assert conn.queries[0][1] == (uuid.UUID(session_id),)


@pytest.mark.asyncio
async def test_internal_auto_reaction_forwards_session_selected_model_verbatim():
    session_id = str(uuid.uuid4())

    _, _, calls = await _run_internal_reaction(
        session_id, _SessionModelConn(["codex:gpt-6.1-sol"]),
    )

    # provider 접두어는 send_message_stream 의 기존 정규화 규칙이 처리하므로 그대로 넘긴다.
    assert calls[1][1]["model_override"] == "codex:gpt-6.1-sol"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "", "   "])
async def test_internal_auto_reaction_blank_selection_keeps_default_routing(stored):
    _, _, calls = await _run_internal_reaction(
        str(uuid.uuid4()), _SessionModelConn([stored]),
    )

    assert calls[1][1]["model_override"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["auto", "mixture"])
async def test_internal_auto_reaction_keeps_explicit_auto_selection(stored):
    _, _, calls = await _run_internal_reaction(
        str(uuid.uuid4()), _SessionModelConn([stored]),
    )

    assert calls[1][1]["model_override"] == stored


@pytest.mark.asyncio
async def test_internal_auto_reaction_uses_latest_selection_after_model_change():
    session_id = str(uuid.uuid4())
    conn = _SessionModelConn(["claude-opus", "codex:gpt-6.1-sol"])

    _, _, first = await _run_internal_reaction(session_id, conn)
    _, _, second = await _run_internal_reaction(session_id, conn)

    assert first[1][1]["model_override"] == "claude-opus"
    assert second[1][1]["model_override"] == "codex:gpt-6.1-sol"


@pytest.mark.asyncio
async def test_internal_auto_reaction_retries_transient_lookup_failure():
    conn = _SessionModelConn([OSError("pool closed"), "codex:gpt-6.1-sol"])

    _, _, calls = await _run_internal_reaction(str(uuid.uuid4()), conn)

    assert len(conn.queries) == 2
    assert calls[1][1]["model_override"] == "codex:gpt-6.1-sol"


@pytest.mark.asyncio
async def test_internal_auto_reaction_lookup_failure_never_calls_default_model():
    session_id = str(uuid.uuid4())
    conn = _SessionModelConn([OSError("db down")])
    send_calls = []

    async def fake_send_message_stream(**kwargs):
        send_calls.append(kwargs)
        yield "data: {}\n\n"

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service.send_message_stream", new=fake_send_message_stream),
        patch("app.services.chat_service._heartbeat_asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(chat_service._AutoReactionModelLookupError):
            await chat_service._consume_internal_reaction_stream(session_id, "보고")

    assert send_calls == []
    assert len(conn.queries) == len(chat_service._AUTO_REACTION_MODEL_LOOKUP_BACKOFF) + 1


async def _run_trigger_with_lookup_failure(*, from_deferred_queue):
    session_id = str(uuid.uuid4())
    chat_service._ai_reaction_active.pop(session_id, None)
    check_conn = AsyncMock()
    check_conn.fetchval = AsyncMock(return_value=None)
    enqueue = AsyncMock(return_value="deferred-id")
    complete_task = AsyncMock()

    with (
        patch("app.services.chat_service._is_local_active_api_slot", return_value=True),
        patch("app.services.chat_service._session_has_live_execution", new=AsyncMock(return_value=False)),
        patch("app.services.chat_service.get_pool", return_value=_Pool(check_conn)),
        patch(
            "app.services.chat_service._consume_internal_reaction_stream",
            new=AsyncMock(side_effect=chat_service._AutoReactionModelLookupError("lookup")),
        ),
        patch("app.services.chat_service._enqueue_deferred_reaction", new=enqueue),
        patch("app.services.ohvis_task_manager.complete_task", new=complete_task),
    ):
        task = await chat_service.trigger_ai_reaction(
            session_id,
            "runner-aaaaaa01 완료 보고",
            "ohvis-task-1",
            _already_safe=True,
            _from_deferred_queue=from_deferred_queue,
        )
        result = await task

    assert session_id not in chat_service._ai_reaction_active
    complete_task.assert_not_awaited()
    return result, enqueue


@pytest.mark.asyncio
async def test_direct_trigger_requeues_durably_when_selected_model_lookup_fails():
    result, enqueue = await _run_trigger_with_lookup_failure(from_deferred_queue=False)

    assert result == chat_service._AUTO_REACTION_RETRY_MARKER
    enqueue.assert_awaited_once()
    assert enqueue.await_args.args[1] == "runner-aaaaaa01 완료 보고"
    assert enqueue.await_args.args[2] == "ohvis-task-1"


@pytest.mark.asyncio
async def test_deferred_trigger_reports_retry_marker_instead_of_requeueing_itself():
    result, enqueue = await _run_trigger_with_lookup_failure(from_deferred_queue=True)

    assert result == chat_service._AUTO_REACTION_RETRY_MARKER
    enqueue.assert_not_awaited()


def test_ohvis_completion_alignment_rejects_unrelated_old_answer():
    goal_id = "0a173409-0917-4c17-9c17-000000000002"
    instruction = f"목표 {goal_id} 등록 후 PRD를 저장하고 마일스톤을 진행해"

    wrong = chat_service._assess_auto_reaction_alignment(
        instruction,
        "브라우저 테스트에서 새 탭이 반복 생성된 원인을 수정했습니다.",
    )
    correct = chat_service._assess_auto_reaction_alignment(
        instruction,
        f"목표 {goal_id}에 PRD를 등록했고 마일스톤 11개를 생성했습니다.",
    )

    assert wrong["aligned"] is False
    assert correct["aligned"] is True
    assert goal_id in correct["matched_identifiers"]


class _DeferredRowStore:
    """chat_deferred_reactions 한 행의 claim/반환/완료 전이를 SQL 의미대로 흉내 낸다."""

    def __init__(self, ohvis_task_id):
        self.id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.ohvis_task_id = ohvis_task_id
        self.status = "pending"
        self.attempts = 0
        self.claimed_by = None
        self.error_message = None

    def pool(self):
        store = self

        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            def transaction(self):
                return _Tx()

            async def execute(self, query, *args):
                if "attempts >= 8" in query:
                    if store.status == "pending" and store.attempts >= 8:
                        store.status = "failed"
                elif "attempts = GREATEST(attempts - 1" in query:
                    guard = query.split("WHERE", 1)[1]
                    if "attempts = $2" in guard:
                        allowed = store.status == "claimed" and store.attempts == args[1]
                    else:
                        allowed = store.claimed_by == args[1]
                    if allowed:
                        store.status = "pending"
                        store.attempts = max(store.attempts - 1, 0)
                        store.claimed_by = None
                        store.error_message = args[2] if len(args) > 2 else None
                elif "SET status = 'completed'" in query:
                    store.status = "completed"

            async def fetch(self, query, *args):
                if "candidates" not in query:
                    return []
                if store.status != "pending" or store.attempts >= 8:
                    return []
                store.status = "claimed"
                store.attempts += 1
                store.claimed_by = args[1]
                return [{
                    "id": store.id,
                    "session_id": store.session_id,
                    "system_message": "자동 보고 본문",
                    "ohvis_task_id": store.ohvis_task_id,
                    "attempts": store.attempts,
                    "runner_job_id": None,
                    "dedupe_key": None,
                }]

            async def fetchval(self, *_a):
                return None

        class _StorePool:
            def acquire(self):
                return _Conn()

        return _StorePool()


async def _drain_background_tasks():
    import asyncio

    for _ in range(3):
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not others:
            return
        await asyncio.gather(*others, return_exceptions=True)


@pytest.mark.asyncio
async def test_deferred_row_survives_repeated_model_lookup_failures_and_completes_ohvis_later():
    store = _DeferredRowStore(ohvis_task_id="ohvis-task-7")
    lookup_failures = 12  # 재시도 예산(8)보다 많이 실패해도 failed 로 가면 안 된다
    consume = AsyncMock(
        side_effect=[chat_service._AutoReactionModelLookupError("db down")] * lookup_failures
        + [None]
    )
    complete_task = AsyncMock()
    seen_states = []

    with (
        patch("app.services.chat_service._is_local_active_api_slot", return_value=True),
        patch("app.services.chat_service.get_pool", side_effect=lambda: store.pool()),
        patch("app.services.chat_service._find_stale_approval_job_status", new=AsyncMock(return_value=None)),
        patch("app.services.chat_service._session_has_live_execution", new=AsyncMock(return_value=False)),
        patch("app.services.chat_service._consume_internal_reaction_stream", new=consume),
        patch("app.services.ohvis_task_manager.complete_task", new=complete_task),
    ):
        for _ in range(lookup_failures):
            assert await chat_service._process_deferred_reactions_once() == 1
            await _drain_background_tasks()
            seen_states.append((store.status, store.attempts))
            # 모델 조회만 실패했으므로 OHVIS 태스크는 아직 끝내지 않는다.
            complete_task.assert_not_awaited()

        # 모델을 부르지 못한 시도는 재시도 예산을 쓰지 않는다.
        assert seen_states == [("pending", 0)] * lookup_failures
        assert store.error_message == chat_service._AUTO_REACTION_RETRY_MARKER

        # 조회가 복구되면 같은 행이 다시 배달되고 그때 OHVIS 태스크를 마무리한다.
        assert await chat_service._process_deferred_reactions_once() == 1
        await _drain_background_tasks()

    assert consume.await_count == lookup_failures + 1
    assert store.status == "completed"
    complete_task.assert_awaited_once()
    assert complete_task.await_args.args[0] == "ohvis-task-7"


@pytest.mark.asyncio
async def test_deferred_retry_return_is_skipped_when_row_was_reclaimed_meanwhile():
    store = _DeferredRowStore(ohvis_task_id=None)
    consume = AsyncMock(side_effect=chat_service._AutoReactionModelLookupError("db down"))

    async def reclaim_by_other_instance(*_args, **_kwargs):
        store.claimed_by = "other-instance"
        store.attempts += 1
        raise chat_service._AutoReactionModelLookupError("db down")

    consume.side_effect = reclaim_by_other_instance

    with (
        patch("app.services.chat_service._is_local_active_api_slot", return_value=True),
        patch("app.services.chat_service.get_pool", side_effect=lambda: store.pool()),
        patch("app.services.chat_service._find_stale_approval_job_status", new=AsyncMock(return_value=None)),
        patch("app.services.chat_service._session_has_live_execution", new=AsyncMock(return_value=False)),
        patch("app.services.chat_service._consume_internal_reaction_stream", new=consume),
    ):
        assert await chat_service._process_deferred_reactions_once() == 1
        await _drain_background_tasks()

    # 남의 claim 을 건드려 attempts 를 깎거나 pending 으로 되돌리지 않는다.
    assert (store.status, store.attempts, store.claimed_by) == ("claimed", 2, "other-instance")
