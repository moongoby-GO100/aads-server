"""담당 세션 조회가 마일스톤을 복제하지 않는지 검증한다.

2026-09-29 실측. `dispatch_pending_milestones` 의 후보 조회가 평범한
`LEFT JOIN chat_sessions ... ON cs.role_key = m.owner_role_key` 였다.
같은 role_key 를 쓰는 채팅 세션이 스무 개 있으면 마일스톤 한 건이 스무
행으로 복제되고, 조회창(`LIMIT 20`)이 그 복제로 전부 찬다. 실제로 후보
62건 중 **1건**만 사이클마다 반복 조회됐고, 그 1건이 비용 상한에 걸린
목표여서 발송은 14시간 동안 0건이었다(`goal_dispatch_cost_gated` 1,060건
/ 53사이클 = 사이클당 20행, 전부 같은 마일스톤).

세션을 고르는 기준이 없으면 사이클마다 다른 세션에 말을 걸 수도 있으므로
정렬까지 함께 고정한다.
"""
from __future__ import annotations

import inspect
import re

from app.services import goal_dispatch

# 조회창 크기. SQL 에서 직접 읽어 테스트가 상수를 따라가게 한다.
_WINDOW_RE = re.compile(r"ORDER BY\s+m\.sequence_order.*?LIMIT\s+(\d+)", re.S | re.I)


def _dispatch_candidate_sql() -> str:
    """후보 조회 SQL 만 잘라 온다.

    끝을 바깥쪽 `ORDER BY m.sequence_order … LIMIT n` 으로 잡는다.
    LATERAL 안에도 `LIMIT 1` 이 있으므로 첫 LIMIT 으로 끊으면 정작
    검사해야 할 조인이 잘려 나간다.
    """
    source = inspect.getsource(goal_dispatch.dispatch_pending_milestones)
    match = re.search(
        r"SELECT m\.id::text AS milestone_id.*?ORDER BY\s+m\.sequence_order"
        r".*?LIMIT\s+\d+",
        source,
        re.S,
    )
    assert match, "착수 후보 조회 SQL 을 찾지 못했다"
    return match.group(0)


def test_owner_session_join_is_deduplicated() -> None:
    """role_key 로 세션을 찾을 때 반드시 한 건으로 좁힌다."""
    sql = _dispatch_candidate_sql()

    assert not re.search(r"LEFT JOIN\s+chat_sessions", sql, re.I), (
        "chat_sessions 를 직접 LEFT JOIN 하면 같은 role_key 를 쓰는 세션 "
        "수만큼 마일스톤이 복제되고 조회창이 한 건으로 채워진다"
    )

    lateral = re.search(
        r"LEFT JOIN LATERAL\s*\((.*?)\)\s*s ON TRUE", sql, re.S | re.I,
    )
    assert lateral, "담당 세션 조회가 LATERAL 서브쿼리가 아니다"

    body = lateral.group(1)
    assert "chat_sessions" in body, "LATERAL 안에서 세션을 찾지 않는다"
    assert re.search(r"LIMIT\s+1", body, re.I), (
        "세션을 한 건으로 좁히지 않으면 복제가 그대로 남는다"
    )
    assert re.search(r"ORDER BY", body, re.I), (
        "정렬이 없으면 사이클마다 다른 세션이 담당으로 잡힌다"
    )


def test_candidate_window_must_hold_many_milestones() -> None:
    """복제 조인은 조회창을 마일스톤 한 건으로 채운다 — 그 차이를 고정한다."""
    sql = _dispatch_candidate_sql()
    window_match = _WINDOW_RE.search(sql)
    assert window_match, "조회창 크기를 SQL 에서 읽지 못했다"
    window = int(window_match.group(1))

    role_sessions = [f"session-{i}" for i in range(window)]
    milestones = [f"milestone-{i}" for i in range(3)]

    duplicated = [(ms, sess) for ms in milestones for sess in role_sessions]
    deduplicated = [(ms, role_sessions[0]) for ms in milestones]

    # 복제 조인: 조회창 안에 마일스톤이 한 건뿐 — 나머지는 보이지 않는다.
    assert len({row[0] for row in duplicated[:window]}) == 1
    # LATERAL 조인: 후보 전부가 조회창 안에 들어온다.
    assert len({row[0] for row in deduplicated[:window]}) == len(milestones)
