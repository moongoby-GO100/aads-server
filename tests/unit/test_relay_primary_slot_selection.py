"""릴레이가 CLI 를 돌릴 때 고르는 슬롯 — 주계정이 실제로 쓰이는가.

2026-09-17 대표님 지적: "LLM관리에서 구독에서 주계정 변경을 할수 있게 해달라고
그럼 우선 그게 사용되게 … 채팅순서가 아니라 CLI사용 슬롯을 말하는거다".

릴레이의 `current` 는 DB priority 1순위 슬롯이고, 설정 화면의 주계정 전환이
바꾸는 값이 바로 그것이다. 그런데 `_pick_auth` 가 `current in ("1","2")` 로
좁혀 놓아, 슬롯 3·4 를 주계정으로 올려도 CLI 는 계속 슬롯 1 로 돌았다.

자동 *폴백* 은 여전히 1·2 안에서만 해야 한다 — 아무도 고르지 않았는데 남의
계정으로 새는 것은 막은 채, 고른 계정만 존중한다.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "scripts" / "claude_relay_server.py"


def _source() -> str:
    return RELAY.read_text(encoding="utf-8")


def _pick_auth_body(source: str) -> str:
    start = source.index("def _pick_auth(")
    end = source.index("def _pick_token(", start)
    return source[start:end]


def test_primary_slot_is_not_clamped_to_slots_one_and_two():
    body = _pick_auth_body(_source())
    assert 'first_slot = current if current in ("1", "2") else "1"' not in body
    assert 'if current and current != "1":' in body
    assert "_slot_credentials_path(current).exists()" in body


def test_auto_fallback_still_stays_inside_slots_one_and_two():
    """주계정만 넓힌다. 폴백까지 넓히면 아무도 고르지 않은 계정이 쓰인다."""
    body = _pick_auth_body(_source())
    assert 'for fallback in ("1", "2"):' in body
    assert "if fallback != first_slot:" in body


def test_explicit_caller_slot_still_wins_alone():
    body = _pick_auth_body(_source())
    assert "explicitly_requested = preferred_slot in tokens" in body
    assert "if not explicitly_requested:" in body


def test_missing_legacy_env_file_does_not_fail_the_switch():
    """레거시 파일이 없다고 500 을 내면 효력 있는 DB 경로까지 막힌다."""
    source = _source()
    start = source.index("async def handle_oauth_switch(")
    end = source.index("async def handle_sessions(", start)
    body = source[start:end]
    assert "if _ENV_OAUTH_FILE.exists():" in body
    assert 'return web.json_response({"error": str(e)}, status=500)' not in body
    # 자격증명이 없는 슬롯으로 넘기는 것은 여전히 막는다.
    assert "status=409" in body
