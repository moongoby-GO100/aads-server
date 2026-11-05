"""/ops/server-health 는 SSH 프로브 동안 이벤트 루프를 막지 않는다 (2026-11-05 채팅 지연 원인)."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.api import ops


def _proc(stdout="", rc=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=rc, stderr=stderr)


@pytest.fixture(autouse=True)
def _clear_cache():
    ops._server_health_cache.clear()
    ops._server_health_inflight.clear()
    yield
    ops._server_health_cache.clear()
    ops._server_health_inflight.clear()


def _slow_probe(calls):
    def probe(cfg, command, timeout=8):
        calls.append(command)
        time.sleep(0.4)  # 실제 subprocess 처럼 스레드를 막는다
        if command.startswith("curl"):
            return _proc('{"status": "ok", "extra_disks": []}')
        return _proc("", 1, "x")
    return probe


def test_probe_does_not_block_event_loop(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ops, "_run_server_probe", _slow_probe(calls))

    async def main():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.02)

        t = asyncio.create_task(ticker())
        payload = await ops.remote_server_health("cafe24_114")
        stop = True
        await t
        return payload, ticks

    payload, ticks = asyncio.run(main())
    assert payload["status"] == "ok"
    # 0.4초 프로브 동안 루프가 살아 있어야 한다(막히면 1~2회).
    assert ticks >= 8, ticks


def test_concurrent_and_repeat_calls_share_one_probe(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ops, "_run_server_probe", _slow_probe(calls))

    async def main():
        first = await asyncio.gather(*(ops.remote_server_health("cafe24_114") for _ in range(5)))
        again = await ops.remote_server_health("cafe24_114")
        return first, again

    first, again = asyncio.run(main())
    assert all(p["status"] == "ok" for p in first) and again["status"] == "ok"
    assert calls == ["curl -sf --max-time 3 http://localhost:%s/health" % ops._remote_health_config("cafe24_114")["port"]]


def test_unknown_server_still_404():
    with pytest.raises(ops.HTTPException) as exc:
        asyncio.run(ops.remote_server_health("nope"))
    assert exc.value.status_code == 404
