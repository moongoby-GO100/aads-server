"""설정 화면의 주계정 전환이 실제로 슬롯을 바꾸는가.

2026-09-17 대표님 지적: "LLM관리에서 구독에서 주계정 변경을 할수 있게 해달라고
그럼 우선 그게 사용되게".

버튼은 있었지만 두 곳에서 끊겨 있었다.

  1. 전환이 릴레이의 CURRENT_OAUTH 만 바꿨다. 그것은 호출자가 슬롯을 지정하지
     않았을 때 쓰는 기본값일 뿐이고, 채팅은 늘 슬롯을 명시해서 부른다.
     채팅의 순서를 정하는 것은 DB priority 다.
  2. 이름 별명이 슬롯 1·2 에만 있어서 "slot3"/"slot4" 로 부르면 레코드를
     찾지 못하고 조용히 False 를 돌려줬다.

여기서는 2번(순수 함수)을 고정한다. 1번은 ops 엔드포인트가
set_token_order_async 를 부르는지로 확인한다.
"""
import ast
from pathlib import Path

from app.core.auth_provider import select_record_by_alias

ROOT = Path(__file__).resolve().parents[2]

RECORDS = [
    {"key_name": "ANTHROPIC_AUTH_TOKEN", "label": "moong76@gmail", "slot": "1"},
    {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "label": "moongoby@naver.com", "slot": "2"},
    {"key_name": "ANTHROPIC_AUTH_TOKEN_3", "label": "jinah-biseo(244)", "slot": "3"},
    {"key_name": "ANTHROPIC_AUTH_TOKEN_4", "label": "라일론", "slot": "4"},
]


def test_every_slot_is_addressable_by_slot_name():
    """슬롯 1·2 만 되던 것 — 3·4 도 "slotN" 으로 찾아야 한다."""
    for slot in ("1", "2", "3", "4"):
        found = select_record_by_alias(RECORDS, "slot%s" % slot)
        assert found is not None, slot
        assert found["slot"] == slot


def test_label_and_key_name_still_work():
    assert select_record_by_alias(RECORDS, "라일론")["slot"] == "4"
    assert select_record_by_alias(RECORDS, "ANTHROPIC_AUTH_TOKEN_3")["slot"] == "3"
    assert select_record_by_alias(RECORDS, "naver")["slot"] == "2"
    assert select_record_by_alias(RECORDS, "gmail")["slot"] == "1"


def test_unknown_and_empty_return_nothing():
    assert select_record_by_alias(RECORDS, "slot9") is None
    assert select_record_by_alias(RECORDS, "") is None
    assert select_record_by_alias([], "slot1") is None


def test_blank_slot_record_is_not_matched_by_empty_alias():
    """slot="" 인 레코드가 아무 호출에나 걸리면 엉뚱한 계정이 주계정이 된다."""
    records = [{"key_name": "X", "label": "", "slot": ""}]
    assert select_record_by_alias(records, "slot1") is None


def test_account_switch_endpoint_changes_db_priority():
    """릴레이만 찌르고 끝나면 채팅은 그대로다 — DB priority 를 바꿔야 한다."""
    source = (ROOT / "app" / "api" / "ops.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "switch_claude_account"
    )
    body = ast.get_source_segment(source, func) or ""
    assert "set_token_order_async" in body
    # 릴레이가 안 떠 있어도 전환 자체는 되어야 한다. 하드 실패는 409 뿐이다.
    assert "409" in body
