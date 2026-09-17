"""승인 목록이 **못 붙는 카드를 팝업으로 되돌리는지** 지킨다.

2026-09-18 CEO 보고 — "승인카드를 올렸다는데 채팅창에서 안보이는데".

화면 규칙은 이렇다. `source_message_id` 가 있으면 그 응답 버블 아래
인라인으로 붙이고, **팝업·하단 카드에서는 뺀다**(같은 것을 두 군데서
물으면 두 번 눌러야 하는 것처럼 보이므로). 그래서 버블이 화면에 없는데
id 만 남아 있으면 카드는 인라인으로도 팝업으로도 뜨지 않는다.

서버가 마지막 방어선이다 — 붙을 메시지가 없거나 숨김/삭제면
`source_message_id` 를 NULL 로 내려 화면이 팝업으로 되돌리게 한다.

실제 DB 를 태우지 않고 SQL 문자열로 지킨다. 이 경로는 asyncpg 풀이 있어야
돌고 유닛 테스트는 DB 없이 돌기 때문이다.
"""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "app" / "api" / "project_docs.py"


def _pending_sql() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.index("async def approvals_pending(")
    end = text.index("\n@router", start) if "\n@router" in text[start:] else len(text)
    return text[start:end]


def test_붙을_메시지를_실제로_확인한다():
    sql = _pending_sql()
    assert "LEFT JOIN chat_messages m ON m.id = r.source_message_id" in sql


def test_없거나_숨김이거나_지워졌으면_NULL_로_내린다():
    sql = _pending_sql()
    assert "WHEN m.id IS NULL THEN NULL" in sql
    assert "WHEN m.is_hidden IS TRUE THEN NULL" in sql
    assert "WHEN m.deleted_at IS NOT NULL THEN NULL" in sql


def test_세션_필터와_대기_조건은_그대로다():
    """되돌림 방지 — 다른 세션 카드가 섞이거나 만료 카드가 뜨면 안 된다."""
    sql = _pending_sql()
    assert "r.decision = 'pending' AND r.tier = 'approve'" in sql
    assert "r.expires_at > now()" in sql
    assert "($2 = '' OR r.requested_by = $2)" in sql
