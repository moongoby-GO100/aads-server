"""codex_oauth 의 refresh_token 회전 저장 + codex_auth_sync.sh DB 사본 감시 회귀 테스트.

네트워크·실DB 를 타지 않는다. 토큰 엔드포인트는 httpx.AsyncClient 를 가짜로 바꾸고
llm_api_keys 는 메모리 가짜 커넥션으로 대신한다 (실제 OpenAI 호출은 회전을 일으켜
정본을 무효화하므로 금지).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

import pytest

from app.core import codex_oauth

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SH = REPO_ROOT / "scripts" / "codex_auth_sync.sh"
KEY = "CODEX_OAUTH_TEST"


def _base_cfg(refresh="rt-old"):
    return {
        "client_id": "c",
        "account_id": "a",
        "refresh_token": refresh,
        "token_endpoint": "https://example.invalid/oauth/token",
        "responses_endpoint": "https://example.invalid/responses",
    }


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeConn:
    def __init__(self, db_cfg, fail_update=False):
        self.db_cfg = db_cfg
        self.fail_update = fail_update
        self.updates: list[tuple] = []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *_args):
        assert "FOR UPDATE" in sql
        return {"encrypted_value": "ENC:" + json.dumps(self.db_cfg)}

    async def execute(self, sql, *args):
        assert "UPDATE llm_api_keys" in sql and "provider = 'codex'" in sql
        if self.fail_update:
            raise RuntimeError("db down")
        self.updates.append(args)
        self.db_cfg = json.loads(args[1][len("ENC:"):])
        return "UPDATE 1"


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _Resp:
    def __init__(self, status, body, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        return self._body


class _FakeHttp:
    """httpx.AsyncClient 대역 — POST 호출 수를 센다."""

    posts: list[dict] = []
    responder = None

    def __init__(self, *_a, **_k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None, **_k):  # noqa: A002
        await asyncio.sleep(0.01)  # 동시 호출이 겹치도록 양보
        type(self).posts.append(json)
        return type(self).responder(json)


@pytest.fixture
def env(monkeypatch):
    codex_oauth._TOKEN_CACHE.clear()
    codex_oauth._REFRESH_LOCKS.clear()
    _FakeHttp.posts = []
    _FakeHttp.responder = lambda _p: _Resp(
        200, {"access_token": "at-1", "refresh_token": "rt-new", "expires_in": 3600}
    )
    conn = _FakeConn(_base_cfg())

    async def fake_pool():
        return _FakePool(conn)

    monkeypatch.setattr(codex_oauth, "get_pool", fake_pool)
    monkeypatch.setattr(codex_oauth, "decrypt_value", lambda v: v[len("ENC:"):])
    monkeypatch.setattr(codex_oauth, "encrypt_value", lambda v: "ENC:" + v)
    monkeypatch.setattr(codex_oauth.httpx, "AsyncClient", _FakeHttp)
    yield conn
    codex_oauth._TOKEN_CACHE.clear()
    codex_oauth._REFRESH_LOCKS.clear()


@pytest.mark.asyncio
async def test_회전된_refresh_token_UPDATE_1회(env):
    cfg = _base_cfg()
    token = await codex_oauth.get_access_token(cfg, key_name=KEY)
    assert token == "at-1"
    assert len(env.updates) == 1
    assert env.updates[0][0] == KEY
    saved = json.loads(env.updates[0][1][len("ENC:"):])
    assert saved["refresh_token"] == "rt-new"
    assert saved["account_id"] == "a"  # 다른 필드는 보존
    assert cfg["refresh_token"] == "rt-new"


@pytest.mark.asyncio
async def test_같은_토큰이면_UPDATE_없음(env):
    _FakeHttp.responder = lambda _p: _Resp(
        200, {"access_token": "at-1", "refresh_token": "rt-old", "expires_in": 3600}
    )
    await codex_oauth.get_access_token(_base_cfg(), key_name=KEY)
    assert env.updates == []


@pytest.mark.asyncio
async def test_응답에_refresh_token_없으면_UPDATE_없음(env):
    _FakeHttp.responder = lambda _p: _Resp(200, {"access_token": "at-1"})
    await codex_oauth.get_access_token(_base_cfg(), key_name=KEY)
    assert env.updates == []


@pytest.mark.asyncio
async def test_동시_2개_호출은_POST_1회(env):
    cfg1, cfg2 = _base_cfg(), _base_cfg()
    t1, t2 = await asyncio.gather(
        codex_oauth.get_access_token(cfg1, key_name=KEY),
        codex_oauth.get_access_token(cfg2, key_name=KEY),
    )
    assert t1 == t2 == "at-1"
    assert len(_FakeHttp.posts) == 1
    assert len(env.updates) == 1


@pytest.mark.asyncio
async def test_다른_워커가_이미_회전했으면_DB_값으로_진행(env):
    env.db_cfg["refresh_token"] = "rt-rotated-by-other"
    await codex_oauth.get_access_token(_base_cfg("rt-stale"), key_name=KEY)
    assert _FakeHttp.posts[0]["refresh_token"] == "rt-rotated-by-other"


@pytest.mark.asyncio
async def test_401_invalidated_재로그인_안내_재시도_없음(env):
    _FakeHttp.responder = lambda _p: _Resp(
        401, {}, text='{"error":{"code":"refresh_token_invalidated"}}'
    )
    with pytest.raises(codex_oauth.CodexAuthError) as exc:
        await codex_oauth.get_access_token(_base_cfg(), key_name=KEY)
    assert "재로그인 필요" in str(exc.value)
    assert KEY in str(exc.value)
    assert len(_FakeHttp.posts) == 1
    assert env.updates == []


@pytest.mark.asyncio
async def test_401_token_revoked_도_재로그인_안내(env):
    _FakeHttp.responder = lambda _p: _Resp(401, {}, text="token_revoked")
    with pytest.raises(codex_oauth.CodexAuthError) as exc:
        await codex_oauth.get_access_token(_base_cfg(), key_name=KEY)
    assert "재로그인 필요" in str(exc.value)
    assert len(_FakeHttp.posts) == 1


@pytest.mark.asyncio
async def test_저장_실패는_error_로그_access_token_은_반환(env, caplog):
    env.fail_update = True
    with caplog.at_level(logging.ERROR, logger=codex_oauth.logger.name):
        token = await codex_oauth.get_access_token(_base_cfg(), key_name=KEY)
    assert token == "at-1"
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errs and "401" in errs[0].getMessage() and KEY in errs[0].getMessage()
    assert "rt-new" not in caplog.text  # 토큰 값은 로그에 남기지 않는다


def _func_body(src: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}\n", src, re.S | re.M)
    assert m, name
    return m.group(1)


def test_sync_sh_구문_검사():
    r = subprocess.run(["bash", "-n", str(SYNC_SH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_sync_sh_DB사본_감시는_토큰을_읽거나_출력하지_않는다():
    src = SYNC_SH.read_text(encoding="utf-8")
    body = _func_body(src, "check_db_copies")
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "provider = 'codex'" in code and "is_active" in code
    assert "encrypted_value" not in code
    assert not re.search(r"refresh_token|access_token|id_token", code)
    assert "codex_token_refresh" not in code and "token_endpoint" not in code
    assert "send_telegram" in code and "DB 사본 위험" in code
