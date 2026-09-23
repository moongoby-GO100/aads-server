import asyncio
import json
import sys
import types

import pytest

from app.services.e2e_verify import (
    assert_screen_evidence_gate,
    evidence_passes_gate,
    run_e2e_verify,
    screen_verification_required,
)


class _Conn:
    def __init__(self, metadata=None):
        self.metadata = metadata

    async def fetchrow(self, query, job_id):
        assert "e2e_evidence" in query
        return {"metadata": self.metadata} if self.metadata is not None else None


class _Locator:
    async def count(self):
        return 1


class _DbConn:
    async def execute(self, *args):
        return "INSERT 0 1"


class _Acquire:
    async def __aenter__(self):
        return _DbConn()

    async def __aexit__(self, *args):
        return None


class _Pool:
    def acquire(self):
        return _Acquire()


async def _empty_credentials(**kwargs):
    return []


async def _http_probe(url):
    return {"success": True, "status_code": 200, "final_url": url, "tool": "test"}


def _patch_e2e_modules(monkeypatch, *, acquire, capture):
    monkeypatch.setitem(
        sys.modules,
        "app.api.ceo_chat_tools",
        types.SimpleNamespace(
            _browser_domain_ok=lambda url: None,
            _acquire_pw_context=acquire,
            _pre_inject_vault_token=None,
            tool_capture_screenshot=capture,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.core.credential_vault",
        types.SimpleNamespace(list_credentials=_empty_credentials),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.agent_vault_service",
        types.SimpleNamespace(list_agent_credentials=_empty_credentials, normalize_origin=lambda url: url),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.core.db_pool",
        types.SimpleNamespace(get_pool=lambda: _Pool()),
    )


def _evidence(*, passed=True):
    return {
        "schema": "aads.e2e_verify.v1",
        "passed": passed,
        "stages": {
            "dom_assertion": {"passed": passed},
            "screenshot": {"success": passed},
        },
    }


def test_screen_gate_classifies_ui_but_not_backend_work():
    assert screen_verification_required("로그인 필요 화면 Visual QA")
    assert screen_verification_required("component update", ["frontend/components/Login.tsx"])
    assert not screen_verification_required("DB 인덱스와 백엔드 API 수정", ["app/services/report.py"])


def test_evidence_contract_requires_dom_and_capture_success():
    assert evidence_passes_gate(_evidence())
    assert not evidence_passes_gate(_evidence(passed=False))
    assert not evidence_passes_gate({"passed": True, "stages": {}})


def test_missing_evidence_blocks_screen_job_completion_and_approval():
    with pytest.raises(ValueError, match="screen_e2e_evidence_required"):
        asyncio.run(
            assert_screen_evidence_gate(
                _Conn(), job_id="runner-screen", instruction="UI 변경 및 스크린샷 검수", changed_files=[]
            )
        )


def test_passing_evidence_allows_screen_job():
    asyncio.run(
        assert_screen_evidence_gate(
            _Conn({"evidence": json.dumps(_evidence())}),
            job_id="runner-screen",
            instruction="로그인 화면 변경",
            changed_files=[],
        )
    )


def test_backend_job_keeps_existing_flow_without_evidence():
    asyncio.run(
        assert_screen_evidence_gate(
            _Conn(), job_id="runner-api", instruction="백엔드 API 수정", changed_files=["app/services/api.py"]
        )
    )


def test_default_e2e_capture_keeps_server_playwright_route(monkeypatch):
    capture_work_keys = []

    class _Page:
        url = "https://aads.newtalk.kr/chat"

        async def goto(self, url, **kwargs):
            self.url = url

        def locator(self, selector):
            return _Locator()

        async def close(self):
            return None

    class _Context:
        async def new_page(self):
            return _Page()

    async def _acquire(session_id, work_key, url):
        assert session_id == ""
        assert work_key == ""
        return _Context(), None

    async def _capture(url, full_page, **kwargs):
        capture_work_keys.append(kwargs["browser_work_key"])
        return "스크린샷 저장 완료. https://aads.newtalk.kr/screenshots/e2e.png"

    _patch_e2e_modules(monkeypatch, acquire=_acquire, capture=_capture)

    evidence = asyncio.run(
        run_e2e_verify(
            job_id="runner-screen",
            project="AADS",
            url="https://aads.newtalk.kr/chat",
            tenant_id="",
            selectors=["body"],
            http_probe=_http_probe,
        )
    )

    assert evidence["passed"] is True
    assert evidence["fallback_used"] is False
    assert capture_work_keys == [""]


def test_dom_assertion_waits_for_selector_before_final_count(monkeypatch):
    """Regression: the DOM assertion must await settle/selector waits before the
    final locator().count() check, not just read whatever is already in the DOM."""

    class _SettlingLocator:
        def __init__(self, state):
            self._state = state

        async def count(self):
            return 1 if self._state["waited"] else 0

    class _SettlingPage:
        url = "https://aads.newtalk.kr/chat"

        def __init__(self):
            self._state = {"waited": False}

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def wait_for_selector(self, selector, **kwargs):
            self._state["waited"] = True

        def locator(self, selector):
            return _SettlingLocator(self._state)

        async def close(self):
            return None

    class _SettlingContext:
        async def new_page(self):
            return _SettlingPage()

    async def _acquire(session_id, work_key, url):
        return _SettlingContext(), None

    async def _capture(url, full_page, **kwargs):
        return "스크린샷 저장 완료. https://aads.newtalk.kr/screenshots/e2e.png"

    _patch_e2e_modules(monkeypatch, acquire=_acquire, capture=_capture)

    evidence = asyncio.run(
        run_e2e_verify(
            job_id="runner-screen",
            project="AADS",
            url="https://aads.newtalk.kr/chat",
            tenant_id="",
            selectors=["body"],
            http_probe=_http_probe,
        )
    )

    assert evidence["stages"]["dom_assertion"]["passed"] is True


def test_dom_assertion_shares_timeout_budget_across_selectors(monkeypatch):
    import app.services.e2e_verify as e2e_verify_module

    monkeypatch.setattr(e2e_verify_module, "_DOM_ASSERTION_BUDGET_SECONDS", 0.05)

    class _BudgetLocator:
        async def count(self):
            return 0

    class _BudgetPage:
        url = "https://aads.newtalk.kr/chat"

        def __init__(self):
            self.wait_timeouts = []

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def wait_for_selector(self, selector, timeout=None, **kwargs):
            self.wait_timeouts.append(timeout)
            await asyncio.sleep(timeout / 1000)
            raise TimeoutError("selector_not_found")

        def locator(self, selector):
            return _BudgetLocator()

        async def close(self):
            return None

    pages = []

    class _BudgetContext:
        async def new_page(self):
            page = _BudgetPage()
            pages.append(page)
            return page

    async def _acquire(session_id, work_key, url):
        return _BudgetContext(), None

    async def _capture(url, full_page, **kwargs):
        return "스크린샷 저장 완료. https://aads.newtalk.kr/screenshots/e2e.png"

    _patch_e2e_modules(monkeypatch, acquire=_acquire, capture=_capture)

    asyncio.run(
        run_e2e_verify(
            job_id="runner-screen",
            project="AADS",
            url="https://aads.newtalk.kr/chat",
            tenant_id="",
            selectors=["body", ".header", "#footer"],
            http_probe=_http_probe,
        )
    )

    assert pages, "expected page to be created"
    timeouts = pages[0].wait_timeouts
    assert sum(timeouts) <= e2e_verify_module._DOM_ASSERTION_BUDGET_SECONDS * 1000
