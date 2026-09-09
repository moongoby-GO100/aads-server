from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import anthropic_client


class _RawResponse:
    def __init__(self):
        self.headers = {}

    def parse(self):
        return SimpleNamespace(
            content=[SimpleNamespace(text="ok")],
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


class _Client:
    def __init__(self):
        create = AsyncMock(return_value=_RawResponse())
        self.messages = SimpleNamespace(with_raw_response=SimpleNamespace(create=create))


@pytest.mark.asyncio
async def test_text_call_uses_db_filtered_oauth_tokens(monkeypatch):
    token_loader = AsyncMock(return_value=["healthy-token"])
    client = _Client()
    create_client = lambda token=None: client

    monkeypatch.setattr(anthropic_client, "get_oauth_tokens_async", token_loader)
    monkeypatch.setattr(anthropic_client, "create_anthropic_client", create_client)
    monkeypatch.setattr("app.services.oauth_usage_tracker.log_usage", lambda **kwargs: None)

    result = await anthropic_client.call_llm_with_fallback("ping", max_tokens=4)

    assert result == "ok"
    token_loader.assert_awaited_once_with()
    client.messages.with_raw_response.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_messages_call_uses_db_filtered_oauth_tokens(monkeypatch):
    token_loader = AsyncMock(return_value=["healthy-token"])
    client = _Client()
    create_client = lambda token=None: client

    monkeypatch.setattr(anthropic_client, "get_oauth_tokens_async", token_loader)
    monkeypatch.setattr(anthropic_client, "create_anthropic_client", create_client)
    monkeypatch.setattr("app.services.oauth_usage_tracker.log_usage", lambda **kwargs: None)

    response = await anthropic_client.call_llm_messages_with_fallback(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        messages=[{"role": "user", "content": "ping"}],
    )

    assert response.content[0].text == "ok"
    token_loader.assert_awaited_once_with()
    client.messages.with_raw_response.create.assert_awaited_once()
