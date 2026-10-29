"""CEO 텔레그램 설정 환경변수 호환성 테스트."""
import asyncio

from app.services import ceo_notify


def test_telegram_environment_variables_are_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fallback-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fallback-chat")
    for name in (
        "AADS_TELEGRAM_BOT_TOKEN",
        "GO100_TELEGRAM_BOT_TOKEN",
        "AADS_TELEGRAM_CHAT_ID",
        "GO100_TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    assert ceo_notify._get_telegram_token() == "fallback-token"
    assert ceo_notify._get_telegram_chat_id() == "fallback-chat"


def test_aads_environment_variables_take_priority(monkeypatch):
    monkeypatch.setenv("AADS_TELEGRAM_BOT_TOKEN", "aads-token")
    monkeypatch.setenv("GO100_TELEGRAM_BOT_TOKEN", "go100-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fallback-token")
    monkeypatch.setenv("AADS_TELEGRAM_CHAT_ID", "aads-chat")
    monkeypatch.setenv("GO100_TELEGRAM_CHAT_ID", "go100-chat")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fallback-chat")

    assert ceo_notify._get_telegram_token() == "aads-token"
    assert ceo_notify._get_telegram_chat_id() == "aads-chat"


def test_send_ceo_text_returns_false_without_configuration(monkeypatch):
    for name in (
        "AADS_TELEGRAM_BOT_TOKEN",
        "GO100_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "AADS_TELEGRAM_CHAT_ID",
        "GO100_TELEGRAM_CHAT_ID",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    async def unexpected_network_call(*args, **kwargs):
        raise AssertionError("network sender must not be called")

    monkeypatch.setattr(ceo_notify, "_send_telegram_message", unexpected_network_call)
    assert asyncio.run(ceo_notify.send_ceo_text("test")) is False
