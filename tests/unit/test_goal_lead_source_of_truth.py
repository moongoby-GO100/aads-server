"""목표의 주도는 **목표에 적힌 것**이 정본이다.

2026-09-15 대표님 지적 — "#119 상한가따라잡기 전략관리자가 주도인데 담당으로
들어가 있어서 변경이 안된다".

두 가지가 겹쳤다.

1. 화면이 주도를 **역할 키가 "Lead" 로 끝나는지**로 판단했다. #119 의 주도는
   `CTO` 역할키를 쓰는 세션이라 담당으로만 보였다. DB 의
   `goals.owner_session_id` 에는 제대로 적혀 있었다.
2. 주도는 **붙일 때(`as_lead`)만** 정할 수 있었고 나중에 바꿀 길이 없었다.
"""
import inspect
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SOURCE = (REPO / "app" / "routers" / "goals.py").read_text()


def _body(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def test_lead_comes_from_the_goal_row_not_the_role_name():
    body = _body(SOURCE)
    # 이름 규칙만으로 판단하던 옛 코드가 남아 있으면 안 된다.
    assert 'r["role_key"] or "").endswith("Lead"),' not in body
    assert "_goal_lead_session" in body
    assert "_goal_lead_role" in body


def test_goal_query_reads_the_owner_columns():
    body = _body(SOURCE)
    assert "owner_session_id::text" in body
    assert "COALESCE(owner_role_key, '') AS owner_role_key" in body


def test_name_convention_is_only_the_last_resort():
    """목표에 주도가 안 적혀 있을 때만 이름 규칙을 쓴다."""
    body = _body(SOURCE)
    idx_session = body.index("_goal_lead_session")
    idx_fallback = body.index('endswith("Lead")', idx_session)
    assert idx_session < idx_fallback


def test_lead_can_be_changed_later():
    assert '@router.post("/goals/{goal_id}/lead")' in SOURCE
    assert "def set_goal_lead" in SOURCE


def test_lead_must_already_be_attached_to_the_goal():
    """붙어 있지 않은 세션을 주도로 세우면 목표 현황에 나오지 않는다."""
    body = _body(SOURCE)
    block = body.split("def set_goal_lead", 1)[1].split("@router", 1)[0]
    assert "goal_task_links" in block
    assert "409" in block
