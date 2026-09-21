"""browser_navigate 가 호출마다 탭을 새로 열지 않는지 검사한다.

2026-09-17, 한 턴에서 browser_navigate 가 210s 타임아웃으로 여러 번 실패했고
그때마다 빈 탭이 남았다. 연 탭을 닫는 경로가 코드 어디에도 없었기 때문이다.
이 테스트가 없으면 같은 회귀가 조용히 되돌아온다.
"""
import asyncio

import app.api.ceo_chat_tools as cct


class FakePage:
    def __init__(self, fail_goto: bool = False):
        self._closed = False
        self.fail_goto = fail_goto
        self.goto_calls: list[str] = []
        self.url = "https://aads.newtalk.kr/"

    def is_closed(self) -> bool:
        return self._closed

    async def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        if self.fail_goto:
            raise RuntimeError("Timeout 60000ms exceeded")

    async def close(self):
        self._closed = True

    async def title(self):
        return "AADS"


class FakeContext:
    def __init__(self, pages=(), fail_goto: bool = False):
        self._pages = list(pages)
        self.new_page_calls = 0
        self.fail_goto = fail_goto

    @property
    def pages(self):
        return list(self._pages)

    async def new_page(self):
        self.new_page_calls += 1
        page = FakePage(fail_goto=self.fail_goto)
        self._pages.append(page)
        return page


def _run(ctx, monkeypatch):
    async def _fake_acquire(*_args, **_kwargs):
        return ctx, None

    monkeypatch.setattr(cct, "_acquire_pw_context", _fake_acquire)
    monkeypatch.setattr(cct, "_browser_domain_ok", lambda _url: None)
    return asyncio.run(cct.tool_browser_navigate("https://aads.newtalk.kr/"))


def test_existing_tab_is_reused(monkeypatch):
    """살아 있는 탭이 있으면 새 탭을 열지 않는다."""
    existing = FakePage()
    ctx = FakeContext(pages=[existing])

    result = _run(ctx, monkeypatch)

    assert ctx.new_page_calls == 0
    assert existing.goto_calls == ["https://aads.newtalk.kr/"]
    assert result.startswith("[탐색 완료]")


def test_tabs_over_limit_are_closed(monkeypatch):
    """상한을 넘게 쌓인 옛 탭은 정리한다."""
    pages = [FakePage() for _ in range(5)]
    ctx = FakeContext(pages=pages)

    _run(ctx, monkeypatch)

    assert ctx.new_page_calls == 0
    # 최신 _BROWSER_MAX_TABS 개만 남는다.
    assert [p.is_closed() for p in pages] == [True, True, False, False, False]
    assert sum(1 for p in pages if not p.is_closed()) == cct._BROWSER_MAX_TABS


def test_failed_navigation_does_not_leak_tab(monkeypatch):
    """탐색이 실패하면 이번에 연 탭을 닫는다."""
    ctx = FakeContext(pages=[], fail_goto=True)

    result = _run(ctx, monkeypatch)

    assert ctx.new_page_calls == 1
    assert ctx.pages[0].is_closed() is True
    assert result.startswith("[ERROR]")


def test_closed_tabs_are_not_reused(monkeypatch):
    """이미 닫힌 탭만 남아 있으면 새로 연다."""
    dead = FakePage()
    dead._closed = True
    ctx = FakeContext(pages=[dead])

    _run(ctx, monkeypatch)

    assert ctx.new_page_calls == 1
