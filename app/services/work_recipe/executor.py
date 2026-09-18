"""Work Recipe 단계를 Browser Bridge에서 실행하는 어댑터."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.browser_bridge.aads_adapter import acquire_browser_context
from app.browser_bridge.service import BrowserBridgeError, get_browser_bridge_service
from app.services.work_recipe.audit import mask_secrets
from app.services.work_recipe.guard import (
    assert_not_page_derived,
    classify_step,
    requires_approval,
)
from app.services.work_recipe.schema import ALLOWED_ACTIONS


class BrowserRecipeExecutor:
    """player의 step payload를 Playwright 호환 Browser Bridge로 옮긴다."""

    def __init__(
        self,
        *,
        browser_session_id: str | None = None,
        browser_work_key: str | None = None,
        page_texts: list[str] | None = None,
    ) -> None:
        self.browser_session_id = browser_session_id
        self.browser_work_key = browser_work_key
        self.page_texts = list(page_texts or [])
        self._context: Any = None
        self._page: Any = None
        # 서비스 초기화/설정 오류를 첫 단계보다 앞에서 드러낸다.
        self._service = get_browser_bridge_service()

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        if action not in ALLOWED_ACTIONS:
            return {"ok": False, "error": f"지원하지 않는 브라우저 action: {action}"}

        try:
            level = classify_step(payload, domain=self._domain(payload))
            # 승인 집행은 orchestrator의 GuardedRunRecorder가 담당한다. 여기서도
            # 반드시 같은 판정을 수행해 우회 실행 경로가 정책을 건너뛰지 않게 한다.
            requires_approval(level)
            self._assert_trusted_arguments(payload)
            page = await self._get_page(payload)
            output = await self._execute(page, action, payload)
            return {"ok": True, "output": output, "risk": level.value, "llm_calls": 0}
        except BrowserBridgeError as exc:
            return {
                "ok": False,
                "error": mask_secrets(f"Browser Bridge 사용 불가: {exc}"),
            }
        except Exception as exc:  # noqa: BLE001 - player 계약상 단계 실패를 결과로 변환
            return {"ok": False, "error": mask_secrets(f"{type(exc).__name__}: {exc}")}

    @staticmethod
    def _domain(payload: Mapping[str, Any]) -> str:
        context = payload.get("context")
        return str(context.get("domain") or "") if isinstance(context, Mapping) else ""

    def _assert_trusted_arguments(self, payload: Mapping[str, Any]) -> None:
        arguments = {
            key: payload.get(key)
            for key in ("url", "selector", "value", "endpoint")
            if payload.get(key) is not None
        }
        context = payload.get("context")
        page_texts = list(self.page_texts)
        if isinstance(context, Mapping):
            supplied = context.get("page_texts")
            if isinstance(supplied, str):
                page_texts.append(supplied)
            elif isinstance(supplied, (list, tuple)):
                page_texts.extend(str(item) for item in supplied)
        assert_not_page_derived(arguments, page_texts, field="step")

    async def _get_page(self, payload: Mapping[str, Any]) -> Any:
        if self._page is not None:
            return self._page
        self._context, error = await acquire_browser_context(
            browser_session_id=self.browser_session_id,
            browser_work_key=self.browser_work_key,
            url=str(payload.get("url") or "about:blank"),
        )
        if self._context is None:
            raise BrowserBridgeError(error or "브라우저 컨텍스트를 확보하지 못했습니다")
        pages = list(getattr(self._context, "pages", []) or [])
        self._page = pages[0] if pages else await self._context.new_page()
        return self._page

    async def _execute(self, page: Any, action: str, step: Mapping[str, Any]) -> Any:
        selector = str(step.get("selector") or "")
        timeout = float(step.get("timeout") or 30.0)
        timeout_ms = int(timeout * 1000)

        if action == "navigate":
            await page.goto(str(step.get("url") or ""), timeout=timeout_ms)
            return {"url": str(getattr(page, "url", step.get("url") or ""))}
        if action == "click":
            await page.click(selector, timeout=timeout_ms)
            return None
        if action == "fill":
            await page.fill(selector, str(step.get("value") or ""), timeout=timeout_ms)
            return None
        if action == "select":
            await page.select_option(selector, step.get("value"), timeout=timeout_ms)
            return None
        if action == "press":
            key = str(step.get("value") or step.get("key") or "Enter")
            if hasattr(page, "press"):
                await page.press(selector, key, timeout=timeout_ms)
            else:
                await page.press_key(key, selector=selector)
            return None
        if action == "upload":
            await page.set_input_files(selector, step.get("value"), timeout=timeout_ms)
            return None
        if action == "download":
            if hasattr(page, "download"):
                return await page.download(
                    selector,
                    download_dir=str(step.get("value") or ""),
                    timeout_seconds=timeout,
                )
            async with page.expect_download(timeout=timeout_ms) as pending:
                await page.click(selector, timeout=timeout_ms)
            download = await pending.value
            return {"suggested_filename": download.suggested_filename}
        if action == "snapshot":
            target = page.locator(selector) if selector else page.locator("body")
            if hasattr(target, "aria_snapshot"):
                return await target.aria_snapshot()
            return await target.inner_text(timeout=timeout_ms)
        if action == "api_call":
            endpoint = str(step.get("endpoint") or step.get("url") or "")
            options = step.get("value") if isinstance(step.get("value"), Mapping) else {}
            return await page.evaluate(
                "async ({endpoint, options}) => { const response = await fetch(endpoint, options); "
                "const text = await response.text(); return {status: response.status, ok: response.ok, text}; }",
                {"endpoint": endpoint, "options": dict(options)},
                timeout=timeout_ms,
            )
        raise ValueError(f"지원하지 않는 브라우저 action: {action}")


async def execute_step(payload: dict[str, Any]) -> dict[str, Any]:
    """단일 단계용 편의 함수."""
    return await BrowserRecipeExecutor()(payload)
