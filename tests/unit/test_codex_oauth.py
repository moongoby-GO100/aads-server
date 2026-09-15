"""app/core/codex_oauth.py 단위 테스트 — 네트워크를 타지 않는다.

Codex 호출 자체는 ChatGPT 구독 OAuth 를 쓰므로 CI 에서 재현할 수 없다.
그래서 여기서는 재현 가능한 두 가지만 본다.

1. SSE 파싱 — 실제 응답에서 잘라온 본문을 그대로 넣는다.
2. 자격증명이 없을 때 조용히 None 을 돌려주지 않고 CodexAuthError 로 터지는가.
   조용히 넘어가면 "Codex 가 왜 안 붙지" 를 호출부에서 다시 추적하게 된다.
"""
from __future__ import annotations

import json

import pytest

from app.core import codex_oauth


# 2026-09-15 실제 응답에서 잘라온 형태 (토큰값·id 는 지웠다)
SAMPLE_SSE = "\n".join(
    [
        'data: {"type":"response.created","response":{"id":"resp_x"}}',
        'data: {"type":"response.output_text.delta","delta":"AADS_"}',
        "",
        "이 줄은 data: 접두어가 없으므로 무시돼야 한다",
        'data: {"type":"response.output_text.delta","delta":"CODEX_OK"}',
        "data: {깨진 JSON",
        "data: [DONE]",
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":20,'
        '"output_tokens":9,"total_tokens":29}}}',
    ]
)


def test_parse_sse_text_추출():
    text, usage = codex_oauth.parse_sse_text(SAMPLE_SSE)
    assert text == "AADS_CODEX_OK"
    assert usage["total_tokens"] == 29
    assert usage["output_tokens"] == 9


def test_parse_sse_text_빈본문():
    text, usage = codex_oauth.parse_sse_text("")
    assert text == ""
    assert usage == {}


def test_parse_sse_text_깨진줄만():
    """깨진 JSON 만 들어와도 예외 없이 빈 결과를 돌려준다."""
    text, usage = codex_oauth.parse_sse_text("data: {아무것도\ndata: \n")
    assert text == ""
    assert usage == {}


def test_허용모델_기본값():
    """ChatGPT 계정 Codex 는 gpt-5.6-sol 만 200 이다 (2026-09-15 실측)."""
    assert codex_oauth.DEFAULT_MODEL == "gpt-5.6-sol"


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *_args, **_kwargs):
        return self._row


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc):
        return False


class _FakePool:
    def __init__(self, row):
        self._conn = _FakeConn(row)

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.mark.asyncio
async def test_자격증명_없으면_에러(monkeypatch):
    async def fake_pool():
        return _FakePool(None)

    monkeypatch.setattr(codex_oauth, "get_pool", fake_pool)
    with pytest.raises(codex_oauth.CodexAuthError) as exc:
        await codex_oauth.load_codex_config("NO_SUCH_KEY")
    assert "NO_SUCH_KEY" in str(exc.value)


@pytest.mark.asyncio
async def test_자격증명_필드누락시_에러(monkeypatch):
    """refresh_token 이 빠진 채 등록돼 있으면 호출 전에 걸러야 한다."""
    partial = json.dumps({"client_id": "c", "account_id": "a"})

    async def fake_pool():
        return _FakePool({"encrypted_value": "ENC"})

    monkeypatch.setattr(codex_oauth, "get_pool", fake_pool)
    monkeypatch.setattr(codex_oauth, "decrypt_value", lambda _v: partial)
    with pytest.raises(codex_oauth.CodexAuthError) as exc:
        await codex_oauth.load_codex_config()
    assert "refresh_token" in str(exc.value)
