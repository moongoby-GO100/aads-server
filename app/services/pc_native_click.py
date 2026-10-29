"""Shared single PC Agent click; never types or retries."""
import asyncio
from typing import Any


async def _evaluate(page, expression, *args, timeout_ms=10000):
    return await asyncio.wait_for(page.evaluate(expression, *args), timeout_ms / 1000)


async def element_click_point(page: Any, selector: str, *, evaluate=None, safe_error=None) -> dict[str, str]:
    """Return a best-effort absolute screen coordinate for a visible element."""
    evaluate = evaluate or _evaluate
    safe_error = safe_error or (lambda exc: {"error_type": type(exc).__name__})
    selector = str(selector or "").strip()
    if not selector:
        return {"resolved": "0", "reason": "missing_selector"}
    try:
        raw = await evaluate(
            page,
            """
            (selector) => {
              const el = document.querySelector(selector);
              if (!el || el.disabled) return {resolved: '0', reason: 'element_missing_or_disabled'};
              const rect = el.getBoundingClientRect();
              if (!rect || rect.width <= 0 || rect.height <= 0) {
                return {resolved: '0', reason: 'element_not_visible'};
              }
              const chromeTop = Math.max(80, Number(window.outerHeight || 0) - Number(window.innerHeight || 0));
              const chromeLeft = Math.max(0, Number(window.outerWidth || 0) - Number(window.innerWidth || 0)) / 2;
              const x = Math.round(Number(window.screenX || 0) + chromeLeft + rect.left + rect.width / 2);
              const y = Math.round(Number(window.screenY || 0) + chromeTop + rect.top + rect.height / 2);
              try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}
              try { el.focus(); } catch (_) {}
              return {
                resolved: '1',
                x: String(x),
                y: String(y),
                width: String(Math.round(rect.width)),
                height: String(Math.round(rect.height)),
                active: document.activeElement === el ? '1' : '0'
              };
            }
            """,
            selector,
            timeout_ms=10000,
        )
    except Exception as exc:
        return {"resolved": "failed", **safe_error(exc)}
    if not isinstance(raw, dict):
        return {"resolved": "0", "reason": "bad_coordinate_payload"}
    result: dict[str, str] = {}
    for key in ("resolved", "x", "y", "width", "height", "active", "reason", "error_code", "error_type"):
        value = str(raw.get(key) or "").strip()
        if value:
            result[key] = value[:120]
    return result or {"resolved": "0"}


async def click_point(page, point, *, title, require_focus=False):
    runner = page._run_browser_command
    result = {}
    try:
        await runner(
            "window_focus",
            {"title": title},
            command_timeout_seconds=8.0,
            queue_wait_timeout_seconds=5.0,
        )
        result["window_focused"] = "1"
    except Exception:
        result["window_focused"] = "0"

    if require_focus and result["window_focused"] != "1":
        return {**result, "mouse_clicked": "0"}
    if point.get("resolved") == "1":
        try:
            await runner(
                "mouse_click",
                {"x": int(point.get("x") or 0), "y": int(point.get("y") or 0), "button": "left", "clicks": 1},
                command_timeout_seconds=8.0,
                queue_wait_timeout_seconds=5.0,
            )
            result["mouse_clicked"] = "1"
        except Exception:
            result["mouse_clicked"] = "0"
    else:
        result["mouse_clicked"] = "0"

    return result
