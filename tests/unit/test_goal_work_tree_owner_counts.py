"""업무 트리 응답의 effective_owner 폴백 순서와 하위 집계."""
from __future__ import annotations

from app.services.goal_work_hierarchy import (
    attach_work_aggregates,
    resolve_effective_owner,
)

ASSIGNED = {"a1": {"session_id": "s-a", "session_title": "배정", "role_key": "dev"}}
MS_OWNER = {"session_id": "s-m", "session_title": "마일스톤담당", "role_key": "ops"}
LEAD = {"session_id": "s-l", "session_title": "주도", "role_key": "lead"}


def test_effective_owner_fallback_order():
    item = {"assignment_id": "a1"}
    assert resolve_effective_owner(item, ASSIGNED, MS_OWNER, LEAD) == {
        "source": "assigned", "session_id": "s-a", "session_title": "배정", "role_key": "dev",
    }
    got = resolve_effective_owner({"assignment_id": None}, ASSIGNED, MS_OWNER, LEAD)
    assert got["source"] == "milestone_owner" and got["session_id"] == "s-m"
    got = resolve_effective_owner({"assignment_id": None}, ASSIGNED, None, LEAD)
    assert got["source"] == "goal_lead" and got["role_key"] == "lead"
    got = resolve_effective_owner({"assignment_id": None}, ASSIGNED, {"session_id": None}, None)
    assert got == {"source": "none", "session_id": None, "session_title": None, "role_key": None}


def _item(iid, typ, status, parent=None, milestone="m1"):
    return {"id": iid, "type": typ, "status": status, "parent_id": parent,
            "milestone_id": milestone, "children": []}


def _tree(specs):
    items = {s["id"]: s for s in specs}
    for s in specs:
        if s["parent_id"]:
            items[s["parent_id"]]["children"].append(s)
    return items


def test_task_counts_and_child_status_counts():
    items = _tree([
        _item("e1", "epic", "in_progress"),
        _item("s1", "story", "in_progress", "e1"),
        _item("s2", "story", "draft", "e1"),
        _item("t1", "task", "completed", "s1"),
        _item("t2", "task", "in_progress", "s1"),
        _item("t3", "task", "blocked", "s2"),
        _item("t4", "task", "in_review", "s2"),
        _item("t5", "task", "cancelled", "s2"),
    ])
    summaries = attach_work_aggregates(items, ["m1", "m2"])

    epic = items["e1"]
    assert epic["child_status_counts"]["in_progress"] == 1
    assert epic["child_status_counts"]["draft"] == 1
    assert epic["task_counts"] == {
        "total": 5, "completed": 1, "in_progress": 1, "blocked": 1, "draft": 0,
        "ready": 0, "in_review": 1, "cancelled": 1,
    }
    assert items["s1"]["task_counts"]["total"] == 2
    assert items["s1"]["child_status_counts"]["completed"] == 1
    assert items["s2"]["task_counts"]["blocked"] == 1
    assert "task_counts" not in items["t1"]

    assert summaries["m1"]["epic"] == {"total": 1, "completed": 0, "in_progress": 1, "blocked": 0}
    assert summaries["m1"]["story"] == {"total": 2, "completed": 0, "in_progress": 1, "blocked": 0}
    assert summaries["m1"]["task"] == {"total": 5, "completed": 1, "in_progress": 1, "blocked": 1}
    assert summaries["m2"]["task"]["total"] == 0


def test_summary_is_split_per_milestone():
    items = _tree([
        _item("e1", "epic", "completed", milestone="m1"),
        _item("e2", "epic", "blocked", milestone="m2"),
    ])
    summaries = attach_work_aggregates(items, ["m1", "m2"])
    assert summaries["m1"]["epic"]["completed"] == 1
    assert summaries["m2"]["epic"]["blocked"] == 1


class _FakeConn:
    def __init__(self, work_rows, milestone_rows):
        self.work_rows = work_rows
        self.milestone_rows = milestone_rows

    async def fetchrow(self, sql, *args):
        if "JOIN chat_sessions" in sql:
            return None
        return {"id": "g1", "tenant_id": "t1", "project": "AADS", "title": "목표",
                "status": "active", "version": 1}

    async def fetch(self, sql, *args):
        if "FROM work_items w" in sql:
            return self.work_rows
        if "FROM milestones m" in sql:
            return self.milestone_rows
        return []


def _work_row(iid, typ, status, parent=None):
    return {"id": iid, "type": typ, "status": status, "parent_id": parent,
            "milestone_id": "m1", "assignment_id": None}


def test_get_goal_tree_aggregates_after_children_are_linked():
    import asyncio

    from app.services.goal_work_hierarchy import get_goal_tree

    conn = _FakeConn(
        [
            _work_row("e1", "epic", "in_progress"),
            _work_row("s1", "story", "in_progress", "e1"),
            _work_row("t1", "task", "completed", "s1"),
            _work_row("t2", "task", "in_progress", "s1"),
            _work_row("t3", "task", "blocked", "s1"),
        ],
        [{"milestone_id": "m1", "title": "단계", "sequence_order": 1, "status": "active",
          "session_id": None, "session_title": None, "role_key": None}],
    )
    tree = asyncio.run(get_goal_tree(
        conn, tenant_id="t1", goal_id="g1", includes=(), include_history=False,
    ))

    assert [r["id"] for r in tree["items"]] == ["e1"]
    epic = tree["items"][0]
    assert epic["task_counts"] == {
        "total": 3, "completed": 1, "in_progress": 1, "blocked": 1, "draft": 0,
        "ready": 0, "in_review": 0, "cancelled": 0,
    }
    story = epic["children"][0]
    assert story["id"] == "s1"
    assert sum(story["child_status_counts"].values()) == 3
    assert story["task_counts"]["total"] == 3
    assert tree["milestones"][0]["work_summary"]["task"]["total"] == 3
