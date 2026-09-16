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


# ── 완료 판정 경로도 같은 정본을 봐야 한다 (2026-09-17) ──────────────────
#
# 위 원칙은 2026-09-15 에 화면·API(`goals.py`)에만 적용됐고 판정 경로
# (`milestone_review.py`)는 그대로 이름 규칙을 쓰고 있었다. 그래서 담당들이
# "마일스톤 검증이 불가하다" 고 올렸다 — 확인 요청이 주도를 건너뛰고 전부
# 대표님께만 갔고, 주도가 CTO 인 #119 는 CTO 가 자기 마일스톤을 자기가
# 판정할 수 있었다.
REVIEW = (REPO / "app" / "services" / "milestone_review.py").read_text()
EXECUTOR = (REPO / "app" / "services" / "tool_executor.py").read_text()


def test_review_lead_reads_the_goal_row_first():
    body = _body(REVIEW)
    block = body.split("async def _lead_session", 1)[1].split("\ndef ", 1)[0]
    assert "g.owner_session_id::text" in block
    idx_owner = block.index("owner_session_id")
    idx_fallback = block.index("LIKE '%Lead'", idx_owner)
    assert idx_owner < idx_fallback


def test_self_confirm_is_not_judged_by_role_name():
    """주도가 'Lead' 로 끝나지 않아도 자기 마일스톤은 자기가 판정 못 한다."""
    body = _body(REVIEW)
    assert 'endswith("Lead")' not in body
    assert "self_confirm" in body
    assert "owner_session" in body


def test_report_is_not_blocked_by_pending():
    """착수 표시가 없다는 이유로 완료 신고를 막으면 원장이 멈춘다."""
    body = _body(REVIEW)
    block = body.split("async def report_done", 1)[1].split("\nasync def ", 1)[0]
    assert 'row["status"] in ("completed", "archived", "failed")' in block
    assert 'not in ("in_progress", "review")' not in block


def test_milestone_tools_resolve_the_bound_session():
    """스키마에 session_id 가 없으므로 컨텍스트에서 풀어야 한다.

    빈 문자열이면 `reported_by` 가 NULL 로 남고 자기확인 차단이 아예
    발동하지 않는다.
    """
    for fn in ("_report_milestone_done", "_confirm_milestone"):
        block = EXECUTOR.split(f"async def {fn}", 1)[1].split("\n    async def ", 1)[0]
        assert "_resolve_bound_chat_session_id" in block, fn
        assert 'str(inp.get("session_id") or "")' not in block, fn
