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
        raise AssertionError(f"unexpected fetchrow query: {normalized}")

    async def fetch(self, query: str, *args):
        normalized = " ".join(query.split())
        if normalized.startswith("UPDATE chat_todo_items SET status = $3"):
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
            statuses = set(args[-1])

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
    assert listed[0]["status"] == svc.TODO_STATUS_IN_PROGRESS
    assert listed[0]["metadata"]["stale_cleanup_promoted"] is True


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


def test_simple_request_does_not_create_todos():
    assert not svc.should_create_todos("안녕", intent="greeting", use_tools=False)
    assert not svc.should_create_todos("AADS가 뭐야?", intent="casual", use_tools=False)
    assert not svc.should_create_todos("todo리스트 구현되었어?", intent="code_modify", use_tools=True)
    assert svc.extract_todo_titles("안녕", intent="greeting", use_tools=False) == []


def test_chat_todo_migration_contains_required_schema():
    content = Path("migrations/083_chat_todo_items.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS chat_todo_items" in content
    assert "execution_id UUID" in content
    assert "status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')" in content
    assert "idx_chat_todo_items_session_status_sort" in content
