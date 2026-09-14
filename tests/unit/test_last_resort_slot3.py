"""슬롯 3(진아 계정) — 등록은 하되 먼저 집히면 안 된다.

2026-09-15 대표님 지시로 들어온 세 번째 OAuth 슬롯이다. AADS 소유 계정이
아니므로 쓰이면 진아 쪽 주간 한도가 줄어든다. 그래서 세 가지를 시험한다.

1. 슬롯이 **붙는가** — 이름이 없어 빈 슬롯("")이 되면 주소 지정이 불가능해
   폴백에서 조용히 사라진다. 실제로 그 상태였다.
2. 순서가 **맨 뒤인가** — DB priority 만 믿으면 누가 우선순위를 만지는 순간
   앞으로 올라온다.
3. 릴레이가 **자동으로 집지 않는가** — 명시 요청일 때만 쓰여야 한다.
"""
import asyncio

import pytest

from app.core.auth_provider import LAST_RESORT_SLOTS, _assign_slots
from app.services import model_selector
from scripts import claude_relay_server as relay


def test_key_name_assigns_slot_three():
    records = [
        {"key_name": "ANTHROPIC_AUTH_TOKEN", "value": "tok-1", "priority": 2},
        {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "value": "tok-2", "priority": 1},
        {"key_name": "ANTHROPIC_AUTH_TOKEN_3", "value": "tok-3", "priority": 3},
    ]
    by_name = {r["key_name"]: r["slot"] for r in _assign_slots(records)}
    assert by_name["ANTHROPIC_AUTH_TOKEN"] == "1"
    assert by_name["ANTHROPIC_AUTH_TOKEN_2"] == "2"
    assert by_name["ANTHROPIC_AUTH_TOKEN_3"] == "3"


def test_slot_three_survives_record_filter(monkeypatch):
    async def _records(include_rate_limited=True):
        return [
            {"key_name": "ANTHROPIC_AUTH_TOKEN", "slot": "1", "priority": 2},
            {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "slot": "2", "priority": 1},
            {"key_name": "ANTHROPIC_AUTH_TOKEN_3", "slot": "3", "priority": 3},
        ]

    monkeypatch.setattr(model_selector, "_ap_get_key_records_async", _records)
    slots = asyncio.run(model_selector._get_claude_slot_records())
    assert set(slots) == {"1", "2", "3"}


def test_last_resort_sorts_last_even_with_top_priority():
    """우선순위가 1이어도 뒤로 간다 — 순서를 DB에 맡기지 않는다."""
    records = {
        "1": {"priority": 2},
        "2": {"priority": 9},
        "3": {"priority": 1},
    }
    order = [s for s, _ in sorted(
        records.items(), key=lambda i: model_selector._slot_sort_key(i[0], i[1]))]
    assert order == ["1", "2", "3"]
    assert "3" in LAST_RESORT_SLOTS


def _relay_tokens(monkeypatch, extras):
    monkeypatch.setattr(
        relay, "_read_oauth_tokens",
        lambda: ("tok-1", "tok-2", "1", "acct1", "acct2"),
    )
    relay._EXTRA_SLOT_AUTH.clear()
    relay._EXTRA_SLOT_AUTH.update(extras)
    monkeypatch.setattr(relay, "_last_429_slot", 0, raising=False)
    # 슬롯 자격증명 파일이 없는 상태 = env 폴백 경로 (진아 토큰은 갱신 불가)
    monkeypatch.setattr(relay, "_slot_auth_status", lambda slot, env_fallback_available=False: {
        "status": "missing", "refresh_capable": False,
    })
    monkeypatch.setattr(relay, "_slot_credentials_path", lambda slot: __import__("pathlib").Path(
        "/nonexistent/slot%s/.credentials.json" % slot))


def test_relay_never_picks_last_resort_automatically(monkeypatch):
    _relay_tokens(monkeypatch, {"3": {"token": "tok-3", "label": "jinah"}})
    auth = relay._pick_auth()
    assert auth["slot"] in ("1", "2")


def test_relay_honors_explicit_last_resort_request(monkeypatch):
    _relay_tokens(monkeypatch, {"3": {"token": "tok-3", "label": "jinah"}})
    auth = relay._pick_auth(preferred_slot="3")
    assert auth["slot"] == "3"
    assert auth["label"] == "jinah"
    assert auth["source"] == "env_fallback"


def test_usage_payload_marks_last_resort():
    """사용량바가 색을 다르게 그릴 수 있어야 한다."""
    import inspect

    from app.services import oauth_usage_tracker

    src = inspect.getsource(oauth_usage_tracker.get_slot_usage_all)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert '"last_resort": slot in _LAST_RESORT_SLOTS' in body
