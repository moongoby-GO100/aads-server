"""이미 받아 둔 승인·목표 설정이면 다음 단계 카드를 올리지 않는다.

## 왜 필요한가

2026-09-17 대표님 지적 — "승인 팝업이 목표 진행시에도 계속 뜨는듯한데
목표 승인설정이 되면 자동승인 범위안에서는 자동으로 진행되어야 하는거
아닌가".

두 곳이 어긋나 있었다.

1. 제안 카드의 범위 조회가 `mission`·`goal` 두 개만 보고 있었다. 그 사이
   실행 게이트(`live_trading_guard.is_approved`)에는 `session`·`project`
   가 붙었다. 그래서 "이 프로젝트 전체 100회" 를 눌러 두셔도 다음 단계는
   계속 물었다 — 24시간 next_step 카드 197장(실측).
2. `goals.approval_policy`(목표 승인 설정)를 제안 카드가 아예 보지
   않았다. 목표를 진행하는 내내 "이걸 할까요" 가 떴다.

이 파일이 지키는 것.

- 범위 목록은 실행 게이트와 **같이** 움직인다. 한쪽만 늘리면 그 범위는
  실행만 통과하고 카드는 계속 뜬다.
- 목표 설정은 **읽기만** 한다. 여기서 횟수를 세면 한 번의 일에 두 번
  깎인다 — 소비는 실제 실행 시점의 게이트가 센다.
- 상한이 없으면 설정이 아니라 게이트 해제다. 남은 횟수 조건이 조회에서
  빠지지 않는다.
"""
from __future__ import annotations

import asyncio

from app.services.next_step_proposals import (
    _covered_by_existing_grant,
    _goal_policy_covers,
    _session_project,
)


class _Conn:
    """fetchrow/fetchval 만 흉내 내는 가짜 커넥션."""

    def __init__(self, row=None, val=None, raises: BaseException | None = None):
        self.row = row
        self.val = val
        self.raises = raises
        self.sql = ""
        self.args: tuple = ()
        self.executed: list = []

    async def fetchrow(self, sql, *args):
        self.sql = sql
        self.args = args
        if self.raises is not None:
            raise self.raises
        return self.row

    async def fetchval(self, sql, *args):
        self.sql = sql
        self.args = args
        if self.raises is not None:
            raise self.raises
        return self.val

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


# ─── 승인 범위 ────────────────────────────────────────────────────────


def test_네_범위를_모두_본다():
    """session·project 가 빠지면 대표님이 눌러 둔 승인이 무시된다."""
    conn = _Conn(row={"id": "grant-1"})
    asyncio.run(_covered_by_existing_grant(conn, "sess-1", "run_remote_command", "AADS"))
    sql = conn.sql
    for scope in ("'goal'", "'mission', 'session'", "'project'"):
        assert scope in sql, f"{scope} 범위가 조회에서 빠졌다"


def test_프로젝트를_모르면_project_범위로_통과하지_않는다():
    """무엇을 여는지 모르는 채로 여는 것이 가장 나쁘다."""
    conn = _Conn(row=None)
    asyncio.run(_covered_by_existing_grant(conn, "sess-1", "tool", ""))
    assert conn.args[2] == ""
    assert "$3 <> ''" in conn.sql


def test_프로젝트_키는_대문자로_맞춘다():
    conn = _Conn(row=None)
    asyncio.run(_covered_by_existing_grant(conn, "sess-1", "tool", "aads"))
    assert conn.args[2] == "AADS"


def test_도구가_없어도_조회한다():
    """제안에 도구가 안 적혀 있다고 승인 범위를 안 볼 이유는 없다.

    예전에는 tool 이 비면 즉시 None 이라, 도구를 특정하지 않은 제안은
    범위 승인이 있어도 항상 카드가 됐다."""
    conn = _Conn(row={"id": "grant-2"})
    got = asyncio.run(_covered_by_existing_grant(conn, "sess-1", "", "AADS"))
    assert got == "grant-2"
    # 도구가 비어도 'next_step' 은 남는다 — 제안 카드에 눌러 두신 승인이
    # 그 자리다. 빈 문자열이 섞이면 action_type = '' 에 걸려 아무것도 안 맞는다.
    assert conn.args[1] == ["next_step"]


def test_도구_승인과_제안_승인을_모두_본다():
    """대표님이 제안 카드에 '이 프로젝트 전체' 를 누르면 그 승인의
    action_type 은 도구 이름이 아니라 next_step 이다."""
    conn = _Conn(row=None)
    asyncio.run(_covered_by_existing_grant(conn, "sess-1", "run_remote_command", "AADS"))
    assert conn.args[1] == ["run_remote_command", "next_step"]
    assert "ANY($2::text[])" in conn.sql


def test_남은_횟수가_없으면_덮지_않는다():
    conn = _Conn(row=None)
    asyncio.run(_covered_by_existing_grant(conn, "sess-1", "tool", "AADS"))
    assert "< COALESCE(r.max_executions, 1)" in conn.sql


def test_조회_실패는_카드로_떨어진다():
    """묻는 쪽이 잘못 통과시키는 쪽보다 낫다."""
    conn = _Conn(raises=RuntimeError("relation does not exist"))
    assert asyncio.run(_covered_by_existing_grant(conn, "s", "t", "AADS")) is None


# ─── 목표 승인 설정 ───────────────────────────────────────────────────


def test_목표_설정이_덮으면_목표_id_를_준다():
    conn = _Conn(row={"id": "goal-1", "title": "AAG"})
    got = asyncio.run(_goal_policy_covers(conn, "sess-1", "high"))
    assert got == "goal-1"


def test_위험도에_따라_보는_필드가_다르다():
    conn = _Conn(row=None)
    asyncio.run(_goal_policy_covers(conn, "sess-1", "high"))
    assert conn.args[1] == "auto_approve_high"
    asyncio.run(_goal_policy_covers(conn, "sess-1", "critical"))
    assert conn.args[1] == "auto_approve_critical"


def test_목표_설정은_횟수_상한을_본다():
    conn = _Conn(row=None)
    asyncio.run(_goal_policy_covers(conn, "sess-1", "high"))
    assert "max_executions" in conn.sql and "'used'" in conn.sql


def test_끝난_목표는_덮지_않는다():
    conn = _Conn(row=None)
    asyncio.run(_goal_policy_covers(conn, "sess-1", "high"))
    assert "g.status IN ('draft', 'active', 'blocked')" in conn.sql
    assert "link_state" in conn.sql


def test_목표_설정_조회는_횟수를_세지_않는다():
    """소비는 실제 실행 시점의 게이트가 센다. 여기서 세면 두 번 깎인다."""
    conn = _Conn(row={"id": "goal-1", "title": "AAG"})
    asyncio.run(_goal_policy_covers(conn, "sess-1", "high"))
    assert conn.executed == []


def test_세션을_모르면_목표_설정을_보지_않는다():
    conn = _Conn(row={"id": "goal-1", "title": "AAG"})
    assert asyncio.run(_goal_policy_covers(conn, "", "high")) is None


# ─── 세션 프로젝트 ────────────────────────────────────────────────────


def test_세션_프로젝트를_읽는다():
    conn = _Conn(val="AADS")
    assert asyncio.run(_session_project(conn, "sess-1")) == "AADS"


def test_프로젝트를_못_읽으면_빈_문자열():
    """None 을 그대로 주면 project 범위 조회에서 잘못 매칭된다."""
    assert asyncio.run(_session_project(_Conn(val=None), "sess-1")) == ""
    conn = _Conn(raises=RuntimeError("no such column"))
    assert asyncio.run(_session_project(conn, "sess-1")) == ""
