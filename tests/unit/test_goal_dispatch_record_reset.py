"""소유권(claim)이 발송을 막고, 기록을 지우는 쪽은 자취를 남긴다.

2026-09-17 22:28~22:33Z 실측(GO100 #119). 마일스톤 05fdc2bb·d195f3d2 가
세 사이클 연속 `attempt=1` 로 나가 담당 세션 둘이 같은 착수 지시를 2회씩
받았다. 최종 DB 는 `dispatch_count=1` — **발송 2회, 카운트 1회**다.

두 가지가 같이 필요하다.

1. **봉쇄.** 조회와 발송 사이에 누가 기록을 지웠든, 발송 직전 조건부
   UPDATE 가 빈손으로 돌아오면 보내지 않는다. 원인을 몰라도 막힌다.
2. **추적.** 봉쇄는 증상을 막을 뿐 **누가 지웠는지**는 알려주지 않는다.
   기록을 되돌리는 경로가 전부 `goal_dispatch_record_reset` 을 남겨야
   다음번엔 로그만으로 지운 주체를 지목할 수 있다. 이번엔 그게 없어서
   "남은 유력 경로는 직접 SQL" 까지밖에 못 갔다.

`tests/unit/test_goal_dispatch_claim.py` 는 1번의 **경합 시나리오**를 본다.
여기서는 1번의 **계약**(빈손이면 안 보낸다 / attempt 은 DB 가 준 값이다 /
두 조건식이 같은 상수를 쓴다)과 2번 전체를 본다.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path

import pytest

from app.services import goal_dispatch

_APP = Path(__file__).resolve().parents[2] / "app"


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.events.append((event, kw))

    warning = info
    error = info

    def named(self, event: str) -> list[dict]:
        return [kw for name, kw in self.events if name == event]


def _row(n: int = 1, **over) -> dict:
    row = {
        "milestone_id": f"{n:08d}-0000-0000-0000-000000000000",
        "milestone_title": f"M{n}",
        "description": "설명",
        "completion_criteria": "기준",
        "dispatch_count": 0,
        "dispatched_at": None,
        "load_deferred_since": None,
        "goal_title": "목표",
        "project": "AADS",
        "goal_id": "g",
        "owner_role_key": f"role{n}",
        "goal_lead_session_id": "lead",
        "session_id": f"session-{n}",
    }
    row.update(over)
    return row


class _Conn:
    """조회는 고정 스냅샷, 소유권 UPDATE 만 지정한 값으로 답한다."""

    def __init__(self, rows: list[dict], claim: dict | None) -> None:
        self.rows = rows
        self.claim = claim
        self.claim_queries: list[str] = []

    async def execute(self, *_a, **_k) -> str:
        return "UPDATE 0"

    async def fetch(self, *_a, **_k) -> list[dict]:
        return self.rows

    async def fetchval(self, *_a, **_k):
        return False

    async def fetchrow(self, query: str, *_a):
        self.claim_queries.append(query)
        return self.claim


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()

    async def fetchrow(self, *a, **k):
        return await self.conn.fetchrow(*a, **k)

    async def execute(self, *a, **k):
        return await self.conn.execute(*a, **k)


@pytest.fixture
def cycle(monkeypatch):
    """게이트는 전부 열고, 발송은 띄우지 않고 인자만 받아 둔다."""
    from app.core import db_pool
    from app.services import orchestration_limits

    async def _open(*_a, **_k):
        return (False, "")

    async def _ok(*_a, **_k):
        return (True, "")

    async def _no_repair(_conn):
        return 0

    monkeypatch.setattr(orchestration_limits, "owner_paused", _open)
    monkeypatch.setattr(orchestration_limits, "cost_gate", _ok)
    monkeypatch.setattr(orchestration_limits, "load_gate", _ok)
    monkeypatch.setattr(goal_dispatch, "repair_owner_links", _no_repair)
    monkeypatch.setattr(goal_dispatch, "_MAX_PER_CYCLE", 10)
    monkeypatch.setattr(goal_dispatch, "_ENABLED", True)

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)

    spawned: list[dict] = []
    monkeypatch.setattr(
        goal_dispatch, "_spawn_send", lambda **kw: spawned.append(kw),
    )

    def _run(rows: list[dict], claim: dict | None) -> tuple[dict, _Conn]:
        conn = _Conn(rows, claim)
        monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
        result = asyncio.run(goal_dispatch.dispatch_pending_milestones(None))
        return result, conn

    return _run, log, spawned


def test_an_empty_claim_never_reaches_the_send(cycle) -> None:
    """소유권 UPDATE 가 0행이면 **발송 자체가 일어나지 않는다.**

    이것이 중복 봉쇄의 계약이다. 로그나 카운터가 아니라 `_spawn_send` 가
    안 불리는 것이 전부다 — 불리는 순간 담당 세션에 메시지가 들어간다.
    """
    run, log, spawned = cycle
    result, _conn = run([_row(1)], None)

    assert spawned == [], "소유권을 못 잡았는데 발송 태스크를 띄웠다"
    assert result["sent"] == 0
    # 미룬 것이지 포기한 것이 아니다 — 포기로 세면 재시도 한도가 헛되이 깎인다.
    assert result["skipped"] == 1 and result["gave_up"] == 0

    lost = log.named("goal_dispatch_claim_lost")
    assert len(lost) == 1, f"봉쇄가 걸린 것이 로그에 없다 — {log.events}"
    assert lost[0]["milestone"] == _row(1)["milestone_id"][:8]
    assert lost[0]["session"] == "session-"
    # 이번 사이클이 조회에서 읽은 값. "조회가 0 을 읽었다" 와 "발송 직전엔
    # 값이 있었다" 의 대조가 로그 안에서 끝나야 한다.
    assert lost[0]["read_count"] == 0
    assert "read_dispatched_at" in lost[0]


def test_attempt_is_what_the_database_returned(cycle) -> None:
    """`attempt` 은 조회 스냅샷의 `count + 1` 이 아니라 RETURNING 값이다.

    2026-09-17 에 네 건이 전부 `attempt=1` 이었던 것이 스냅샷을 믿으면
    안 된다는 증거다. DB 가 3 을 돌려주면 3 으로 보내고 지시문도
    "재알림" 이어야 한다.
    """
    run, log, spawned = cycle
    result, _conn = run([_row(1)], {"dispatch_count": 3})

    assert result["sent"] == 1
    assert len(spawned) == 1
    assert spawned[0]["attempt"] == 3, (
        f"조회 스냅샷(0)을 믿었다 — attempt={spawned[0]['attempt']}"
    )
    assert spawned[0]["count_after"] == 3
    assert "[목표 진행 — 재알림]" in spawned[0]["message"], (
        "실측 횟수가 3 인데 '착수' 로 보냈다"
    )


def test_the_select_and_the_claim_share_one_retry_window() -> None:
    """두 조건식이 어긋나면 claim 이 영원히 실패하거나 영원히 통과한다.

    좁으면(claim 쪽이 더 엄하면) 정상 건까지 전부 `claim_lost` 로 빠져
    오케스트레이션이 통째로 멎고, 넓으면 봉쇄가 아무것도 막지 않는다.
    둘 다 조용히 망가지므로 소스 수준에서 고정한다.
    """
    src = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    preds = re.findall(r"dispatched_at IS NULL[\s\S]{0,240}?::interval\)", src)
    assert len(preds) == 2, f"재시도 창 조건식이 2개가 아니다 — {len(preds)}개"

    def _norm(text: str) -> str:
        text = re.sub(r"\$\d+", "$N", text)          # 파라미터 번호는 달라도 된다
        text = re.sub(r'"\s*\n\s*"', " ", text)      # 문자열 이어붙이기 제거
        return " ".join(text.replace("m.", "").split())

    assert _norm(preds[0]) == _norm(preds[1]), (
        f"조회와 소유권 UPDATE 의 조건식이 다르다:\n"
        f"  조회 : {_norm(preds[0])}\n  claim: {_norm(preds[1])}"
    )
    assert src.count("str(_RETRY_AFTER_MIN)") == 2, (
        "두 조건식이 같은 상수를 넘기지 않는다 — 한쪽이 상수를 바꾸면 어긋난다"
    )


def test_every_reset_path_also_logs() -> None:
    """발송 기록을 되돌리는 코드는 **전부** 자취를 남겨야 한다.

    경로가 하나만 조용하면 추적은 그 경로에서 끊긴다. 새 경로가 생기면
    이 테스트가 먼저 깨지게 둔다 — 사건이 나고 나서 grep 하는 것보다 싸다.
    """
    resets: dict[str, list[str]] = {}
    for path in sorted(_APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = [
            line.strip() for line in text.splitlines()
            if "dispatched_at = NULL" in line or "dispatch_count = 0," in line
        ]
        if hits:
            resets[str(path.relative_to(_APP.parent))] = hits

    assert set(resets) == {
        "app/routers/goals.py",
        "app/services/goal_intervene.py",
        "app/services/milestone_review.py",
    }, f"발송 기록을 되돌리는 경로가 바뀌었다 — {sorted(resets)}"

    for rel in resets:
        text = (_APP.parent / rel).read_text(encoding="utf-8")
        assert "note_record_reset(" in text, (
            f"{rel} 이 발송 기록을 지우면서 자취를 남기지 않는다"
        )


def test_the_reset_log_carries_the_caller_and_the_old_values() -> None:
    """무엇을·왜·어디서 지웠는지, 그리고 **지우기 전 값**이 남는다."""
    log = _Log()
    real = goal_dispatch.logger
    goal_dispatch.logger = log
    try:
        goal_dispatch.note_record_reset(
            "05fdc2bb-0000-0000-0000-000000000000",
            reason="검증 반려",
            where="tests:여기",
            prev_dispatched_at="2026-09-18T07:28:44+09:00",
            prev_dispatch_count=1,
        )
    finally:
        goal_dispatch.logger = real

    got = log.named("goal_dispatch_record_reset")
    assert len(got) == 1, f"기록 초기화가 로그에 없다 — {log.events}"
    assert got[0]["milestone"] == "05fdc2bb"
    assert got[0]["reason"] == "검증 반려"
    assert got[0]["where"] == "tests:여기"
    assert got[0]["prev_dispatched_at"] == "2026-09-18T07:28:44+09:00"
    assert got[0]["prev_dispatch_count"] == 1
    # `where` 는 손으로 적는 값이라 복사해 붙인 채 안 고치는 일이 잦다.
    # 실제 프레임이 정본이다.
    assert got[0]["called_from"].startswith(f"{Path(__file__).name}:"), (
        f"호출 위치가 실제 호출자가 아니다 — {got[0]['called_from']}"
    )


def test_rewind_leaves_a_trace(monkeypatch) -> None:
    """되돌리기가 발송 기록을 지웠다는 사실이 로그에 남는다."""
    from app.core import db_pool
    from app.services import goal_intervene

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)
    monkeypatch.setattr(goal_intervene, "logger", log)

    class _RewindConn(_Conn):
        async def fetchrow(self, query: str, *_a):
            assert "prev" in query, "지우기 전 값을 안 받아 온다"
            return {
                "title": "M1", "goal_id": "36da9794",
                "prev_dispatched_at": "2026-09-18T07:28:44+09:00",
                "prev_dispatch_count": 2,
            }

    monkeypatch.setattr(
        db_pool, "get_pool", lambda: _Pool(_RewindConn([], None)),
    )

    out = asyncio.run(goal_intervene.rewind("05fdc2bb", reason="방향이 틀렸다"))

    assert out["status"] == "pending"
    got = log.named("goal_dispatch_record_reset")
    assert len(got) == 1, f"rewind 가 조용히 지웠다 — {log.events}"
    assert got[0]["milestone"] == "05fdc2bb"
    assert "rewind" in got[0]["reason"]
    assert got[0]["prev_dispatch_count"] == 2
    assert got[0]["called_from"].startswith("goal_intervene.py:")


def test_restart_owner_traces_every_milestone_it_clears(monkeypatch) -> None:
    """담당 재시작은 여러 건을 한꺼번에 지운다 — 건별로 남아야 한다."""
    from app.core import db_pool
    from app.routers import goals as goals_router

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)

    cleared = [
        {
            "milestone_id": "05fdc2bb-0000-0000-0000-000000000000",
            "prev_dispatched_at": "2026-09-18T07:28:44+09:00",
            "prev_dispatch_count": 1,
        },
        {
            "milestone_id": "d195f3d2-0000-0000-0000-000000000000",
            "prev_dispatched_at": None,
            "prev_dispatch_count": 0,
        },
    ]

    class _RestartConn(_Conn):
        async def fetchval(self, query: str, *_a):
            assert "tenant_id = $2::uuid" in query
            return 1

        async def fetch(self, query: str, *_a):
            assert "WITH target" in query, "지운 건 전부를 받아 오지 않는다"
            assert "tenant_id = $3::uuid" in query
            return cleared

    monkeypatch.setattr(
        db_pool, "get_pool", lambda: _Pool(_RestartConn([], None)),
    )

    out = asyncio.run(
        goals_router.restart_owner(
            "36da9794-0000-0000-0000-000000000000",
            "15782f6e-0000-0000-0000-000000000000",
            {"tenant": {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}},
        )
    )

    assert out == {"restarted": True, "resumed": True}
    got = log.named("goal_dispatch_record_reset")
    assert [g["milestone"] for g in got] == ["05fdc2bb", "d195f3d2"], (
        f"지운 건 중 일부만 남았다 — {got}"
    )
    assert all(g["called_from"].startswith("goals.py:") for g in got)
    assert got[0]["prev_dispatch_count"] == 1


def test_a_rejected_review_traces_the_reset(monkeypatch) -> None:
    """반려는 기록을 지우고 다시 지시하게 한다 — 그 사실이 남아야 한다."""
    from app.core import db_pool
    from app.services import milestone_review

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)
    monkeypatch.setattr(milestone_review, "logger", log)

    class _ReviewConn(_Conn):
        async def fetchrow(self, query: str, *_a):
            return {
                "status": "review", "title": "M1", "goal_id": "36da9794",
                "dispatched_at": "2026-09-18T07:28:44+09:00",
                "dispatch_count": 1,
            }

    monkeypatch.setattr(
        db_pool, "get_pool", lambda: _Pool(_ReviewConn([], None)),
    )

    out = asyncio.run(
        milestone_review.confirm("05fdc2bb", ok=False, reason="근거가 없다")
    )

    assert out["status"] == "in_progress"
    got = log.named("goal_dispatch_record_reset")
    assert len(got) == 1, f"반려가 조용히 지웠다 — {log.events}"
    assert got[0]["milestone"] == "05fdc2bb"
    assert got[0]["prev_dispatch_count"] == 1
    assert got[0]["called_from"].startswith("milestone_review.py:")
