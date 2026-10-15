"""설정화면 '재로그인 필요' 판정에 토큰 만료를 반영하고 전 슬롯을 감시한다.

2026-10-15 슬롯1 토큰이 약 20시간 만료·갱신 거부 상태였는데 화면에 '재로그인 필요'가
뜨지 않았다. refreshToken 존재만 보고 expiresAt 은 보지 않았기 때문이다.
"""
import asyncio
import json
import time

import pytest

from app.api import llm_keys
from app.services import slot_token_health as health
from scripts import account_login

NOW = 1_800_000_000.0


def _cred(path, *, expires_in=None, refresh="r-secret"):
    oauth = {"accessToken": "a-secret", "subscriptionType": "max"}
    if refresh:
        oauth["refreshToken"] = refresh
    if expires_in is not None:
        oauth["expiresAt"] = int((NOW + expires_in) * 1000)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    return path


@pytest.mark.parametrize("expires_in,refresh,ok,reason", [
    (-20 * 3600, "r", False, "token_expired"),                       # 만료 + 유예 경과
    (-(account_login.EXPIRY_GRACE_SEC - 60), "r", True, None),       # 유예 안
    (3600, "r", True, None),                                          # 정상
    (3600, None, False, "refresh_missing"),                           # 갱신 토큰 없음
    (-20 * 3600, None, False, "refresh_missing"),                     # 둘 다면 갱신 토큰 부재가 우선
])
def test_credential_status_cases(tmp_path, expires_in, refresh, ok, reason):
    path = _cred(tmp_path / "c.json", expires_in=expires_in, refresh=refresh)
    st = account_login.claude_credential_status(path, now=NOW)
    assert st["ok"] is ok
    assert st["reason"] == reason
    assert "secret" not in json.dumps(st)


def test_missing_file_and_unknown_expiry(tmp_path):
    assert account_login.claude_credential_status(tmp_path / "none.json", now=NOW)["reason"] == "credential_missing"
    path = _cred(tmp_path / "c.json", expires_in=None)
    assert account_login.claude_credential_status(path, now=NOW)["ok"] is True


def test_bindings_expired_slot_needs_login(tmp_path, monkeypatch):
    root = tmp_path / "slots"
    _cred(root / "slot1" / ".claude" / ".credentials.json", expires_in=-20 * 3600 + time.time() - NOW)
    _cred(root / "slot2" / ".claude" / ".credentials.json", expires_in=3600 + time.time() - NOW)
    monkeypatch.setattr(account_login, "CLAUDE_SLOT_ROOT", root)
    monkeypatch.setattr(account_login, "CODEX_ACCOUNTS_ROOT", tmp_path / "codex")
    monkeypatch.setattr(account_login, "KEEPER_LOG", tmp_path / "none.log")

    by = {b["target"]: b for b in account_login.bindings()}
    assert by["claude:1"]["needs_login"] is True
    assert by["claude:1"]["reason"] == "token_expired"
    assert by["claude:1"]["expires_at"]
    assert by["claude:1"]["bound"] is True          # 갱신 토큰은 있다
    assert by["claude:2"]["needs_login"] is False
    assert by["claude:2"]["reason"] is None


def test_keeper_failure_streak(tmp_path):
    log = tmp_path / "k.log"
    log.write_text("\n".join([
        "2026-10-15 06:00:00 slot1: 만료까지 -5분 (<= 45) — 갱신",
        "2026-10-15 06:00:06   갱신됨: -5분 → 480분",
        "2026-10-15 06:10:00 slot1: 만료까지 -5분 (<= 45) — 갱신",
        "2026-10-15 06:10:06   ⚠️ 갱신 실패 (-5분 → -5분) — 다음 주기에 다시 시도한다",
        "2026-10-15 06:20:00 slot1: 만료까지 -15분 (<= 45) — 갱신",
        "2026-10-15 06:20:06   ⚠️ 갱신 실패 (-15분 → -15분) — 다음 주기에 다시 시도한다",
        "2026-10-15 06:20:07 slot2: 만료까지 400분 — 건너뜀",
    ]))
    assert account_login.keeper_refresh_failures("1", log) == 2
    assert account_login.keeper_refresh_failures("2", log) == 0
    assert account_login.keeper_refresh_failures("1", tmp_path / "missing.log") == 0


def test_overview_detail_label_kst():
    detail = llm_keys._needs_login_detail({
        "needs_login": True, "reason": "token_expired",
        "expires_at": "2026-10-14T09:00:00+00:00", "refresh_failures": 3,
    })
    assert detail["needs_login_reason"] == "token_expired"
    assert detail["needs_login_label"] == "재로그인 필요(토큰 만료 10/14 18:00)"
    assert detail["refresh_failures"] == 3
    ok = llm_keys._needs_login_detail({"needs_login": False, "reason": None})
    assert ok["needs_login_label"] is None and ok["needs_login_reason"] is None


def _binding(slot, needs_login=False, reason=None, expires_at=None):
    return {"target": f"claude:{slot}", "kind": "claude", "needs_login": needs_login,
            "reason": reason, "expires_at": expires_at, "refresh_failures": 2 if needs_login else 0}


def test_expiry_watch_covers_all_active_slots_and_alerts_once(monkeypatch):
    health._expiry_state.clear()
    health._expiry_alerted.clear()
    sent = []

    async def _notify(title, message="", **kw):
        sent.append(title)
        return True

    monkeypatch.setattr("app.services.ohvis_alert.notify", _notify)
    bindings = [
        _binding(1, True, "token_expired", "2026-10-14T09:00:00+00:00"),
        _binding(2),
        _binding(3, True, "refresh_missing"),
        _binding(4, True, "token_expired", "2026-10-14T09:00:00+00:00"),  # 꺼진 슬롯
    ]
    seen = asyncio.run(health.check_credential_expiry(bindings, disabled_slots={"4"}))
    assert set(seen) == {"1", "2", "3"}
    assert len(sent) == 2 and "슬롯 1" in sent[0] and "슬롯 3" in sent[1]

    asyncio.run(health.check_credential_expiry(bindings, disabled_slots={"4"}))
    assert len(sent) == 2, "같은 만료를 매 회차 다시 알리고 있다"

    # 회복 후 다시 만료되면 새로 알린다
    asyncio.run(health.check_credential_expiry([_binding(1), _binding(2), _binding(3)], disabled_slots=set()))
    asyncio.run(health.check_credential_expiry(bindings[:1], disabled_slots=set()))
    assert len(sent) == 3


def test_poller_runs_expiry_check():
    import inspect
    assert "check_credential_expiry" in inspect.getsource(health.slot_token_health_poller)
