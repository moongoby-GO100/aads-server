"""오비서 전용 앱의 기동 계약.

이 앱은 `app/main.py` 와 별도 프로세스로 뜬다. 기동 훅이 없으면
`app.core.db_pool.init_pool()` 이 호출되지 않아, 풀을 쓰는 경로가 실행 시점에
"DB pool이 초기화되지 않았습니다" 로 500 을 낸다(2026-09-23 실측:
`/api/v1/auth/login/e2e-inject`). 라우터는 등록돼 있으므로 경로 존재만
확인해서는 드러나지 않는다.
"""
from __future__ import annotations

import pytest

from app import yeoljeong_main


def test_app_has_lifespan_registered():
    assert yeoljeong_main.app.router.lifespan_context is not None


@pytest.mark.asyncio
async def test_lifespan_opens_and_closes_db_pool(monkeypatch):
    calls: list[str] = []

    async def fake_init_pool():
        calls.append("init")
        return object()

    async def fake_close_pool():
        calls.append("close")

    monkeypatch.setattr("app.core.db_pool.init_pool", fake_init_pool)
    monkeypatch.setattr("app.core.db_pool.close_pool", fake_close_pool)

    async with yeoljeong_main.lifespan(yeoljeong_main.app):
        assert calls == ["init"], "요청을 받기 전에 풀이 열려 있어야 한다"

    assert calls == ["init", "close"]


@pytest.mark.asyncio
async def test_init_failure_does_not_block_boot(monkeypatch):
    """풀 초기화가 실패해도 서비스는 떠야 한다. 업무 API 는 자체 커넥션을 쓴다."""

    async def failing_init_pool():
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다")

    async def fake_close_pool():
        return None

    monkeypatch.setattr("app.core.db_pool.init_pool", failing_init_pool)
    monkeypatch.setattr("app.core.db_pool.close_pool", fake_close_pool)

    async with yeoljeong_main.lifespan(yeoljeong_main.app):
        pass  # 예외가 새어 나오면 실패한다


@pytest.mark.asyncio
async def test_close_failure_does_not_mask_shutdown(monkeypatch):
    async def fake_init_pool():
        return object()

    async def failing_close_pool():
        raise RuntimeError("already closed")

    monkeypatch.setattr("app.core.db_pool.init_pool", fake_init_pool)
    monkeypatch.setattr("app.core.db_pool.close_pool", failing_close_pool)

    async with yeoljeong_main.lifespan(yeoljeong_main.app):
        pass
