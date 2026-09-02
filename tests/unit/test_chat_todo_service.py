from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.chat import ChatTodoItemOut
from app.services import chat_todo_service as svc


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _TodoConn:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    async def execute(self, query: str, *args):
        normalized = " ".join(query.split())
        if normalized.startswith("DELETE FROM chat_todo_items"):
            session_id, target_id, source = args
            target_key = str(target_id)
            self.items = {
                key: value
                for key, value in self.items.items()
                if not (
                    str(value["session_id"]) == str(session_id)
                    and value["source"] == source
                    and (
                        str(value.get("execution_id")) == target_key
                        or str(value.get("message_id")) == target_key
                    )
                )
            }
            return "DELETE"
        raise AssertionError(f"unexpected execute query: {normalized}")

    async def fetchrow(self, query: str, *args):
        normalized = " ".join(query.split())
        if normalized.startswith("INSERT INTO chat_todo_items"):
            (
                session_id,
                message_id,
                execution_id,
                title,
                status,
                sort_order,
                source,
                metadata_json,
            ) = args
            todo_id = uuid.uuid4()
            now = _utcnow()
            row = {
                "id": todo_id,
                "session_id": session_id,
                "message_id": message_id,
                "execution_id": execution_id,
                "title": title,
                "status": status,
                "sort_order": sort_order,
                "source": source,
                "metadata": json.loads(metadata_json),
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self.items[str(todo_id)] = row
            return row
        if normalized == "SELECT * FROM chat_todo_items WHERE id = $1":
            return self.items.get(str(args[0]))
        if normalized == "SELECT * FROM chat_todo_items WHERE id = $1 AND session_id = $2":
            row = self.items.get(str(args[0]))
            if row and str(row["session_id"]) == str(args[1]):
                return row
            return None
        if normalized.startswith("UPDATE chat_todo_items SET title = $2"):
            todo_id, title, status, metadata_json, completed_at = args
            row = self.items[str(todo_id)]
            row.update({
                "title": title,
                "status": status,
                "metadata": json.loads(metadata_json),
                "updated_at": _utcnow(),
                "completed_at": completed_at,
            })
            return row
        if normalized.startswith("DELETE FROM chat_todo_items WHERE id = $1 AND session_id = $2"):
            todo_id, session_id = args
            row = self.items.get(str(todo_id))
            if row and str(row["session_id"]) == str(session_id):
                return self.items.pop(str(todo_id))
            return None
        raise AssertionError(f"unexpected fetchrow query: {normalized}")

    async def fetch(self, query: str, *args):
        normalized = " ".join(query.split())
        if normalized.startswith("DELETE FROM chat_todo_items WHERE session_id = $1"):
            session_id, statuses = args
            deleted = []
            for key, row in list(self.items.items()):
                if str(row["session_id"]) == str(session_id) and row["status"] in set(statuses):
                    deleted.append({"id": row["id"]})
                    self.items.pop(key)
            return deleted
        if normalized.startswith("UPDATE chat_todo_items SET status = $3") and "make_interval" in normalized:
            session_id, from_status, to_status, stale_minutes = args
            cutoff = _utcnow() - timedelta(minutes=int(stale_minutes))
            rows = []
            for row in self.items.values():
                if (
                    str(row["session_id"]) == str(session_id)
                    and row["status"] == from_status
                    and row["updated_at"] < cutoff
                ):
                    row["status"] = to_status
                    row["metadata"] = {
                        **row.get("metadata", {}),
                        "stale_reset_reason": "in_progress_timeout",
                    }
                    row["updated_at"] = _utcnow()
                    row["completed_at"] = None
                    rows.append(dict(row))
            return rows
        if normalized.startswith("UPDATE chat_todo_items SET status = $3"):
            session_id, from_status, to_status = args
            rows = []
            for row in self.items.values():
                if str(row["session_id"]) == str(session_id) and row["status"] == from_status:
                    row["status"] = to_status
                    row["metadata"] = {
                        **row.get("metadata", {}),
                        "retried_from": from_status,
                    }
                    row["updated_at"] = _utcnow()
                    row["completed_at"] = None
                    rows.append(dict(row))
            return rows
        if "FROM chat_todo_items" not in normalized:
            raise AssertionError(f"unexpected fetch query: {normalized}")

        session_id = str(args[0])
        execution_id = None
        message_id = None
        statuses = None
        arg_index = 1
        if "execution_id = $2" in normalized:
            execution_id = str(args[arg_index])
            arg_index += 1
        if f"message_id = ${arg_index + 1}" in normalized or "message_id = $3" in normalized:
            message_id = str(args[arg_index])
            arg_index += 1
        if "status = ANY" in normalized:
            statuses = set(args[arg_index])
            arg_index += 1
        max_items = None
        if " LIMIT $" in normalized:
            max_items = int(args[-1])

        rows = []
        for row in self.items.values():
            if str(row["session_id"]) != session_id:
                continue
            if execution_id and str(row.get("execution_id")) != execution_id:
                continue
            if message_id and str(row.get("message_id")) != message_id:
                continue
            if statuses and row["status"] not in statuses:
                continue
            rows.append(dict(row))

        rows.sort(key=lambda item: (0 if item["status"] in svc.TODO_ACTIVE_STATUSES else 1, item["sort_order"]))
        if max_items is not None:
            rows = rows[:max_items]
        return rows


@pytest.mark.asyncio
async def test_todo_service_crud_state_transition_and_session_isolation():
    conn = _TodoConn()
    session_a = str(uuid.uuid4())
    session_b = str(uuid.uuid4())
    execution_a = str(uuid.uuid4())
    execution_b = str(uuid.uuid4())

    created_a = await svc.create_todo_items(
        session_id=session_a,
        message_id=str(uuid.uuid4()),
        execution_id=execution_a,
        titles=["migration 추가", "chat_service 통합"],
        source="user_turn",
        metadata={"requires_tool": True},
        conn=conn,
    )
    created_b = await svc.create_todo_items(
        session_id=session_b,
        message_id=str(uuid.uuid4()),
        execution_id=execution_b,
        titles=["session B 점검"],
        source="user_turn",
        metadata={"requires_tool": True},
        conn=conn,
    )

    open_a = await svc.list_todo_items(session_id=session_a, include_completed=False, conn=conn)
    assert [item["status"] for item in open_a] == [svc.TODO_STATUS_IN_PROGRESS, svc.TODO_STATUS_PENDING]
    assert [item["title"] for item in open_a] == ["migration 추가", "chat_service 통합"]

    completed = await svc.mark_complete(
        str(created_a[0]["id"]),
        metadata={"phase": "save"},
        conn=conn,
    )
    failed = await svc.mark_failed(
        str(created_a[1]["id"]),
        reason="completion_gate_missing",
        conn=conn,
    )

    listed_a = await svc.list_todo_items(session_id=session_a, conn=conn)
    listed_b = await svc.list_todo_items(session_id=session_b, conn=conn)

    assert completed["status"] == svc.TODO_STATUS_COMPLETED
    assert completed["completed_at"] is not None
    assert completed["metadata"]["audit"][-1]["action"] == "update"
    assert failed["status"] == svc.TODO_STATUS_FAILED
    assert failed["metadata"]["failure_reason"] == "completion_gate_missing"
    assert len(listed_a) == 2
    assert len(listed_b) == 1
    assert listed_b[0]["title"] == "session B 점검"
    assert listed_b[0]["id"] == created_b[0]["id"]


@pytest.mark.asyncio
async def test_todo_rows_decode_jsonb_metadata_strings_for_api_response():
    conn = _TodoConn()
    session_id = str(uuid.uuid4())
    created = await svc.create_todo_items(
        session_id=session_id,
        message_id=str(uuid.uuid4()),
        execution_id=str(uuid.uuid4()),
        titles=["API response validation"],
        source="user_turn",
        metadata={"created_from": "test"},
        conn=conn,
    )
    conn.items[str(created[0]["id"])]["metadata"] = json.dumps({"created_from": "asyncpg"}, ensure_ascii=False)

    listed = await svc.list_todo_items(session_id=session_id, conn=conn)

    assert listed[0]["metadata"] == {"created_from": "asyncpg"}
    ChatTodoItemOut.model_validate(listed[0])


@pytest.mark.asyncio
async def test_list_todo_items_caps_response_size():
    conn = _TodoConn()
    session_id = str(uuid.uuid4())
    await svc.create_todo_items(
        session_id=session_id,
        message_id=str(uuid.uuid4()),
        execution_id=str(uuid.uuid4()),
        titles=["첫 항목", "둘째 항목", "셋째 항목"],
        source="user_turn",
        conn=conn,
    )

    listed = await svc.list_todo_items(session_id=session_id, max_items=2, conn=conn)

    assert [item["title"] for item in listed] == ["첫 항목", "둘째 항목"]


@pytest.mark.asyncio
async def test_cleanup_stale_in_progress_resets_and_promotes_next_item():
    conn = _TodoConn()
    session_id = str(uuid.uuid4())
    created = await svc.create_todo_items(
        session_id=session_id,
        message_id=str(uuid.uuid4()),
        execution_id=str(uuid.uuid4()),
        titles=["오래된 진행 항목", "다음 항목"],
        source="user_turn",
        conn=conn,
    )
    conn.items[str(created[0]["id"])]["updated_at"] = _utcnow() - timedelta(minutes=180)

    cleaned = await svc.cleanup_stale_in_progress_todos(
        session_id=session_id,
        stale_after_minutes=120,
        conn=conn,
    )

    listed = await svc.list_todo_items(session_id=session_id, include_completed=False, conn=conn)
    assert len(cleaned) >= 2
    assert listed[0]["title"] == "오래된 진행 항목"
    assert listed[0]["status"] == svc.TODO_STATUS_PENDING
    assert listed[0]["metadata"]["stale_reset_reason"] == "in_progress_timeout"
    assert listed[1]["title"] == "다음 항목"
    assert listed[1]["status"] == svc.TODO_STATUS_IN_PROGRESS
    assert listed[1]["metadata"]["stale_cleanup_promoted"] is True


@pytest.mark.asyncio
async def test_user_actions_update_delete_clear_and_retry_by_session_scope():
    conn = _TodoConn()
    session_a = str(uuid.uuid4())
    session_b = str(uuid.uuid4())
    created_a = await svc.create_todo_items(
        session_id=session_a,
        message_id=str(uuid.uuid4()),
        execution_id=str(uuid.uuid4()),
        titles=["대기 항목", "실패 항목", "완료 항목"],
        source="user_turn",
        conn=conn,
    )
    await svc.create_todo_items(
        session_id=session_b,
        message_id=str(uuid.uuid4()),
        execution_id=str(uuid.uuid4()),
        titles=["다른 세션 항목"],
        source="user_turn",
        conn=conn,
    )
    await svc.mark_failed(str(created_a[1]["id"]), conn=conn)
    await svc.mark_complete(str(created_a[2]["id"]), conn=conn)

    skipped = await svc.update_session_todo_item(
        session_id=session_a,
        todo_id=str(created_a[0]["id"]),
        status=svc.TODO_STATUS_SKIPPED,
        metadata={"reason": "manual_hide"},
        conn=conn,
    )
    assert skipped["status"] == svc.TODO_STATUS_SKIPPED
    assert skipped["metadata"]["reason"] == "manual_hide"

    cross_session = await svc.update_session_todo_item(
        session_id=session_b,
        todo_id=str(created_a[0]["id"]),
        status=svc.TODO_STATUS_PENDING,
        conn=conn,
    )
    assert cross_session is None

    retried = await svc.retry_failed_session_todos(session_id=session_a, conn=conn)
    assert retried == 1
    listed = await svc.list_todo_items(session_id=session_a, conn=conn)
    assert any(
        item["title"] == "실패 항목"
        and item["status"] in {svc.TODO_STATUS_PENDING, svc.TODO_STATUS_IN_PROGRESS}
        for item in listed
    )

    deleted = await svc.delete_session_todo_item(
        session_id=session_a,
        todo_id=str(created_a[2]["id"]),
        conn=conn,
    )
    assert deleted["title"] == "완료 항목"

    cleared = await svc.clear_session_todos(
        session_id=session_a,
        statuses=[svc.TODO_STATUS_SKIPPED],
        conn=conn,
    )
    assert cleared == 1
    remaining_a = await svc.list_todo_items(session_id=session_a, conn=conn)
    remaining_b = await svc.list_todo_items(session_id=session_b, conn=conn)
    assert {item["title"] for item in remaining_a} == {"실패 항목"}
    assert {item["title"] for item in remaining_b} == {"다른 세션 항목"}


def test_completion_gate_detects_missing_items():
    todo_items = [
        {
            "id": uuid.uuid4(),
            "title": "migration 추가",
            "metadata": {"match_terms": ["migration", "추가"]},
        },
        {
            "id": uuid.uuid4(),
            "title": "chat_service 통합",
            "metadata": {"match_terms": ["chat_service", "통합"]},
        },
    ]

    result = svc.evaluate_todo_completion(
        todo_items,
        response_text="migration 추가 완료했습니다.",
        tools_called=[{"type": "tool_use", "tool_name": "run_remote_command"}],
    )

    assert len(result["completed_ids"]) == 1
    assert result["missing_titles"] == ["chat_service 통합"]
    assert result["all_completed"] is False


def test_todo_prompt_exposes_ids_and_tool_write_rule():
    todo_id = uuid.uuid4()

    prompt = svc.build_todo_prompt_block([
        {
            "id": todo_id,
            "title": "실제 작업 리스트 제목 관리",
            "status": svc.TODO_STATUS_IN_PROGRESS,
        }
    ])

    assert "todo_write 도구로 TODO 상태를 명시적으로 갱신" in prompt
    assert f"todo_id={todo_id}" in prompt
    assert "[in_progress] 실제 작업 리스트 제목 관리" in prompt


def test_simple_request_does_not_create_todos():
    assert not svc.should_create_todos("안녕", intent="greeting", use_tools=False)
    assert not svc.should_create_todos("AADS가 뭐야?", intent="casual", use_tools=False)
    assert not svc.should_create_todos("todo리스트 구현되었어?", intent="code_modify", use_tools=True)
    assert svc.extract_todo_titles("안녕", intent="greeting", use_tools=False) == []


def test_extract_todo_titles_uses_pm_style_action_titles():
    titles = svc.extract_todo_titles(
        "다음단계로 PM식 작성으로 개선 진행하고 보고해",
        intent="code_modify",
        use_tools=True,
    )

    assert titles == ["PM식 TODO 작성 기준 개선 및 결과 보고"]
    assert all(not title.endswith(("해줘", "보고해", "진행해")) for title in titles)


def test_extract_todo_titles_splits_pm_actions_and_completion_intent():
    titles = svc.extract_todo_titles(
        "1. 브레인스토밍 페이지 확인해 2. 버블 내용 저장 오류 수정하고 검증해",
        intent="code_modify",
        use_tools=True,
    )

    assert titles == [
        "브레인스토밍 페이지 확인",
        "버블 내용 저장 오류 수정 및 검증",
    ]


def test_chat_todo_migration_contains_required_schema():
    content = Path("migrations/083_chat_todo_items.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS chat_todo_items" in content
    assert "execution_id UUID" in content
    assert "status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')" in content
    assert "idx_chat_todo_items_session_status_sort" in content
