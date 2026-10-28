"""서버 레인 사이트 공지 팝업 일괄 닫기 — DB 공용 목록(browser_site_overlays, approved)의 도메인만, '닫기' 버튼만 누른다.

겹쳐 뜬 공지 모달이 로그아웃 등 다른 클릭을 가린다(오류사전
smart_browser.server_click_obscured_by_site_modals). 가려진 요소를 누르지 않는 browser_click 은
그대로 두고, 실제 최상단 팝업의 '닫기'만 사람처럼 눌러 하나씩 치운다. 강제 클릭(force)은 쓰지 않는다.
쿠키·저장소는 건드리지 않는다.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.services.work_recipe.audit import mask_secrets
from app.services.work_recipe.executor import OverlayNotDismissible
from app.services.work_recipe.schema import (
    OVERLAY_FORBIDDEN_MARKERS,
    OVERLAY_SENSITIVE_MARKERS,
    SITE_POPUP_CLOSE_LABELS,
    SitePopupPolicy,
    clamp_site_popup_max_close,
)
from app.services.work_recipe.site_overlays import site_popup_policy_for_host

_REPORT_TEXT_LEN = 40
_GONE_POLL_SECONDS = 0.15
_GONE_POLL_ROUNDS = 8

_PROBE_SCRIPT = """(args) => {
    const isVisible = el => {
        if (!el || !el.isConnected) return false;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        if (typeof el.checkVisibility === 'function') {
            return el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
        }
        const cs = getComputedStyle(el);
        return cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity || '1') > 0;
    };
    const zOf = el => {
        let z = 0;
        for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
            const cs = getComputedStyle(n);
            const v = parseInt(cs.zIndex, 10);
            if (!Number.isNaN(v) && cs.position !== 'static') z = Math.max(z, v);
        }
        return z;
    };
    const labelOf = el => String(el.innerText || el.textContent || el.getAttribute('aria-label')
        || el.getAttribute('title') || el.value || '').replace(/\\s+/g, ' ').trim();
    const norm = s => String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const closePoint = (root) => {
        const buttons = Array.from(root.querySelectorAll('button, [role="button"], a, input[type="button"]'));
        let found = null;
        for (const btn of buttons) {
            const label = labelOf(btn);
            if (!args.labels.includes(norm(label)) || !isVisible(btn)) continue;
            const r = btn.getBoundingClientRect();
            const x = Math.round(r.left + r.width / 2);
            const y = Math.round(r.top + r.height / 2);
            const hit = document.elementFromPoint(x, y);
            const info = {label, x, y, hit: !!hit && (hit === btn || btn.contains(hit))};
            if (info.hit) return info;
            found = found || info;
        }
        return found;
    };
    const titleOf = root => {
        const aria = root.getAttribute('aria-label');
        if (aria) return aria.trim().slice(0, 80);
        const by = root.getAttribute('aria-labelledby');
        if (by) {
            const t = by.split(/\\s+/).map(id => (document.getElementById(id) || {}).textContent || '').join(' ').trim();
            if (t) return t.slice(0, 80);
        }
        const head = root.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"]');
        if (head && head.textContent.trim()) return head.textContent.trim().slice(0, 80);
        return String(root.innerText || root.textContent || '').trim().split('\\n')[0].slice(0, 80);
    };
    if (args.mode === 'gone') {
        const el = (window.__aadsSitePopups || [])[args.index];
        return {gone: !el || !isVisible(el)};
    }
    if (args.mode === 'repoint') {
        const el = (window.__aadsSitePopups || [])[args.index];
        return {close: el && isVisible(el) ? closePoint(el) : null};
    }
    if (args.mode === 'cleanup') {
        delete window.__aadsSitePopups;
        return {};
    }
    const roots = Array.from(document.querySelectorAll(args.selector)).filter(isVisible);
    const ranked = roots.map(root => ({root, z: zOf(root)})).sort((a, b) => {
        if (a.z !== b.z) return b.z - a.z;
        return (a.root.compareDocumentPosition(b.root) & Node.DOCUMENT_POSITION_FOLLOWING) ? 1 : -1;
    });
    window.__aadsSitePopups = ranked.map(item => item.root);
    return {
        url: window.location.href,
        popups: ranked.map((item, index) => ({
            index,
            z: item.z,
            title: titleOf(item.root),
            text: String(item.root.innerText || item.root.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
            close: closePoint(item.root),
        })),
    };
}"""


class SitePopupRejected(RuntimeError):
    """실행 전 거부 — 허용목록 밖 호스트 등. 화면은 건드리지 않았다."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _label_allowed(label: str) -> bool:
    """정확히 '닫기'/'Close'/'×' 이고 금지 표지(하루 동안 보지 않기·신청·연동·확인 …)가 아닐 때만."""
    normalized = re.sub(r"\s+", " ", str(label or "")).strip().lower()
    return normalized in SITE_POPUP_CLOSE_LABELS and not OVERLAY_FORBIDDEN_MARKERS.search(normalized)


def _report_text(popup: Mapping[str, Any]) -> str:
    return mask_secrets(str(popup.get("text") or "")[:_REPORT_TEXT_LEN])


def _pick_target(popups: list[Mapping[str, Any]]) -> tuple[Mapping[str, Any] | None, str]:
    """위에서부터 첫 번째로 안전하게 닫을 수 있는 팝업. 없으면 (None, 남은 이유)."""
    reasons: set[str] = set()
    for popup in popups:
        if OVERLAY_SENSITIVE_MARKERS.search(str(popup.get("title") or "")):
            reasons.add("sensitive_popup")
            continue
        close = popup.get("close")
        if not isinstance(close, Mapping) or not _label_allowed(str(close.get("label") or "")):
            reasons.add("close_control_missing")
            continue
        if not close.get("hit"):
            reasons.add("close_obscured")
            continue
        return popup, ""
    for reason in ("close_obscured", "sensitive_popup", "close_control_missing"):
        if reason in reasons:
            return None, reason
    return None, ""


async def _probe(page: Any, policy: SitePopupPolicy) -> tuple[str, list[Mapping[str, Any]]]:
    state = await page.evaluate(
        _PROBE_SCRIPT,
        {"mode": "list", "selector": policy.popup_selector, "labels": list(SITE_POPUP_CLOSE_LABELS)},
    )
    state = state if isinstance(state, Mapping) else {}
    popups = [p for p in (state.get("popups") or []) if isinstance(p, Mapping)]
    return str(state.get("url") or ""), popups


async def _wait_gone(page: Any, index: int) -> bool:
    for _ in range(_GONE_POLL_ROUNDS):
        await asyncio.sleep(_GONE_POLL_SECONDS)
        state = await page.evaluate(_PROBE_SCRIPT, {"mode": "gone", "index": index, "labels": []})
        if isinstance(state, Mapping) and state.get("gone"):
            return True
    return False


async def _click_close(page: Any, target: Mapping[str, Any]) -> bool:
    """닫기를 누르고 그 팝업이 사라졌는지 확인한다. 안 사라지면 좌표를 다시 구해 한 번만 재시도."""
    index = int(target["index"])
    close: Any = target["close"]
    for attempt in range(2):
        await page.mouse.click(close["x"], close["y"])
        if await _wait_gone(page, index):
            return True
        if attempt == 0:
            repoint = await page.evaluate(
                _PROBE_SCRIPT, {"mode": "repoint", "index": index, "labels": list(SITE_POPUP_CLOSE_LABELS)}
            )
            close = repoint.get("close") if isinstance(repoint, Mapping) else None
            if not isinstance(close, Mapping) or not close.get("hit") or not _label_allowed(str(close.get("label") or "")):
                return False
    return False


async def dismiss_site_popups(page: Any, *, max_close: Any = None, tenant_id: Any = None) -> dict[str, Any]:
    """현재 페이지의 공지 팝업을 위에서부터 '닫기'로만 닫는다. 허용 도메인·루트는 DB 공용 목록(approved)이다.

    SitePopupRejected(site_not_allowlisted) / OverlayNotDismissible(닫은 목록 포함) 를 던질 수 있다.
    """
    limit = clamp_site_popup_max_close(max_close)
    host = urlsplit(str(getattr(page, "url", "") or "")).hostname or ""
    policy = await site_popup_policy_for_host(host, tenant_id)
    if policy is None:
        raise SitePopupRejected("site_not_allowlisted", f"허용목록에 없는 사이트입니다: {host or '(없음)'}")

    closed: list[str] = []
    reason = ""
    remaining = 0
    try:
        for _ in range(limit):
            _, popups = await _probe(page, policy)
            remaining = len(popups)
            if not popups:
                break
            target, reason = _pick_target(popups)
            if target is None:
                break
            if not await _click_close(page, target):
                exc = OverlayNotDismissible("not_closed_after_click", [{"text": t} for t in closed])
                exc.remaining_count = remaining  # type: ignore[attr-defined]
                raise exc
            closed.append(_report_text(target))
            remaining -= 1
            reason = ""
        else:
            reason = "max_close_reached"
        _, popups = await _probe(page, policy)
        remaining = len(popups)
        if remaining == 0:
            reason = ""
        elif not reason:
            reason = "max_close_reached"
    finally:
        try:
            await page.evaluate(_PROBE_SCRIPT, {"mode": "cleanup", "labels": []})
        except Exception:  # noqa: BLE001
            pass
    if remaining == 0:
        status = "closed" if closed else "none"
    else:
        status = "partial" if closed else "blocked"
    return {
        "status": status,
        "closed_count": len(closed),
        "remaining_count": remaining,
        "closed_popups": closed,
        "remaining_reason": reason,
        "host": host,
    }
