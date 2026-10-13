"""구독이 해지된 Claude 슬롯은 켜 둔 채 건너뛴다 (AADS-CLAUDE-SLOT-SUBSCRIPTION-GATE).

2026-10-13 슬롯 2 구독 해지 사고: 토큰은 멀쩡한데 CLI 가 매번
"Your organization has disabled Claude subscription access ..." 로 실패했고,
이 문장이 인증 오류로 분류되지 않아 슬롯이 쉬지 않았다.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.claude_oauth_credentials import (
    SUBSCRIPTION_HOLD_SEC,
    classify_auth_error,
    classify_subscription_profile,
    is_subscription_inactive_text,
    read_slot_auth_status,
    record_slot_validation,
    record_subscription_profile,
)
from app.services.account_primary import auto_order, subscription_inactive
from scripts import claude_relay_server as relay

BLOCK_SENTENCE = (
    "Your organization has disabled Claude subscription access for Claude Code · "
    "Use an Anthropic API key instead..."
)

CANCELED_PROFILE = {
    "account": {"has_claude_max": False},
    "organization": {
        "organization_type": "claude_free",
        "billing_type": "none",
        "subscription_status": "canceled",
        "rate_limit_tier": "default_claude_ai",
    },
}
MAX_PROFILE = {
    "account": {"has_claude_max": True},
    "organization": {
        "organization_type": "claude_max",
        "billing_type": "google_play_subscription",
        "subscription_status": "active",
        "rate_limit_tier": "default_claude_max_20x",
    },
}


def _credential(root: Path, slot: str) -> Path:
    path = root / f"slot{slot}" / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "unit-access-value",
        "refreshToken": "unit-refresh-value",
        "expiresAt": int((time.time() + 28_800) * 1000),
    }}), encoding="utf-8")
    return path


def _two_slots(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_SLOT_HOME_ROOT", tmp_path)
    monkeypatch.setattr(relay, "_read_oauth_tokens", lambda: ("", "", "2", "one", "two"))
    return _credential(tmp_path, "1"), _credential(tmp_path, "2")


# ── A. 분류 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    BLOCK_SENTENCE,
    "organization has disabled access",
    "subscription_inactive: Claude 구독이 비활성인 계정이라 건너뜁니다 (slot=2)",
])
def test_block_sentence_is_classified_as_subscription_inactive(text):
    assert classify_auth_error(text) == "subscription_inactive"
    assert is_subscription_inactive_text(text)


@pytest.mark.parametrize("text,expected", [
    ("OAuth access token has been revoked", "revoked"),
    ("OAuth token has expired", "expired"),
    ("HTTP 401", "unauthorized"),
    ("Failed to authenticate", "authentication_failed"),
    ("something else", "error"),
])
def test_existing_classes_are_unchanged(text, expected):
    assert classify_auth_error(text) == expected
    assert not is_subscription_inactive_text(text)


def test_long_text_is_not_treated_as_a_banner():
    assert not is_subscription_inactive_text("organization has disabled " + "x" * 500)


# ── A. 슬롯 선택 ───────────────────────────────────────────────────────────

def test_subscription_failure_puts_slot_on_hold_without_touching_account_switch(tmp_path):
    cred = _credential(tmp_path, "2")
    record_slot_validation(cred, ok=False, error=BLOCK_SENTENCE)

    status = read_slot_auth_status(cred)

    assert status["status"] == "subscription_inactive"
    assert status["subscription_hold"] is True
    assert status["auth_available"] is False
    assert status["healthy"] is False
    assert status["refresh_capable"] is True  # 자격증명 자체는 멀쩡하다
    assert "unit-access-value" not in json.dumps(status)


def test_hold_expires_when_not_rechecked(tmp_path):
    cred = _credential(tmp_path, "2")
    record_slot_validation(cred, ok=False, error=BLOCK_SENTENCE)

    later = time.time() + SUBSCRIPTION_HOLD_SEC + 60
    status = read_slot_auth_status(cred, now_epoch=later)

    assert status["subscription_hold"] is False
    assert status["status"] != "subscription_inactive"
    assert status["auth_available"] is True


def test_pick_auth_skips_held_slot_and_uses_next(monkeypatch, tmp_path):
    cred1, cred2 = _two_slots(monkeypatch, tmp_path)
    record_slot_validation(cred2, ok=False, error=BLOCK_SENTENCE)

    selected = relay._pick_auth()

    assert selected["slot"] == "1"
    assert selected["source"] == "slot_credentials"


def test_held_slot_is_not_used_even_when_explicitly_requested_or_recovering(monkeypatch, tmp_path):
    _cred1, cred2 = _two_slots(monkeypatch, tmp_path)
    record_slot_validation(cred2, ok=False, error=BLOCK_SENTENCE)

    assert relay._pick_auth(preferred_slot="2")["source"] == "none"
    assert relay._pick_auth(preferred_slot="2", allow_auth_recovery=True)["source"] == "none"


def test_all_slots_held_returns_no_auth(monkeypatch, tmp_path):
    cred1, cred2 = _two_slots(monkeypatch, tmp_path)
    for cred in (cred1, cred2):
        record_slot_validation(cred, ok=False, error=BLOCK_SENTENCE)

    assert relay._pick_auth()["source"] == "none"


# ── B. 재확인(읽기 전용 프로필) ────────────────────────────────────────────

def test_profile_classification():
    assert classify_subscription_profile(CANCELED_PROFILE) == "canceled"
    assert classify_subscription_profile(MAX_PROFILE) == "active"
    assert classify_subscription_profile({"organization": {"organization_type": "claude_free"}}) == "free"
    # billing_type=none 만으로는 비활성이라고 단정하지 않는다.
    assert classify_subscription_profile({"organization": {"billing_type": "none"}}) == "unknown"
    assert classify_subscription_profile("garbage") == "unknown"


def test_recheck_marks_canceled_slot_and_restores_it_when_subscription_returns(tmp_path):
    cred = _credential(tmp_path, "2")

    assert record_subscription_profile(cred, CANCELED_PROFILE) == "canceled"
    held = read_slot_auth_status(cred)
    assert held["subscription_hold"] is True
    assert held["subscription"]["status"] == "canceled"
    assert held["subscription"]["checked_at"]

    assert record_subscription_profile(cred, MAX_PROFILE) == "active"
    restored = read_slot_auth_status(cred)
    assert restored["subscription_hold"] is False
    assert restored["auth_available"] is True
    assert restored["subscription"]["status"] == "active"
    assert restored["status"] == "ready"


def test_unknown_profile_does_not_release_or_extend_hold(tmp_path):
    cred = _credential(tmp_path, "2")
    record_slot_validation(cred, ok=False, error=BLOCK_SENTENCE)

    assert record_subscription_profile(cred, {"unexpected": True}) == "unknown"

    assert read_slot_auth_status(cred)["subscription_hold"] is True


def test_active_profile_does_not_overwrite_an_unrelated_failure(tmp_path):
    cred = _credential(tmp_path, "1")
    record_slot_validation(cred, ok=False, error="OAuth access token has been revoked")

    record_subscription_profile(cred, MAX_PROFILE)

    assert read_slot_auth_status(cred)["status"] == "revoked"


def test_canceled_profile_does_not_erase_revoked_so_active_cannot_make_it_ready(tmp_path):
    cred = _credential(tmp_path, "1")
    record_slot_validation(cred, ok=False, error="OAuth access token has been revoked")

    record_subscription_profile(cred, CANCELED_PROFILE)
    assert read_slot_auth_status(cred)["status"] == "revoked"
    assert read_slot_auth_status(cred)["subscription"]["status"] == "canceled"

    record_subscription_profile(cred, MAX_PROFILE)
    status = read_slot_auth_status(cred)
    assert status["status"] == "revoked"
    assert status["healthy"] is False


def test_stale_active_profile_cannot_clear_newer_cli_hold(tmp_path):
    cred = _credential(tmp_path, "2")
    fetch_started = time.time() - 5
    record_slot_validation(cred, ok=False, error=BLOCK_SENTENCE, checked_at=time.time() - 1)

    assert record_subscription_profile(cred, MAX_PROFILE, observed_at=fetch_started) == "active"

    assert read_slot_auth_status(cred)["subscription_hold"] is True


def test_active_profile_clears_hold_recorded_before_the_fetch(tmp_path):
    cred = _credential(tmp_path, "2")
    record_slot_validation(cred, ok=False, error=BLOCK_SENTENCE, checked_at=time.time() - 60)

    record_subscription_profile(cred, MAX_PROFILE, observed_at=time.time() - 5)

    assert read_slot_auth_status(cred)["subscription_hold"] is False


def test_concurrent_sidecar_updates_do_not_lose_subscription_record(tmp_path):
    import threading

    cred = _credential(tmp_path, "2")
    errors = []

    def writer(fn):
        try:
            for _ in range(40):
                fn()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(lambda: record_slot_validation(cred, ok=True),)),
        threading.Thread(target=writer, args=(lambda: record_subscription_profile(cred, MAX_PROFILE),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert read_slot_auth_status(cred)["subscription"]["status"] == "active"


def test_validation_write_keeps_subscription_record(tmp_path):
    cred = _credential(tmp_path, "1")
    record_subscription_profile(cred, MAX_PROFILE)

    record_slot_validation(cred, ok=True)

    assert read_slot_auth_status(cred)["subscription"]["status"] == "active"


def test_relay_recheck_uses_profile_fetch_and_survives_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "_SLOT_HOME_ROOT", tmp_path)
    cred = _credential(tmp_path, "2")
    seen = []

    def fetch(token):
        seen.append(token)
        return CANCELED_PROFILE

    assert relay._recheck_slot_subscription("2", fetch_profile=fetch) == "canceled"
    assert seen == ["unit-access-value"]
    assert read_slot_auth_status(cred)["subscription_hold"] is True

    def boom(token):
        raise OSError("network down Bearer " + token)

    assert relay._recheck_slot_subscription("2", fetch_profile=boom) == ""
    assert read_slot_auth_status(cred)["subscription_hold"] is True  # 실패는 상태를 바꾸지 않는다
    assert relay._recheck_slot_subscription("9", fetch_profile=fetch) == ""  # 자격증명 없음


# ── C. 자동 주계정 순서 ────────────────────────────────────────────────────

def _acct(name, *, quota=True, inactive=False, reset_in_h=None, priority=9):
    now = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)
    return {
        "key_name": name,
        "has_quota": quota,
        "subscription_inactive": inactive,
        "resets_at": now + timedelta(hours=reset_in_h) if reset_in_h is not None else None,
        "headroom_pct": 50.0,
        "priority": priority,
    }


def test_auto_order_sends_held_account_back_even_if_it_resets_first():
    order = auto_order([
        _acct("held", inactive=True, reset_in_h=1, priority=1),
        _acct("usable", reset_in_h=30, priority=2),
    ])
    assert order == ["usable", "held"]


def test_subscription_inactive_helper():
    assert subscription_inactive({"hold": True, "status": "active"})
    # 보류가 만료됐으면(릴레이가 쓰는 중) canceled 기록이 남아 있어도 뒤로 보내지 않는다.
    assert not subscription_inactive({"hold": False, "status": "canceled"})
    assert not subscription_inactive({"hold": False, "status": "free"})
    assert not subscription_inactive({"hold": False, "status": "active"})
    assert not subscription_inactive({"hold": False, "status": None})
    assert not subscription_inactive(None)


@pytest.mark.asyncio
async def test_relay_slot_subscriptions_reads_health(monkeypatch):
    import httpx

    from app.services import account_primary

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"slot_auth": {
                "1": {"subscription": {"status": "active", "checked_at": "t1"},
                      "subscription_hold": False},
                "2": {"subscription": {"status": "canceled", "checked_at": "t2"},
                      "subscription_hold": True},
            }}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    subs = await account_primary.relay_slot_subscriptions()

    assert subs["2"] == {"status": "canceled", "checked_at": "t2", "hold": True}
    assert subs["1"]["hold"] is False
    assert subscription_inactive(subs["2"]) and not subscription_inactive(subs["1"])


@pytest.mark.asyncio
async def test_relay_slot_subscriptions_returns_empty_when_relay_unreachable(monkeypatch):
    import httpx

    from app.services import account_primary

    class Down:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise httpx.ConnectError("down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", Down)

    assert await account_primary.relay_slot_subscriptions() == {}


# ── D. 오류 문장이 본문으로 새지 않는다 ────────────────────────────────────

def _assistant(*texts):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": t} for t in texts]}}


def test_relay_drops_banner_only_assistant_event():
    assert relay._is_subscription_banner_event(_assistant(BLOCK_SENTENCE))
    assert not relay._is_subscription_banner_event(_assistant("정상 답변입니다"))
    assert not relay._is_subscription_banner_event(_assistant(BLOCK_SENTENCE, "정상 답변입니다"))
    assert not relay._is_subscription_banner_event({"type": "assistant", "message": {"content": []}})


def test_chat_never_receives_block_sentence_as_text():
    from app.services.model_selector import _map_cli_event

    assert _map_cli_event(_assistant(BLOCK_SENTENCE)) is None

    mixed = _map_cli_event(_assistant(BLOCK_SENTENCE, "실제 답변"))
    contents = [e.get("content", "") for e in (mixed or []) if e.get("type") == "delta"]
    assert contents == ["실제 답변"]


def test_subscription_error_counts_as_auth_error_for_slot_fallback():
    from app.services.model_selector import _is_cli_auth_error

    assert _is_cli_auth_error(BLOCK_SENTENCE)
    assert _is_cli_auth_error("subscription_inactive: 건너뜁니다 (slot=2)")


@pytest.mark.asyncio
async def test_subscription_error_does_not_retry_same_slot(monkeypatch):
    calls = []

    async def blocked(*args, **kwargs):
        calls.append(kwargs.get("oauth_slot"))
        yield {"type": "error", "content": "subscription_inactive: 건너뜁니다 (slot=2)"}

    monkeypatch.setattr("app.services.model_selector._stream_cli_relay_once", blocked)
    from app.services.model_selector import _stream_cli_relay

    events = [e async for e in _stream_cli_relay(
        "claude-sonnet", "system", [{"role": "user", "content": "ping"}], oauth_slot="2",
    )]

    assert calls == ["2"]
    assert events[-1]["type"] == "error"
    assert BLOCK_SENTENCE not in events[-1]["content"]


@pytest.mark.asyncio
async def test_result_error_reaches_chat_only_as_short_marker(monkeypatch):
    """result(is_error) 의 차단 문장은 error 이벤트의 표식으로만 바뀌어 나간다."""
    import httpx

    from scripts.claude_model_contract import CONTRACT_VERSION
    from app.services import model_selector

    class Health:
        status_code = 200

        def json(self):
            return {
                "claude_model_contract": {"version": CONTRACT_VERSION},
                "slot_auth": {"2": {"auth_available": True}},
            }

    class Resp:
        status_code = 200

        async def aiter_lines(self):
            yield json.dumps(_assistant(BLOCK_SENTENCE))
            yield json.dumps({"type": "result", "is_error": True, "result": BLOCK_SENTENCE})

    class Stream:
        async def __aenter__(self):
            return Resp()

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return Health()

        def stream(self, *a, **k):
            return Stream()

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    events = [e async for e in model_selector._stream_cli_relay_once(
        "claude-sonnet", "system", [{"role": "user", "content": "ping"}], oauth_slot="2",
    )]

    texts = [e.get("content", "") for e in events if e.get("type") == "delta"]
    assert texts == []
    assert events[-1]["type"] == "error"
    assert events[-1]["content"].startswith("subscription_inactive:")
    assert "Anthropic API key" not in events[-1]["content"]
