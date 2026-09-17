"""제안 카드가 **어느 응답 버블에 붙을지** 스스로 찾는 경로를 지킨다.

## 왜 필요한가

2026-09-17 CEO 지시 — "승인건은 승인 또는 추가지시 버튼 반영해서 적용할수
있음 좋겠다 ... 응답 버블에 반영하는걸로".

버블 아래에 붙이려면 카드가 자기 버블 id 를 알아야 한다. 그 값을
chat_service → model_selector → tool_executor 로 넘기는 방법도 있지만,
세 층 중 어디서든 빠지면 조용히 None 이 되고 카드는 다시 화면 구석
팝업으로만 뜬다. 그래서 카드를 만드는 자리에서 직접 찾는다.

이 파일이 지키는 것은 둘이다.

1. **흐르는 중인 버블을 고른다.** 호출 시점은 턴이 아직 끝나지 않아
   `streaming_placeholder` 가 살아 있다. 그게 지금 회장님 화면에서
   자라고 있는 버블이다. 더 최근에 만들어진 assistant 행이 있어도
   placeholder 가 우선이다.
2. **모르면 안 붙인다.** 찾지 못하면 None 을 준다. 아무 버블에나 붙이면
   회장님이 **다른 답변의 제안을 승인**하시게 된다. 붙일 자리를 모르는
   카드는 지금까지처럼 팝업으로 가는 편이 낫다.
"""
from __future__ import annotations

import asyncio

from app.services.next_step_proposals import _current_bubble_id


class _Conn:
    """fetchval 한 개만 흉내 내는 가짜 커넥션."""

    def __init__(self, result=None, raises: BaseException | None = None):
        self.result = result
        self.raises = raises
        self.sql = ""
        self.args: tuple = ()

    async def fetchval(self, sql, *args):
        self.sql = sql
        self.args = args
        if self.raises is not None:
            raise self.raises
        return self.result


def test_버블_id_를_돌려준다():
    conn = _Conn(result="aaaaaaaa-1111-2222-3333-444444444444")
    got = asyncio.run(_current_bubble_id(conn, "sess-1"))
    assert got == "aaaaaaaa-1111-2222-3333-444444444444"
    assert conn.args == ("sess-1",)


def test_흐르는_중인_placeholder_를_먼저_고른다():
    """정렬절이 placeholder 를 우선하지 않으면, 이전 턴의 확정된 답변에
    이번 턴 제안이 붙는다."""
    conn = _Conn(result="x")
    asyncio.run(_current_bubble_id(conn, "sess-1"))
    assert "streaming_placeholder" in conn.sql
    # COALESCE 없이 쓰면 intent NULL 인 옛 답변이 NULLS FIRST 로 1등이 된다.
    # 2026-09-17 첫 실증에서 카드 2장이 50분 전 버블에 붙은 원인이 이것이었다.
    assert "COALESCE(intent = 'streaming_placeholder', false) DESC" in conn.sql
    assert "role = 'assistant'" in conn.sql


def test_없으면_None_이지_빈문자열이_아니다():
    """빈 문자열을 주면 INSERT 의 NULLIF 를 통과해 잘못된 uuid 캐스팅이 난다."""
    assert asyncio.run(_current_bubble_id(_Conn(result=None), "sess-1")) is None
    assert asyncio.run(_current_bubble_id(_Conn(result=""), "sess-1")) is None


def test_조회가_실패해도_제안_자체를_막지_않는다():
    """붙일 자리를 못 찾은 것뿐이다. 카드는 팝업으로 뜨면 된다 —
    여기서 예외를 올리면 다음 단계 제안이 통째로 사라진다."""
    conn = _Conn(raises=RuntimeError("relation does not exist"))
    assert asyncio.run(_current_bubble_id(conn, "sess-1")) is None


def test_intent_이_NULL_인_옛_답변에_붙지_않는다():
    """정렬절에 COALESCE 가 없으면 NULL 이 NULLS FIRST 로 1등이 된다.

    SQL 문자열로 지킨다 — 이 경로는 실제 DB 를 태워야 값이 갈리는데,
    유닛 테스트는 DB 없이 돈다. 문자열 검사가 유일하게 남는 방어선이다.
    """
    conn = _Conn(result="x")
    asyncio.run(_current_bubble_id(conn, "sess-1"))
    sql = conn.sql
    assert "COALESCE(intent = 'streaming_placeholder', false)" in sql
    # 맨 앞 정렬키가 placeholder 여야 한다 — created_at 이 먼저 오면
    # 흐르는 중인 버블을 고르지 못한다.
    order = sql[sql.index("ORDER BY"):]
    assert order.index("COALESCE") < order.index("created_at")


def test_숨김이거나_지워진_메시지에는_붙지_않는다():
    """붙을 자리가 화면에 없으면 카드는 **어느 화면에도** 남지 않는다.

    2026-09-18 실측. 48시간 동안 11장이 그렇게 사라졌다 — 7장은 숨김
    메시지(러너 진행·중단 조각), 4장은 이미 지워진 메시지에 붙어 있었다.
    인라인은 버블이 없어 못 그리고, 팝업은 source_message_id 가 있다는
    이유로 그 카드를 제외한다.
    """
    conn = _Conn(result="x")
    asyncio.run(_current_bubble_id(conn, "sess-1"))
    assert "is_hidden IS NOT TRUE" in conn.sql
    assert "deleted_at IS NULL" in conn.sql


def test_러너_진행_메시지에는_붙지_않는다():
    """러너 진행은 대화에서 한 줄로 접힌다(대시보드 runnerGroup).
    접힌 줄 안에는 승인 버튼을 그릴 자리가 없다."""
    conn = _Conn(result="x")
    asyncio.run(_current_bubble_id(conn, "sess-1"))
    for intent in ("pipeline_runner", "runner_notification", "ai_review_warning"):
        assert intent in conn.sql
    assert "[Pipeline Runner]" in conn.sql
