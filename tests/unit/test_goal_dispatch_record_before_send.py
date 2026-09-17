"""발송 기록은 **보내기 전에** 남긴다 — 취소되면 뒤에 있는 기록은 사라진다.

2026-09-17 실측. 라일론 목표(NTV2, P0) M4 의 착수 지시가 10:17:12 KST 에
담당 세션(ce2822ed)으로 실제로 들어갔는데 `milestones.dispatched_at` 은
NULL 로 남았다. 발송 기록 UPDATE 가 `send_message_stream` **뒤에** 있었고,
상위 사이클의 `asyncio.wait_for(..., 120초)` 가 먼저 터져 코루틴이
취소됐기 때문이다. 다음 주기는 NULL 을 보고 같은 지시를 10:20:09 에 다시
보냈다 — 담당은 같은 일을 두 번 받고 첫 턴은 `interrupted_partial` 로 죽었다.

못 보냈는데 보냈다고 적히는 쪽이 나은가. 그렇다. 그건 다음 주기가
"재알림" 으로 복구한다. 반대는 복구되지 않고 중복 발송과 비용을 계속 만든다.

같은 사고의 다른 얼굴도 여기서 지킨다 — 소유자(`owner_session_id`)는
채워져 있는데 `goal_task_links` 가 비어 **어느 창에도 목표가 뜨지 않는**
상태. 그날 대표님이 "안 뜨지?" 로 먼저 알아채셨다.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from app.services import goal_dispatch

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "app" / "services" / "goal_dispatch.py").read_text(encoding="utf-8")

RECORD = "UPDATE milestones SET dispatched_at = NOW(), "
SEND = "_spawn_send("


def test_dispatch_is_recorded_before_the_stream_starts() -> None:
    """스트림이 태스크로 나갔어도 순서는 그대로다 — 기록이 먼저다.

    태스크화로 사이클 상한에 취소될 일은 줄었지만 순서를 바꿔도 되는
    것은 아니다. 발송 태스크가 서버 종료로 끊기거나 실패해도 기록이
    남아 있어야 다음 주기가 중복 발송이 아니라 재알림으로 이어받는다.
    """
    body = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    assert body.index(RECORD) < body.index(SEND), (
        "발송 기록이 발송 뒤에 있다 — 태스크가 끊기면 기록이 사라지고 "
        "다음 주기가 같은 지시를 또 보낸다"
    )


def test_the_send_task_does_not_record_dispatch_itself() -> None:
    """기록은 사이클의 커넥션으로 한 번만. 태스크가 또 적으면 두 번 센다."""
    task = inspect.getsource(goal_dispatch._send_milestone)

    assert RECORD not in task
    assert "dispatch_count = dispatch_count + 1" not in task


def test_one_attempt_increments_the_counter_once() -> None:
    assert SRC.count("dispatch_count = dispatch_count + 1") == 1, (
        "한 번의 시도가 재시도 한도를 두 칸 깎는다"
    )


def test_send_failure_still_leaves_a_reason() -> None:
    """실패를 조용히 넘기면 '보냈는데 아무 일도 안 일어남' 이 침묵으로 끝난다."""
    body = inspect.getsource(goal_dispatch._send_milestone)

    assert "goal_dispatch_send_failed" in body
    assert "발송 실패" in body


def test_owner_links_are_repaired_before_dispatch() -> None:
    body = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    assert "repair_owner_links(conn)" in body, (
        "소유자만 채워진 목표는 담당 화면에 뜨지 않는다 — 지시를 받아도 "
        "자기 목표를 찾을 수 없다"
    )
    assert body.index("repair_owner_links(conn)") < body.index("rows = await conn.fetch("), (
        "연결고리 복원이 발송보다 뒤에 있다"
    )


def test_repair_does_not_resurrect_detached_owners() -> None:
    sql = inspect.getsource(goal_dispatch.repair_owner_links)

    assert "NOT EXISTS" in sql
    assert "ON CONFLICT" not in sql, (
        "이미 있는 줄을 되살리면 대표님이 손으로 떼신 담당이 다시 붙는다 — "
        "복원이 아니라 되돌리기다"
    )


def test_repair_counts_inserted_rows() -> None:
    class _Conn:
        def __init__(self, result: str) -> None:
            self.result = result

        async def execute(self, *_args, **_kwargs) -> str:
            return self.result

    assert asyncio.run(goal_dispatch.repair_owner_links(_Conn("INSERT 0 3"))) == 3
    assert asyncio.run(goal_dispatch.repair_owner_links(_Conn("INSERT 0 0"))) == 0
    # 형식이 달라져도 사이클을 죽이지 않는다.
    assert asyncio.run(goal_dispatch.repair_owner_links(_Conn("weird"))) == 0
