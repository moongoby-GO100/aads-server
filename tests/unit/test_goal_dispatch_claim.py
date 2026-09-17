"""같은 마일스톤을 두 번 보내지 않는다 — 기록이 아니라 **소유권**으로 잡는다.

2026-09-17 22:28~22:33Z 실측(GO100 #119, 목표 36da9794). 마일스톤
05fdc2bb(담당 15782f6e)와 d195f3d2(담당 5a17a5d0)가 세 사이클 연속
`goal_dispatch_cycle sent=2` 로 나갔고, 담당 세션 둘에 같은
"[목표 진행 — 착수]" 가 2회씩 실제로 인입됐다 —
`chat_messages` 07:28:44·07:31:42 / 07:29:46·07:32:30 KST.
`goal_dispatch_sent` 네 건이 전부 `attempt=1` 이었고 DB 의
`dispatch_count` 는 1 이었다. 발송은 두 번, 카운트는 한 번이다.

조회 조건이 느슨했던 것이 아니다. 그 사이클의 `skipped=8` 은 담당이 없어
건너뛴 8건(sequence_order 1~4)과 정확히 맞고, 정렬이
`dispatched_at NULLS FIRST, sequence_order` 이므로 seq 5·6 이 아홉째·열째로
잡히려면 **그 시점에 `dispatched_at` 이 NULL** 이어야 한다. 즉 앞 사이클이
남긴 발송 기록이 조회와 발송 사이에 지워졌다.

무조건 `UPDATE ... WHERE id = $1` 은 그것을 알아챌 방법이 없다 — 누가
지웠든, 다른 사이클이 방금 잡았든 덮어쓰고 보낸다. 조건부 UPDATE 는
알아챈다. 지우는 쪽을 전부 찾아 막는 것보다, 보내기 직전에 한 번 더 묻는
편이 싸고 확실하다.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.services import goal_dispatch

# 재시도 창(기본 30분) 안에서는 두 번째 요구가 반드시 빈손으로 돌아와야 한다.
_CLAIM = "RETURNING dispatch_count"


class _Store:
    """마일스톤 한 벌. 조건부 UPDATE 를 DB 처럼 판정한다.

    시간은 "이 테스트가 도는 동안" 으로 충분하다 — 재시도 창이 30분이므로
    한 번 잡힌 행은 이 테스트 안에서 다시 잡히지 않는 것이 맞다.
    """

    def __init__(self, milestone_ids: list[str]) -> None:
        self.rows = {
            mid: {"dispatched_at": None, "dispatch_count": 0, "status": "in_progress"}
            for mid in milestone_ids
        }
        self.claimed: list[str] = []

    def claim(self, milestone_id: str) -> dict | None:
        row = self.rows[milestone_id]
        if row["status"] != "in_progress":
            return None
        if row["dispatched_at"] is not None:
            # 재시도 창 안이다 — 다른 사이클이 이미 가져갔다.
            return None
        row["dispatched_at"] = "now"
        row["dispatch_count"] += 1
        self.claimed.append(milestone_id)
        return {"dispatch_count": row["dispatch_count"]}


class _Conn:
    """조회는 고정된 스냅샷을 주고, 소유권 UPDATE 만 진짜처럼 판정한다.

    스냅샷이 고정인 것이 핵심이다 — 두 사이클이 **같은 낡은 조회 결과**를
    들고 발송에 들어가는 상황이 바로 재현하려는 것이다.
    """

    def __init__(self, store: _Store, rows: list[dict]) -> None:
        self.store = store
        self.rows = rows

    async def execute(self, *_args, **_kwargs) -> str:
        return "INSERT 0 0"

    async def fetch(self, *_args, **_kwargs) -> list[dict]:
        return self.rows

    async def fetchval(self, *_args, **_kwargs):
        return False

    async def fetchrow(self, query: str, *args):
        assert _CLAIM in query, f"소유권 UPDATE 가 아닌 fetchrow: {query[:80]}"
        return self.store.claim(args[0])


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


class _Log:
    """구조화 로그를 그대로 받아 둔다."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.events.append((event, kw))

    warning = info
    error = info

    def named(self, event: str) -> list[dict]:
        return [kw for name, kw in self.events if name == event]


def _row(n: int) -> dict:
    return {
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


@pytest.fixture
def harness(monkeypatch):
    """게이트는 전부 통과시키고 발송만 관찰한다."""
    from app.services import orchestration_limits

    async def _open(*_a, **_k):
        return (False, "")

    async def _ok(*_a, **_k):
        return (True, "")

    monkeypatch.setattr(orchestration_limits, "owner_paused", _open)
    monkeypatch.setattr(orchestration_limits, "cost_gate", _ok)
    monkeypatch.setattr(orchestration_limits, "load_gate", _ok)
    monkeypatch.setattr(goal_dispatch, "_MAX_PER_CYCLE", 10)
    monkeypatch.setattr(goal_dispatch, "_ENABLED", True)

    log = _Log()
    monkeypatch.setattr(goal_dispatch, "logger", log)

    sent_to: list[str] = []

    async def _stream(*, session_id: str, **_kwargs):
        sent_to.append(session_id)
        return
        yield  # pragma: no cover - 제너레이터로 만들기 위한 것

    def _install(conn_factory):
        from app.core import db_pool
        from app.services import chat_service

        monkeypatch.setattr(chat_service, "send_message_stream", _stream)
        monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn_factory()))

    return _install, log, sent_to


async def _drain() -> None:
    """띄운 발송 태스크가 끝날 때까지 기다린다 — 뒤에 남기지 않는다(R-BG)."""
    for _ in range(20):
        await asyncio.sleep(0)
    tasks = list(goal_dispatch._send_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_two_cycles_on_the_same_milestone_send_once(harness, monkeypatch) -> None:
    """두 사이클이 같은 행을 집어도 지시는 한 번만 나간다.

    모듈 잠금은 일부러 무력화한다. API 프로세스가 둘이면(블루/그린 교차
    구간) 잠금은 공유되지 않는다 — 마지막 방어선은 DB 여야 한다.
    """
    install, log, sent_to = harness
    store = _Store([_row(1)["milestone_id"]])

    # 프로세스가 다르면 모듈 잠금은 서로를 못 본다. 그 상태를 흉내 낸다.
    monkeypatch.setattr(goal_dispatch, "_cycle_lock", lambda: asyncio.Lock())

    async def _run():
        install(lambda: _Conn(store, [_row(1)]))
        a, b = await asyncio.gather(
            goal_dispatch.dispatch_pending_milestones(None),
            goal_dispatch.dispatch_pending_milestones(None),
        )
        await _drain()
        return a, b

    a, b = asyncio.run(_run())

    assert a["sent"] + b["sent"] == 1, (
        f"두 사이클이 같은 마일스톤을 각자 보냈다 — sent={a['sent']}/{b['sent']}"
    )
    assert store.claimed == [_row(1)["milestone_id"]], "소유권이 두 번 잡혔다"
    assert store.rows[_row(1)["milestone_id"]]["dispatch_count"] == 1
    assert sent_to == ["session-1"], f"담당에게 실제로 들어간 지시: {sent_to}"


def test_losing_the_claim_leaves_a_log(harness) -> None:
    """졌으면 조용히 넘어가지 않는다 — 왜 안 보냈는지가 남아야 한다."""
    install, log, sent_to = harness
    store = _Store([_row(1)["milestone_id"]])
    # 다른 사이클이 이미 가져간 상태로 시작한다.
    store.rows[_row(1)["milestone_id"]]["dispatched_at"] = "now"

    async def _run():
        install(lambda: _Conn(store, [_row(1)]))
        result = await goal_dispatch.dispatch_pending_milestones(None)
        await _drain()
        return result

    result = asyncio.run(_run())

    assert result["sent"] == 0, "이미 나간 지시를 또 보냈다"
    assert sent_to == [], "소유권을 못 잡았는데 스트림이 열렸다"
    lost = log.named("goal_dispatch_claim_lost")
    assert len(lost) == 1, f"goal_dispatch_claim_lost 가 없다 — {log.events}"
    assert lost[0]["milestone"] == _row(1)["milestone_id"][:8]
    assert lost[0]["session"] == "session-"  # str(session_id)[:8]
    assert lost[0]["why"], "이유가 비어 있다"
    # 못 보낸 것은 미룬 것이다 — 포기로 세면 한도가 헛되이 깎인다.
    assert result["skipped"] == 1 and result["gave_up"] == 0


def test_the_normal_path_is_unchanged(harness) -> None:
    """정상 케이스는 그대로 1회 발송되고 횟수가 1 올라간다."""
    install, log, sent_to = harness
    store = _Store([_row(1)["milestone_id"]])

    async def _run():
        install(lambda: _Conn(store, [_row(1)]))
        result = await goal_dispatch.dispatch_pending_milestones(None)
        await _drain()
        return result

    result = asyncio.run(_run())

    assert result["sent"] == 1
    assert sent_to == ["session-1"]
    assert store.rows[_row(1)["milestone_id"]]["dispatch_count"] == 1
    assert log.named("goal_dispatch_claim_lost") == []

    # 실측 횟수를 로그에 같이 남긴다 — `attempt` 만으로는 이번 같은
    # 불일치(발송 2회 / 카운트 1)를 로그만 보고 잡을 수 없었다.
    done = log.named("goal_dispatch_sent")
    assert len(done) == 1
    assert done[0]["attempt"] == 1
    assert done[0]["count_after"] == 1


def test_an_overlapping_cycle_returns_immediately(harness) -> None:
    """이미 도는 사이클이 있으면 줄 서지 않고 바로 돌아간다."""
    install, log, sent_to = harness
    store = _Store([_row(1)["milestone_id"]])
    gate = asyncio.Event()

    class _SlowConn(_Conn):
        async def fetch(self, *_args, **_kwargs):
            await gate.wait()
            return self.rows

    async def _run():
        install(lambda: _SlowConn(store, [_row(1)]))
        first = asyncio.create_task(goal_dispatch.dispatch_pending_milestones(None))
        # 첫 사이클이 잠금을 쥐고 조회에서 멈출 틈을 준다.
        for _ in range(10):
            await asyncio.sleep(0)

        second = await asyncio.wait_for(
            goal_dispatch.dispatch_pending_milestones(None), 1,
        )
        gate.set()
        done = await asyncio.wait_for(first, 5)
        await _drain()
        return second, done

    second, done = asyncio.run(_run())

    assert second["overlap_skipped"] == 1, f"겹침을 안 걸렀다 — {second}"
    assert second["sent"] == 0
    assert log.named("goal_dispatch_cycle_skipped_overlap"), (
        "겹쳐서 건너뛴 것을 기록하지 않으면 사이클이 왜 안 돌았는지 모른다"
    )
    # 먼저 들어간 사이클은 방해받지 않고 제 일을 끝낸다.
    assert done["sent"] == 1
    assert sent_to == ["session-1"]


def test_the_claim_is_conditional_not_a_blind_write() -> None:
    """조건 없이 덮어쓰면 경합에서 둘 다 보낸다."""
    body = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    claim = body[body.index("UPDATE milestones SET dispatched_at = NOW(), "):]
    claim = claim[:claim.index(_CLAIM) + len(_CLAIM)]

    assert "dispatched_at IS NULL" in claim, "재시도 창 조건이 없다"
    assert "minutes')::interval" in claim, "재시도 간격을 안 본다"
    assert "status = 'in_progress'" in claim, (
        "진행중에서 빠진 마일스톤에도 지시가 나간다"
    )
    assert body.index(_CLAIM) < body.index("_spawn_send("), (
        "소유권을 잡기 전에 발송한다"
    )
