"""자동 주계정 규칙 — 곧 리셋될 한도부터 태운다.

2026-09-17 대표님 확인: "'곧 리셋될 한도부터 태운다' 가 맞아".

곧 리셋될 잔량은 안 쓰면 사라진다. 그러니 정렬 기준은 잔량이 아니라 갱신
시각이다. 잔량은 동률일 때만 본다.
"""
from datetime import datetime, timedelta, timezone

from app.services.account_primary import auto_order

NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)


def acct(name, *, quota=True, reset_in_h=None, headroom=50.0, priority=9):
    return {
        "key_name": name,
        "has_quota": quota,
        "resets_at": NOW + timedelta(hours=reset_in_h) if reset_in_h is not None else None,
        "headroom_pct": headroom,
        "priority": priority,
    }


def test_soonest_reset_is_burned_first():
    order = auto_order([
        acct("late", reset_in_h=40),
        acct("soon", reset_in_h=2),
        acct("mid", reset_in_h=9),
    ])
    assert order == ["soon", "mid", "late"]


def test_exhausted_accounts_go_last_even_if_resetting_soonest():
    """한도가 없으면 곧 리셋돼도 지금 태울 것이 없다."""
    order = auto_order([
        acct("empty", quota=False, reset_in_h=1),
        acct("usable", reset_in_h=30),
    ])
    assert order == ["usable", "empty"]


def test_accounts_without_reset_data_go_behind_known_ones():
    order = auto_order([
        acct("unknown", reset_in_h=None),
        acct("known", reset_in_h=50),
    ])
    assert order == ["known", "unknown"]


def test_tie_on_reset_prefers_more_headroom():
    order = auto_order([
        acct("thin", reset_in_h=5, headroom=5.0),
        acct("fat", reset_in_h=5, headroom=80.0),
    ])
    assert order == ["fat", "thin"]


def test_full_tie_falls_back_to_existing_priority():
    order = auto_order([
        acct("second", reset_in_h=5, headroom=50.0, priority=2),
        acct("first", reset_in_h=5, headroom=50.0, priority=1),
    ])
    assert order == ["first", "second"]
