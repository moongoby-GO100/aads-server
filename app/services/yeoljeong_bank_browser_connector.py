"""Bank browser connector for Yeoljeong finance service.

Uses PC Agent / Browser Bridge sessions to collect transaction data from
bank quick-service portals (Shinhan 간편서비스, IBK 빠른서비스).

Security rules enforced here:
- No account passwords, raw account numbers, or business registration
  numbers are logged, returned, or stored.
- Raw portal HTML is never persisted.
- A missing/expired browser session returns action_required; this
  module never initiates a headless credential login.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import html
import importlib.util
import inspect
import logging
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from app.services.auth_challenge_orchestrator import classify_portal_state
    from app.services.browser_collection_audit import SITE_STAGE_LOG_SCHEMA, append_site_stage_log
except Exception:  # pragma: no cover - keeps standalone unit loading dependency-light
    _AUTH_SPEC = importlib.util.spec_from_file_location(
        "auth_challenge_orchestrator",
        Path(__file__).with_name("auth_challenge_orchestrator.py"),
    )
    _AUTH_MODULE = importlib.util.module_from_spec(_AUTH_SPEC)
    assert _AUTH_SPEC and _AUTH_SPEC.loader
    sys.modules[_AUTH_SPEC.name] = _AUTH_MODULE
    _AUTH_SPEC.loader.exec_module(_AUTH_MODULE)
    classify_portal_state = _AUTH_MODULE.classify_portal_state
    _AUDIT_SPEC = importlib.util.spec_from_file_location(
        "browser_collection_audit",
        Path(__file__).with_name("browser_collection_audit.py"),
    )
    _AUDIT_MODULE = importlib.util.module_from_spec(_AUDIT_SPEC)
    assert _AUDIT_SPEC and _AUDIT_SPEC.loader
    sys.modules[_AUDIT_SPEC.name] = _AUDIT_MODULE
    _AUDIT_SPEC.loader.exec_module(_AUDIT_MODULE)
    SITE_STAGE_LOG_SCHEMA = _AUDIT_MODULE.SITE_STAGE_LOG_SCHEMA
    append_site_stage_log = _AUDIT_MODULE.append_site_stage_log


logger = logging.getLogger(__name__)


# ── Work-key generation ──────────────────────────────────────────────────────

def bank_browser_work_key(account_id: str, business_id: str, branch_id: str) -> str:
    """Return a deterministic, opaque Browser Bridge work key.

    All three identifiers are hashed so no human-readable branch name,
    account alias, or account ID appears in the work key.
    """
    raw = f"{account_id}|{business_id}|{branch_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"yeoljeong-bank-browser-{digest}"


def shinhan_individual_browser_work_key(business_id: str, branch_id: str) -> str:
    """Return the dedicated Shinhan individual-business simple-query work key."""
    raw = f"shinhan-individual-simple|{business_id}|{branch_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"yeoljeong-bank-shinhan-individual-{digest}"


def ibk_business_browser_work_key(business_id: str, branch_id: str) -> str:
    """Return the dedicated IBK business quick-service work key."""
    raw = f"ibk-business-quick|{business_id}|{branch_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"yeoljeong-bank-ibk-business-{digest}"


def _diagnostic_screen_state(diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": str(diagnostics.get("screen_state") or "unknown"),
        "reason_code": str(diagnostics.get("screen_reason_code") or ""),
        "suggested_action": str(diagnostics.get("screen_suggested_action") or "no_action"),
        "requires_operator": str(diagnostics.get("screen_requires_operator") or "") == "1",
    }


def _safe_browser_error_fields(exc: Exception) -> dict[str, str]:
    fields = {"error_type": exc.__class__.__name__[:80]}
    error_code = str(getattr(exc, "error_code", "") or "").strip()
    if error_code:
        fields["error_code"] = error_code[:120]
    return fields


def _append_shinhan_stage_log(
    stage_logs: list[dict[str, str]],
    *,
    stage: str,
    status: str,
    started_at: float,
    error_code: str = "",
    reason: str = "",
    **fields: Any,
) -> None:
    """Append a secret-free Shinhan collection stage audit entry."""
    append_site_stage_log(
        stage_logs,
        stage=stage,
        status=status,
        started_at=started_at,
        logger=logger,
        event_name="shinhan_bank_collection_stage",
        error_code=error_code,
        reason=reason,
        **fields,
    )


def _append_shinhan_flow_stage_logs(
    stage_logs: list[dict[str, str]],
    *,
    step_result: dict[str, str],
    started_at: float,
    attempt_index: int,
) -> None:
    stage = str(step_result.get("stage") or "")
    error_code = str(step_result.get("error_code") or "")
    attempted = str(step_result.get("attempted") or "")
    common = {"attempt_index": str(attempt_index + 1), "source_stage": stage[:80]}
    if attempted == "failed":
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_flow_step",
            status="failed",
            started_at=started_at,
            error_code=error_code or str(step_result.get("error_type") or "SHINHAN_STEP_FAILED"),
            failure_condition="step_exception_or_recoverable_browser_error",
            **common,
        )
        return

    if stage in {"login", "login_keyboard", "login_keyboard_prepare"}:
        input_ok = (
            step_result.get("username") == "1"
            and (
                step_result.get("login_secret") == "1"
                or step_result.get("keyboard_secret") == "1"
                or step_result.get("transkey_secret") == "1"
            )
        )
        submit_ok = (
            step_result.get("login_submitted") == "1"
            or step_result.get("navigation_clicked") == "1"
            or step_result.get("websquare_triggered") == "1"
        )
        login_ok = step_result.get("login_success") == "1"
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_idpw_input",
            status="success" if input_ok else "failed",
            started_at=started_at,
            error_code="" if input_ok else (error_code or "IDPW_INPUT_NOT_CONFIRMED"),
            success_condition="username_and_secret_input_confirmed" if input_ok else "",
            failure_condition="" if input_ok else "username_or_secret_input_not_confirmed",
            **common,
        )
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_login_submit",
            status="success" if submit_ok else "failed",
            started_at=started_at,
            error_code="" if submit_ok else (error_code or "LOGIN_SUBMIT_NOT_CONFIRMED"),
            success_condition="login_button_or_websquare_submit_triggered" if submit_ok else "",
            failure_condition="" if submit_ok else "login_submit_not_triggered",
            **common,
        )
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_login_success",
            status="success" if login_ok else "pending",
            started_at=started_at,
            error_code="" if login_ok else (error_code or "LOGIN_SUCCESS_NOT_OBSERVED"),
            reason=str(step_result.get("login_success_reason") or ""),
            login_elapsed_ms=str(step_result.get("login_elapsed_ms") or ""),
            success_condition="post_login_text_or_url_marker_observed" if login_ok else "",
            failure_condition="" if login_ok else "post_login_marker_not_observed_before_timeout",
            **common,
        )
        return

    if stage in {"login_notice_confirm", "account_page_navigation"}:
        account_page_ok = (
            step_result.get("notice_confirm") == "1"
            or step_result.get("account_page_navigation") == "1"
            or step_result.get("account_page_direct_hash") == "1"
            or step_result.get("navigation_clicked") == "1"
        )
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_account_query_page",
            status="success" if account_page_ok else "failed",
            started_at=started_at,
            error_code="" if account_page_ok else (error_code or "ACCOUNT_QUERY_PAGE_NOT_CONFIRMED"),
            success_condition="account_query_page_navigation_confirmed" if account_page_ok else "",
            failure_condition="" if account_page_ok else "account_query_page_navigation_not_confirmed",
            **common,
        )
        return

    if stage in {"account_query", "corporate_quick"}:
        account_ok = (
            step_result.get("account_resolved") == "1"
            or step_result.get("account_selected") == "1"
            or step_result.get("account_no") == "1"
            or step_result.get("account_direct_input") == "1"
        )
        secret_ok = step_result.get("account_secret") == "1"
        date_ok = step_result.get("date_from") == "1" and step_result.get("date_to") == "1"
        query_ok = step_result.get("query_submitted") == "1"
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_account_select",
            status="success" if account_ok else "failed",
            started_at=started_at,
            error_code="" if account_ok else (error_code or "ACCOUNT_SELECTION_NOT_CONFIRMED"),
            success_condition="account_selected_or_direct_account_input_confirmed" if account_ok else "",
            failure_condition="" if account_ok else "account_selection_or_input_not_confirmed",
            **common,
        )
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_account_password_input",
            status="success" if secret_ok else "failed",
            started_at=started_at,
            error_code="" if secret_ok else (error_code or "ACCOUNT_PASSWORD_NOT_CONFIRMED"),
            success_condition="account_password_input_confirmed" if secret_ok else "",
            failure_condition="" if secret_ok else "account_password_input_not_confirmed",
            **common,
        )
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_period_select",
            status="success" if date_ok else "failed",
            started_at=started_at,
            error_code="" if date_ok else (error_code or "PERIOD_SELECTION_NOT_CONFIRMED"),
            success_condition="date_from_and_date_to_confirmed" if date_ok else "",
            failure_condition="" if date_ok else "date_range_not_confirmed",
            **common,
        )
        _append_shinhan_stage_log(
            stage_logs,
            stage="shinhan_query_submit",
            status="success" if query_ok else "failed",
            started_at=started_at,
            error_code="" if query_ok else (error_code or "QUERY_SUBMIT_NOT_CONFIRMED"),
            success_condition="query_submit_triggered" if query_ok else "",
            failure_condition="" if query_ok else "query_submit_not_confirmed",
            **common,
        )


def _bank_eval_timeout_ms(timeout_ms: int | None) -> int | None:
    if timeout_ms is None:
        return None
    try:
        base_timeout = int(timeout_ms)
    except (TypeError, ValueError):
        return timeout_ms
    try:
        multiplier = float(os.getenv("YEOLJEONG_BANK_BROWSER_EVAL_TIMEOUT_MULTIPLIER", "1.0") or "1.0")
    except ValueError:
        multiplier = 1.0
    try:
        min_timeout = int(os.getenv("YEOLJEONG_BANK_BROWSER_MIN_EVAL_TIMEOUT_MS", "5000") or "5000")
    except ValueError:
        min_timeout = 5000
    try:
        max_timeout = int(os.getenv("YEOLJEONG_BANK_BROWSER_MAX_EVAL_TIMEOUT_MS", "15000") or "15000")
    except ValueError:
        max_timeout = 15000
    expanded = max(base_timeout, min_timeout, int(base_timeout * max(multiplier, 1.0)))
    return max(1000, min(max_timeout, expanded))


async def _evaluate_page(
    page: Any,
    expression: str,
    *args: Any,
    timeout_ms: int | None = None,
    await_promise: bool = False,
) -> Any:
    """Evaluate browser DOM with an optional hard timeout."""
    evaluate_kwargs: dict[str, Any] = {}
    if await_promise:
        evaluate_kwargs["await_promise"] = True
    if timeout_ms is None:
        try:
            return await page.evaluate(expression, *args, **evaluate_kwargs)
        except TypeError as exc:
            if "await_promise" not in str(exc):
                raise
            return await page.evaluate(expression, *args)
    effective_timeout_ms = _bank_eval_timeout_ms(timeout_ms)
    timeout_seconds = max((effective_timeout_ms or timeout_ms) / 1000, 0.1)
    evaluate_kwargs["timeout"] = effective_timeout_ms or timeout_ms
    try:
        coro = page.evaluate(expression, *args, **evaluate_kwargs)
        return await asyncio.wait_for(coro, timeout=timeout_seconds + 1.0)
    except TypeError as exc:
        if "timeout" not in str(exc) and "await_promise" not in str(exc):
            raise
    return await asyncio.wait_for(page.evaluate(expression, *args), timeout=timeout_seconds)


async def _trigger_password_manager_fallback(page: Any) -> bool:
    """Focus a login field so the browser password manager can assist.

    This does not read field values, submit forms, or bypass OTP/CAPTCHA.
    It only dispatches focus/input events in the connected PC Agent session.
    """
    try:
        return bool(await _evaluate_page(
            page,
            """
            () => {
              const selectors = [
                "input[autocomplete='username']",
                "input[autocomplete='current-password']",
                "input[type='password']",
                "input[name*='user' i]",
                "input[name*='id' i]"
              ];
              const input = selectors
                .map((selector) => document.querySelector(selector))
                .find((candidate) => candidate && !candidate.disabled && candidate.offsetParent !== null);
              if (!input) return false;
              input.focus();
              input.dispatchEvent(new Event('input', {bubbles: true}));
              return true;
            }
            """,
            timeout_ms=8000,
        ))
    except Exception:
        return False


def _install_dialog_auto_accept(page: Any) -> bool:
    """Accept Shinhan's post-login browser alert without exposing secrets."""
    try:
        on_dialog = getattr(page, "on", None)
        if not callable(on_dialog) or inspect.iscoroutinefunction(on_dialog):
            return False

        def _accept(dialog: Any) -> None:
            try:
                asyncio.get_running_loop().create_task(dialog.accept())
            except Exception:
                pass

        on_dialog("dialog", _accept)
        return True
    except Exception:
        return False


async def _close_shinhan_security_notice(page: Any) -> bool:
    """Close Shinhan blocking notices when they prevent the next bank step."""
    closed_once = False
    for _attempt in range(4):
        try:
            raw = await _evaluate_page(
                page,
                """
                () => {
                  const visible = (el) => !!(el && !el.disabled && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                  const visibleText = (root = document) => Array.from(root.querySelectorAll('.w2popup_window, .w2window, [role="dialog"], a, button, input, span, div'))
                    .filter((el) => visible(el))
                    .map((el) => String(el.innerText || el.value || el.title || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean)
                    .join(' ');
                  const bodyText = visibleText();
                  const noticePatterns = [
                    '인터넷뱅킹 보안프로그램설치안내',
                    '키보드 입력 검증에 실패',
                    '거래를 처음부터 다시 진행',
                    '이용자ID를 입력해주세요',
                    '비밀번호를 입력해주세요',
                    '비밀번호 최소자릿수'
                  ];
                  const matchedNotice = noticePatterns.find((item) => bodyText.includes(item)) || '';
                  if (!matchedNotice) return {closed: '0'};
                  const componentById = (id) => {
                    if (!id) return null;
                    const candidates = [
                      () => window.$p?.getComponentById?.(id),
                      () => window.WebSquare?.util?.getComponentById?.(id),
                      () => window.WebSquare?.ModelUtil?.getInstance?.(id),
                      () => window[id]
                    ];
                    for (const getter of candidates) {
                      try {
                        const component = getter();
                        if (component) return component;
                      } catch (_) {}
                    }
                    return null;
                  };
                  const call = (component, methods, ...args) => {
                    if (!component) return false;
                    for (const method of methods) {
                      try {
                        if (typeof component[method] === 'function') {
                          component[method](...args);
                          return true;
                        }
                      } catch (_) {}
                    }
                    return false;
                  };
                  const clickCandidate = (el) => {
                    if (!el) return false;
                    const component = componentById(el.id);
                    try { call(component, ['trigger', 'fireEvent', 'dispatchEvent'], 'onclick'); } catch (_) {}
                    try { call(component, ['trigger', 'fireEvent', 'dispatchEvent'], 'click'); } catch (_) {}
                    try { call(component, ['click', 'userClick']); } catch (_) {}
                    try {
                      el.click();
                      el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                    } catch (_) {}
                    return true;
                  };
                  const exactClose = Array.from(document.querySelectorAll('[id*="CO00038RP"][id*="btnmakedpopupclose"], [id*="btnmakedpopupclose"], [class*="w2window_close"], .w2window_close'))
                    .filter(visible)
                    .find((el) => {
                      const text = String(el.closest?.('.w2popup_window,.w2window,[role="dialog"],[id*="CO00038RP"]')?.innerText || '');
                      return /보안프로그램설치안내|키보드 입력 검증|처음부터 다시 진행|이용자ID를 입력|비밀번호를 입력|비밀번호 최소자릿수/.test(text)
                        || /CO00038RP|btnmakedpopupclose/i.test(String(el.id || el.className || ''));
                    }) || null;
                  const all = exactClose ? [exactClose] : Array.from(document.querySelectorAll('a,button,input,span,div'));
                  const scored = all
                    .filter(visible)
                    .map((el) => {
                      const label = String(el.innerText || el.value || el.title || '').replace(/\\s+/g, ' ').trim();
                      const meta = String(el.id || el.className || el.title || '');
                      let score = 0;
                      if (/btnmakedpopupclose|w2window_close|_close\\b|layerClose/i.test(meta)) score += 140;
                      if (/CO00038RP/i.test(meta)) score += 50;
                      if (label === '확인') score += 110;
                      if (label === '닫기') score += 90;
                      if (/보안프로그램설치안내/.test(String(el.closest?.('.w2popup_window,.w2window')?.innerText || ''))) score += 60;
                      if (/키보드 입력 검증|처음부터 다시 진행|이용자ID를 입력|비밀번호를 입력|비밀번호 최소자릿수/.test(String(el.closest?.('.w2popup_window,.w2window,[role="dialog"]')?.innerText || ''))) score += 60;
                      if (/btnTotalClose/i.test(meta)) score -= 200;
                      const rect = el.getBoundingClientRect();
                      if (rect.width <= 0 || rect.height <= 0) score = 0;
                      return {el, label, meta, score};
                    })
                    .filter((item) => item.score > 0)
                    .sort((a, b) => b.score - a.score);
                  const hit = scored[0]?.el || null;
                  if (!hit) return {closed: '0', notice: '1'};
                  clickCandidate(hit);
                  let afterText = visibleText();
                  if (noticePatterns.some((item) => afterText.includes(item))) {
                    try {
                      const popup = hit.closest?.('.w2popup_window,.w2window,[id*="CO00038RP"]');
                      if (popup) {
                        popup.style.display = 'none';
                        popup.setAttribute('aria-hidden', 'true');
                      }
                      afterText = visibleText();
                    } catch (_) {}
                  }
                  return {
                    closed: noticePatterns.some((item) => afterText.includes(item)) ? '0' : '1',
                    notice: '1',
                    notice_type: matchedNotice.slice(0, 80),
                    tag: String(hit.tagName || '').slice(0, 20),
                    id: String(hit.id || '').slice(0, 80)
                  };
                }
                """,
                timeout_ms=25000,
            )
        except Exception:
            raw = None
        if isinstance(raw, dict) and str(raw.get("closed") or "") == "1":
            closed_once = True
            try:
                await asyncio.sleep(0.75)
            except Exception:
                pass
            notice_state = await _shinhan_security_notice_state(page)
            if str(notice_state.get("present") or "") != "1":
                return True
        else:
            try:
                await asyncio.sleep(0.75)
            except Exception:
                pass
    return closed_once


async def _shinhan_security_notice_state(page: Any) -> dict[str, str]:
    """Return safe Shinhan blocking notice diagnostics without exposing secrets."""
    try:
        raw = await _evaluate_page(
            page,
            """
            () => {
              const visible = (el) => !!(el && !el.disabled && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
              const bodyText = Array.from(document.querySelectorAll('.w2popup_window, .w2window, [role="dialog"], a, button, input, span, div'))
                .filter((el) => visible(el))
                .map((el) => String(el.innerText || el.value || el.title || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean)
                .join(' ');
              const notices = [
                ['SHINHAN_KEYBOARD_VERIFICATION_FAILED', /키보드 입력 검증에 실패|처음부터 다시 진행/],
                ['SHINHAN_LOGIN_ID_REQUIRED', /이용자ID를 입력해주세요/],
                ['SHINHAN_PASSWORD_REQUIRED', /비밀번호를 입력해주세요|비밀번호 최소자릿수/],
                ['SHINHAN_SECURITY_PROGRAM_NOTICE', /인터넷뱅킹 보안프로그램설치안내/]
              ];
              const matched = notices.find(([, pattern]) => pattern.test(bodyText));
              if (!matched) return {present: '0'};
              return {
                present: '1',
                error_code: matched[0],
                notice_type: matched[0]
              };
            }
            """,
            timeout_ms=12000,
        )
    except Exception:
        return {"present": "0"}
    if not isinstance(raw, dict) or str(raw.get("present") or "") != "1":
        return {"present": "0"}
    return {
        "present": "1",
        "error_code": str(raw.get("error_code") or "SHINHAN_SECURITY_NOTICE")[:80],
        "notice_type": str(raw.get("notice_type") or "SHINHAN_SECURITY_NOTICE")[:80],
    }


async def _try_pc_agent_keyboard_type(page: Any, text: str) -> bool:
    """Type text through the PC Agent OS keyboard path for bank secure inputs."""
    value = str(text or "")
    if not value:
        return False
    runner = getattr(page, "_run_browser_command", None)
    if not callable(runner):
        return False
    timeout_seconds = max(10.0, min(60.0, 8.0 + len(value) * 1.5))
    try:
        try:
            await runner(
                "window_focus",
                {"title": "간편조회서비스"},
                command_timeout_seconds=8.0,
                queue_wait_timeout_seconds=5.0,
            )
        except Exception:
            pass
        await runner(
            "keyboard_type",
            {"text": value},
            command_timeout_seconds=timeout_seconds,
            queue_wait_timeout_seconds=10.0,
        )
        return True
    except TypeError:
        try:
            await runner("keyboard_type", {"text": value})
            return True
        except Exception:
            return False
    except Exception:
        return False


async def _try_shinhan_individual_keyboard_login_step(
    page: Any,
    *,
    username: str,
    password: str,
) -> dict[str, str]:
    """Use real PC Agent keyboard input for Shinhan ID login secure fields."""
    if not username or not password or not callable(getattr(page, "_run_browser_command", None)):
        return {"attempted": "0"}
    try:
        prepared = await _evaluate_page(
            page,
            """
	            (input) => {
	              const byId = (id) => document.getElementById(id);
	              const visible = (el) => !!(el && !el.disabled && el.offsetParent !== null);
	              const firstVisible = (selectors) => {
	                for (const selector of selectors) {
	                  try {
	                    const found = Array.from(document.querySelectorAll(selector)).find((el) => visible(el));
	                    if (found) return found;
	                  } catch (_) {}
	                }
	                return null;
	              };
	              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
	              const loginIdEl = byId('ibx_loginId') || byId('ibx_loginId_cib') || byId('mf_wfm_main_ibx_loginId')
	                || firstVisible([
	                  'input[id$="_ibx_loginId"]',
	                  'input[id*="loginId"]',
	                  'input[title*="ID"]',
	                  'input[placeholder*="로그인ID"]'
	                ]);
	              const passwordEl = byId('비밀번호') || byId('비밀번호_cib') || byId('wq_uuid_769_scr_pwd')
	                || firstVisible([
	                  'input[id$="_scr_pwd"]',
	                  'input[id*="scr_pwd"]',
	                  'input[title*="비밀번호"]',
	                  'input[placeholder*="비밀번호"]',
	                  'input[type="password"]'
	                ]);
              const loginFieldsVisible = visible(loginIdEl) && visible(passwordEl);
              const openAccountInquiry = () => {
                try {
                  if (window.shbComm?.menu) {
                    window.shbComm.menu.redirectUrl = '210101000000';
                  }
                } catch (_) {}
                try {
                  if (window.shbComm && typeof window.shbComm.goPage === 'function') {
                    window.setTimeout(() => {
                      try { window.shbComm.goPage('210101000000'); } catch (_) {}
                    }, 30);
                    return true;
                  }
                } catch (_) {}
                const candidate = Array.from(document.querySelectorAll('a,button,input[type=button],input[type=submit],span'))
                  .filter((el) => visible(el))
                  .find((el) => String(el.innerText || el.value || el.title || '').replace(/\\s+/g, ' ').trim() === '계좌조회');
                if (candidate) {
                  try { candidate.click(); } catch (_) {}
                  try { candidate.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window})); } catch (_) {}
                  return true;
                }
                try {
                  window.location.hash = '210101000000';
                  window.dispatchEvent(new HashChangeEvent('hashchange'));
                  return true;
                } catch (_) {}
                return false;
              };
              if (!loginIdEl && !passwordEl && String(window.location.href || '').includes('#210000000000')) {
                const opened = openAccountInquiry();
                return {
                  attempted: opened ? '1' : '0',
                  stage: opened ? 'account_page_navigation' : 'hidden_login_panel',
                  username: '0',
                  password_focused: '0',
                  password_selector: '',
                  navigation_clicked: opened ? '1' : '0',
                  websquare_triggered: opened ? '1' : '0'
                };
              }
              const hasLoginPanel = /이용자\\s*ID\\s*로그인|이용자ID\\s*로그인|아이디\\s*로그인/i.test(text)
                || loginFieldsVisible;
              if (!hasLoginPanel || !passwordEl) return {attempted: '0', stage: 'not_login_panel'};
              const setField = (el, value) => {
                if (!el || !value) return false;
                try {
                  const component = window.$p?.getComponentById?.(el.id)
                    || window.WebSquare?.util?.getComponentById?.(el.id)
                    || window.WebSquare?.ModelUtil?.getInstance?.(el.id)
                    || null;
                  if (component) {
                    for (const method of ['setValue', 'setText', 'setInputValue']) {
                      if (typeof component[method] === 'function') {
                        try { component[method](value); break; } catch (_) {}
                      }
                    }
                  }
                  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                  if (setter) setter.call(el, value);
                  else el.value = value;
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                  return true;
                } catch (_) {
                  return false;
                }
              };
	              const usernameOk = setField(loginIdEl, input.username);
              try {
                passwordEl.focus();
                passwordEl.click();
                passwordEl.value = '';
                passwordEl.dispatchEvent(new Event('input', {bubbles: true}));
                if (window.tk && typeof window.tk.onKeyboard === 'function') window.tk.onKeyboard(passwordEl);
              } catch (_) {}
              const selector = passwordEl.id ? `[id="${String(passwordEl.id).replace(/"/g, '\\\\"')}"]` : '';
              return {
                attempted: '1',
                stage: 'login_keyboard_prepare',
                username: usernameOk ? '1' : '0',
                password_focused: document.activeElement === passwordEl ? '1' : '0',
                password_selector: selector
              };
            }
            """,
            {"username": username},
            timeout_ms=15000,
        )
    except Exception as exc:
        return {"attempted": "failed", **_safe_browser_error_fields(exc)}
    if not isinstance(prepared, dict) or str(prepared.get("attempted") or "") != "1":
        return {"attempted": str((prepared or {}).get("attempted") or "0")[:20], "stage": str((prepared or {}).get("stage") or "")[:40]}
    if str(prepared.get("stage") or "") == "account_page_navigation":
        return {
            "attempted": "1",
            "stage": "account_page_navigation",
            "username": "0",
            "login_secret": "0",
            "keyboard_secret": "0",
            "navigation_clicked": "1" if str(prepared.get("navigation_clicked") or "") == "1" else "0",
            "websquare_triggered": "1" if str(prepared.get("websquare_triggered") or "") == "1" else "0",
        }
    if str(prepared.get("username") or "") != "1" or str(prepared.get("password_focused") or "") != "1":
        return {
            "attempted": "1",
            "stage": "login_keyboard_prepare",
            "username": "1" if str(prepared.get("username") or "") == "1" else "0",
            "login_secret": "0",
            "keyboard_secret": "0",
            "navigation_clicked": "0",
            "websquare_triggered": "0",
        }
    password_selector = str(prepared.get("password_selector") or "").strip()
    if password_selector and hasattr(page, "click"):
        try:
            await page.click(password_selector)
        except Exception:
            pass
    typed = await _try_pc_agent_keyboard_type(page, password)
    if not typed:
        return {
            "attempted": "1",
            "stage": "login_keyboard_prepare",
            "username": "1",
            "login_secret": "0",
            "keyboard_secret": "0",
            "navigation_clicked": "0",
            "websquare_triggered": "0",
        }
    try:
        clicked = await _evaluate_page(
            page,
            """
            () => {
              const byId = (id) => document.getElementById(id);
              const componentById = (id) => {
                try { return window.$p?.getComponentById?.(id); } catch (_) {}
                try { return window.WebSquare?.util?.getComponentById?.(id); } catch (_) {}
                try { return window.WebSquare?.ModelUtil?.getInstance?.(id); } catch (_) {}
                return null;
              };
              const call = (component, methods, ...args) => {
                if (!component) return false;
                for (const method of methods) {
                  try {
                    if (typeof component[method] === 'function') {
                      component[method](...args);
                      return true;
                    }
                  } catch (_) {}
                }
                return false;
              };
              try {
                if (window.shbComm) window.shbComm.ASTX_INSTALL = true;
                if (window.$ASTX2 && typeof window.$ASTX2.getPCLOGData === 'function') {
                  window.$ASTX2.getPCLOGData = function(_a, successCb) {
                    if (typeof successCb === 'function') successCb({pclog_data: ''});
                  };
                }
              } catch (_) {}
              try {
                if (window.shbObj && typeof window.shbObj.fncIdLogin === 'function') {
                  window.setTimeout(() => {
                    try { window.shbObj.fncIdLogin(); } catch (_) {}
                  }, 30);
                  return {clicked: '1', method: 'fncIdLogin'};
                }
              } catch (_) {}
	              for (const id of ['btn_idLogin', 'btn_idLogin_cib', 'mf_wfm_main_btn_login']) {
                const el = byId(id);
                const component = componentById(id);
                if (el || component) {
                  window.setTimeout(() => {
                    try { call(component, ['trigger', 'fireEvent', 'dispatchEvent'], 'onclick'); } catch (_) {}
                    try { call(component, ['trigger', 'fireEvent', 'dispatchEvent'], 'click'); } catch (_) {}
                    try { call(component, ['click', 'userClick']); } catch (_) {}
                    try {
                      if (el) {
                        el.click();
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                      }
                    } catch (_) {}
                  }, 30);
                  return {clicked: '1', method: id};
                }
              }
              return {clicked: '0'};
            }
            """,
            timeout_ms=15000,
        )
    except Exception:
        clicked = {"clicked": "0"}
    if isinstance(clicked, dict) and str(clicked.get("clicked") or "") == "1":
        try:
            await page.wait_for_load_state("networkidle", timeout=1800)
        except Exception:
            pass
        notice_state = await _shinhan_security_notice_state(page)
        if str(notice_state.get("present") or "") == "1":
            await _close_shinhan_security_notice(page)
            return {
                "attempted": "failed",
                "stage": "login_keyboard_verification_failed",
                "username": "1",
                "login_secret": "0",
                "keyboard_secret": "0",
                "navigation_clicked": "0",
                "websquare_triggered": "0",
                "error_code": str(notice_state.get("error_code") or "SHINHAN_SECURITY_NOTICE")[:120],
                "error_type": "shinhan_security_notice",
            }
    return {
        "attempted": "1",
        "stage": "login_keyboard",
        "username": "1",
        "login_secret": "1",
        "keyboard_secret": "1",
        "navigation_clicked": "1" if isinstance(clicked, dict) and str(clicked.get("clicked") or "") == "1" else "0",
        "websquare_triggered": "1" if isinstance(clicked, dict) and str(clicked.get("clicked") or "") == "1" else "0",
    }


def _safe_portal_text(raw_html: str, *, max_chars: int = 4000) -> str:
    """Return a redacted text sample for state classification only."""
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", str(raw_html or ""), flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\b\d{2,}[-.\s]?\d{2,}[-.\s]?\d{2,}\b", "[redacted-number]", text)
    text = re.sub(r"\b\d{6,}\b", "[redacted-number]", text)
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


async def _focus_auth_challenge_input(page: Any, state: str) -> bool:
    """Focus a safe input for operator/keychain completion without reading values."""
    if state not in {
        "captcha_required",
        "otp_required",
        "certificate_password_required",
        "identity_check_required",
    }:
        return False
    try:
        return bool(await _evaluate_page(
            page,
            """
            (state) => {
              const byState = {
                captcha_required: [
                  "input[name*='captcha' i]",
                  "input[id*='captcha' i]",
                  "input[placeholder*='보안문자']",
                  "input[aria-label*='보안문자']"
                ],
                otp_required: [
                  "input[name*='otp' i]",
                  "input[id*='otp' i]",
                  "input[autocomplete='one-time-code']",
                  "input[placeholder*='인증번호']",
                  "input[aria-label*='인증번호']"
                ],
                certificate_password_required: [
                  "input[type='password']",
                  "input[name*='cert' i]",
                  "input[id*='cert' i]",
                  "input[placeholder*='인증서']"
                ],
                identity_check_required: [
                  "button",
                  "input[type='button']",
                  "input[type='submit']"
                ]
              };
              const selectors = byState[state] || [];
              for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && !el.disabled && el.offsetParent !== null) {
                  el.focus();
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  return true;
                }
              }
              return false;
            }
            """,
            state,
            timeout_ms=8000,
        ))
    except Exception:
        return False


async def _safe_selector_candidates(page: Any) -> list[dict[str, str]]:
    """Collect non-secret selector hints for diagnostics."""
    try:
        raw = await _evaluate_page(
            page,
            """
            () => Array.from(document.querySelectorAll('input,button,select,a'))
              .filter((el) => el && el.offsetParent !== null)
              .slice(0, 12)
              .map((el) => {
                const tag = (el.tagName || '').toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const id = (el.id || '').slice(0, 40);
                const name = (el.getAttribute('name') || '').slice(0, 40);
                const label = (
                  el.getAttribute('aria-label') ||
                  el.getAttribute('placeholder') ||
                  el.innerText ||
                  ''
                ).slice(0, 40);
                return {tag, type, id, name, label};
              })
            """,
            timeout_ms=10000,
        )
    except Exception:
        return []
    result: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        safe_item: dict[str, str] = {}
        for key in ("tag", "type", "id", "name", "label"):
            value = re.sub(r"\d{4,}", "[n]", str(item.get(key) or ""))[:40]
            if value:
                safe_item[key] = value
        if safe_item:
            result.append(safe_item)
    return result


def _selector_candidates_include_bank_login(candidates: list[dict[str, str]]) -> bool:
    has_account = False
    has_secret = False
    for item in candidates:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(
            str(item.get(key) or "").lower()
            for key in ("id", "name", "label")
        )
        field_type = str(item.get("type") or "").lower()
        if any(token in haystack for token in ("account", "계좌", "acct")):
            has_account = True
        if field_type == "password" or any(token in haystack for token in ("password", "비밀번호", "secret")):
            has_secret = True
    return has_account and has_secret


async def _try_open_transaction_view(page: Any, clicked_keys: list[str] | None = None) -> dict[str, str]:
    """Click a safe transaction-list navigation candidate, if visible.

    This only clicks generic navigation/query controls. It never fills,
    submits, reads credentials, or attempts to solve a challenge.
    """
    try:
        raw = await _evaluate_page(
            page,
            """
            (clickedKeys) => {
              const clicked = new Set(clickedKeys || []);
              const scoreFor = (label) => {
                const value = String(label || '').toLowerCase();
                if (value.includes('거래내역') || value.includes('거래조회')) return 100;
                if (value.includes('입출금')) return 90;
                if (value.includes('내역조회') || value.includes('상세조회')) return 80;
                if (value.includes('계좌조회')) return 65;
                if (value.includes('빠른조회')) return 55;
                if (value.includes('조회') || value.includes('transaction')) return 40;
                return 0;
              };
              const deny = ['로그아웃', '삭제', '해지', '이체', '송금', '납부', '비밀번호'];
              const candidates = Array.from(document.querySelectorAll('a,button,input[type=button],input[type=submit]'))
                .map((el, index) => {
                  const label = (
                    el.innerText ||
                    el.value ||
                    el.getAttribute('title') ||
                    el.getAttribute('aria-label') ||
                    ''
                  ).replace(/\\s+/g, ' ').trim();
                  const id = el.id || el.getAttribute('name') || '';
                  const key = `${id}|${label}|${index}`;
                  return {el, label, id, key, score: scoreFor(label)};
                })
                .filter((item) => {
                  if (!item.el || item.el.disabled || item.el.offsetParent === null) return false;
                  if (!item.label || !item.score) return false;
                  if (clicked.has(item.key) || clicked.has(item.label) || clicked.has(item.id)) return false;
                  if (deny.some((word) => item.label.includes(word))) return false;
                  return true;
                })
                .sort((a, b) => b.score - a.score);
              for (const item of candidates) {
                const el = item.el;
                if (!el || el.disabled || el.offsetParent === null) continue;
                el.focus();
                el.click();
                return {clicked: true, label: item.label.slice(0, 60), key: item.key, id: item.id};
              }
              return {clicked: false};
            }
            """,
            clicked_keys or [],
            timeout_ms=10000,
        )
    except Exception:
        return {"clicked": "0"}
    if not isinstance(raw, dict) or not raw.get("clicked"):
        return {"clicked": "0"}
    label = re.sub(r"\d{4,}", "[n]", str(raw.get("label") or ""))[:60]
    key = re.sub(r"\d{4,}", "[n]", str(raw.get("key") or ""))[:120]
    result = {"clicked": "1", "label": label}
    if key:
        result["key"] = key
    return result


def _registrable_host(host: str) -> str:
    parts = [part for part in str(host or "").lower().split(".") if part]
    if len(parts) <= 2:
        return ".".join(parts)
    return ".".join(parts[-2:])


def _portal_url_reusable(current_url: str, portal_url: str) -> bool:
    current = str(current_url or "").strip()
    portal = str(portal_url or "").strip()
    if not current or current.startswith(("about:", "chrome:", "edge:")):
        return False
    if not portal:
        return True
    try:
        current_host = urlparse(current).hostname or ""
        portal_host = urlparse(portal).hostname or ""
    except Exception:
        return False
    if not current_host or not portal_host:
        return False
    if current_host == portal_host:
        return True
    return _registrable_host(current_host) == _registrable_host(portal_host)


def _browser_session_agent_id(session: Any) -> str:
    try:
        metadata = dict(getattr(getattr(session, "endpoint", None), "metadata", None) or {})
    except Exception:
        metadata = {}
    return str(metadata.get("agent_id") or "").strip()


def _is_shinhan_service(bank_code: str, bank_name: str, institution_code: str, portal_url: str) -> bool:
    haystack = " ".join(
        str(value or "").strip().lower()
        for value in (bank_code, bank_name, institution_code, portal_url)
    )
    return "신한" in haystack or "shinhan" in haystack or str(bank_code or "").strip() == "088"


def _shinhan_query_flow_mode(business_entity_type: str, account: dict[str, Any]) -> str:
    """Return Shinhan browser flow mode without exposing business identifiers."""
    values = [
        business_entity_type,
        account.get("business_entity_type"),
        account.get("entityType"),
        account.get("entity_type"),
        account.get("business_type"),
        account.get("businessType"),
    ]
    normalized = " ".join(str(value or "").strip().lower() for value in values)
    if any(token in normalized for token in ("corporation", "corporate", "법인")):
        return "corporate_quick"
    return "individual_simple"


def _is_ibk_service(bank_code: str, bank_name: str, institution_code: str, portal_url: str = "") -> bool:
    haystack = " ".join(
        str(value or "").strip().lower()
        for value in (bank_code, bank_name, institution_code, portal_url)
    )
    if (
        str(bank_code or "").strip() == "088"
        or str(institution_code or "").strip().lower() == "shinhan_business"
        or "shinhan" in haystack
        or "신한" in haystack
    ):
        return False
    return (
        "ibk" in haystack
        or "기업은행" in haystack
        or str(bank_code or "").strip() == "003"
        or str(institution_code or "").strip() == "003"
    )


async def _try_prepare_ibk_quick_flow(
    page: Any,
    *,
    username: str,
    password: str,
    account_no: str,
    account_password: str,
    business_registration_no: str,
    date_from: str,
    date_to: str,
) -> dict[str, str]:
    """Prepare IBK quick-service query screens via the connected PC Agent."""
    if not any([username, password, account_no, account_password, business_registration_no, date_from, date_to]):
        return {"attempted": "0", "mode": "ibk_quick"}
    try:
        raw = await _evaluate_page(
            page,
            """
            async (input) => {
              // ibkQuickFlow
              const visible = (el) => !!(el && !el.disabled && el.offsetParent !== null);
              const digits = (value) => String(value || '').replace(/\\D+/g, '');
              const textOf = (el) => String(
                el?.innerText ||
                el?.value ||
                el?.getAttribute?.('title') ||
                el?.getAttribute?.('aria-label') ||
                el?.getAttribute?.('placeholder') ||
                ''
              ).replace(/\\s+/g, ' ').trim();
              const sameOriginDocuments = () => {
                const docs = [document];
                for (const frame of Array.from(document.querySelectorAll('iframe,frame'))) {
                  try {
                    if (frame.contentDocument) docs.push(frame.contentDocument);
                  } catch (_) {}
                }
                return docs;
              };
              const allElements = (selector) => {
                const result = [];
                for (const doc of sameOriginDocuments()) {
                  try { result.push(...Array.from(doc.querySelectorAll(selector))); } catch (_) {}
                }
                return result;
              };
              const fieldText = (el) => String([
                el?.id,
                el?.name,
                el?.getAttribute?.('title'),
                el?.getAttribute?.('aria-label'),
                el?.getAttribute?.('placeholder'),
                el?.closest?.('label')?.innerText,
                el?.parentElement?.innerText
              ].filter(Boolean).join(' ')).replace(/\\s+/g, ' ').trim();
              const setValue = (el, value) => {
                if (!visible(el) || !value) return false;
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.focus();
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
                return true;
              };
              const inputs = () => allElements('input,textarea').filter(visible);
              const firstInput = (patterns, type = '') => inputs().find((el) => {
                if (type && String(el.type || '').toLowerCase() !== type) return false;
                const text = fieldText(el);
                return patterns.some((pattern) => pattern.test(text));
              }) || null;
              const fillByPattern = (patterns, value, type = '') => setValue(firstInput(patterns, type), value);
              const passwordInputs = () => inputs().filter((el) => String(el.type || '').toLowerCase() === 'password');
              const loginPassword = () => firstInput([
                /이용자.?비밀번호|로그인.?비밀번호|비밀번호|password|passwd|login.*pw/i
              ], 'password') || passwordInputs()[0] || null;
              const quickAccountPassword = () => firstInput([
                /계좌.?비밀번호|계좌.?암호|통장.?비밀번호|account.*password|acct.*pw|4자리/i
              ], 'password') || passwordInputs()[1] || passwordInputs()[0] || null;
              const clickBest = (purpose = 'query') => {
                const deny = ['이체', '송금', '납부', '삭제', '해지', '출금'];
                const score = (label, id) => {
                  const value = `${label} ${id}`.toLowerCase();
                  if (!value.trim() || deny.some((word) => value.includes(word))) return 0;
                  if (purpose === 'login') {
                    if (/로그인|login/.test(value)) return 120;
                    if (/확인|다음/.test(value)) return 60;
                    return 0;
                  }
                  if (/조회|검색|확인/.test(value)) return value.includes('조회') ? 120 : 70;
                  return 0;
                };
                const item = allElements('a,button,input[type=button],input[type=submit]')
                  .filter(visible)
                  .map((el) => ({el, label: textOf(el), id: String(el.id || el.name || '')}))
                  .map((item) => ({...item, score: score(item.label, item.id)}))
                  .filter((item) => item.score > 0)
                  .sort((a, b) => b.score - a.score)[0];
                if (!item) return '';
                item.el.focus();
                try { item.el.click(); } catch (_) {}
                try { item.el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window})); } catch (_) {}
                return item.label.slice(0, 40);
              };
              const fillAccountNo = () => {
                const target = digits(input.accountNo);
                if (!target) return false;
                let ok = fillByPattern([
                  /계좌.?번호|출금.?계좌|조회.?계좌|빠른.?계좌|account|acct|acno|acctno/i
                ], target);
                for (const part of target.match(/.{1,6}/g) || []) {
                  const blank = inputs().find((el) => /계좌|account|acct|acno/i.test(fieldText(el)) && !String(el.value || '').trim());
                  if (blank) ok = setValue(blank, part) || ok;
                }
                return ok;
              };
              const fillDateRange = () => {
                const dashedFrom = String(input.dateFrom || '');
                const dashedTo = String(input.dateTo || '');
                const plainFrom = dashedFrom.replace(/-/g, '');
                const plainTo = dashedTo.replace(/-/g, '');
                const dotFrom = dashedFrom.replace(/-/g, '.');
                const dotTo = dashedTo.replace(/-/g, '.');
                const fromOk = fillByPattern([
                  /시작일|조회시작|시작.?일자|from|start|fr[_-]?dt|inqr.*start/i
                ], dashedFrom || plainFrom || dotFrom);
                const toOk = fillByPattern([
                  /종료일|조회종료|종료.?일자|to|end|to[_-]?dt|inqr.*end/i
                ], dashedTo || plainTo || dotTo);
                if (fromOk || toOk) return {from: fromOk, to: toOk};
                const dateInputs = inputs().filter((el) => {
                  const value = String(el.value || '');
                  const text = fieldText(el);
                  return /\\d{4}[.\\-/]?\\d{1,2}[.\\-/]?\\d{1,2}/.test(value) || /일자|날짜|기간|date|dt/i.test(text);
                });
                return {
                  from: setValue(dateInputs[0] || null, dashedFrom || plainFrom || dotFrom),
                  to: setValue(dateInputs[1] || null, dashedTo || plainTo || dotTo)
                };
              };
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ');
              const result = {
                attempted: '1',
                mode: 'ibk_quick',
                stage: 'quick_query',
                username: '0',
                login_secret: '0',
                account_no: '0',
                account_secret: '0',
                business_registration_no: '0',
                date_from: '0',
                date_to: '0',
                navigation_clicked: '0',
                query_submitted: '0',
                login_success: /로그아웃|조회결과|거래내역|빠른조회/i.test(text) ? '1' : '0'
              };
              result.username = fillByPattern([
                /이용자.?id|이용자.?아이디|아이디|user|login.*id|cust.*id/i
              ], input.username) ? '1' : '0';
              result.login_secret = setValue(loginPassword(), input.password) ? '1' : '0';
              result.account_no = fillAccountNo() ? '1' : '0';
              result.account_secret = setValue(quickAccountPassword(), input.accountPassword) ? '1' : '0';
              result.business_registration_no = fillByPattern([
                /사업자|사업자등록|주민.?등록|주민.?사업자|business|bizno|registration/i
              ], digits(input.businessRegistrationNo)) ? '1' : '0';
              const dateResult = fillDateRange();
              result.date_from = dateResult.from ? '1' : '0';
              result.date_to = dateResult.to ? '1' : '0';
              const loginLike = result.username === '1' && result.login_secret === '1' && result.account_no === '0';
              const label = clickBest(loginLike ? 'login' : 'query');
              if (label) {
                result.navigation_clicked = '1';
                result.query_submitted = loginLike ? '0' : '1';
              }
              return result;
            }
            """,
            {
                "username": username,
                "password": password,
                "accountNo": account_no,
                "accountPassword": account_password,
                "businessRegistrationNo": business_registration_no,
                "dateFrom": date_from,
                "dateTo": date_to,
            },
            timeout_ms=30000,
            await_promise=True,
        )
    except Exception as exc:
        return {"attempted": "failed", "mode": "ibk_quick", **_safe_browser_error_fields(exc)}
    if not isinstance(raw, dict):
        return {"attempted": "failed", "mode": "ibk_quick"}
    result: dict[str, str] = {"mode": "ibk_quick"}
    for key in (
        "attempted",
        "stage",
        "username",
        "login_secret",
        "account_no",
        "account_secret",
        "business_registration_no",
        "date_from",
        "date_to",
        "navigation_clicked",
        "query_submitted",
        "login_success",
        "error_code",
        "error_type",
    ):
        if key == "stage":
            result[key] = str(raw.get(key) or "unknown")[:40]
        elif key in {"error_code", "error_type"}:
            value = str(raw.get(key) or "").strip()
            if value:
                result[key] = value[:120]
        else:
            result[key] = "1" if str(raw.get(key) or "") == "1" else "0"
    return result


async def _try_prepare_shinhan_query_flow(
    page: Any,
    *,
    flow_mode: str,
    username: str,
    password: str,
    account_no: str,
    account_password: str,
    business_registration_no: str,
    date_from: str,
    date_to: str,
) -> dict[str, str]:
    """Prepare Shinhan query screens for corporate quick/personal simple flows.

    Secrets are sent only to the browser DOM. The returned diagnostics contain
    flags and counts only, never plaintext IDs, passwords, account numbers, or
    business registration numbers.
    """
    if flow_mode not in {"corporate_quick", "individual_simple"}:
        return {"attempted": "0", "mode": "unsupported"}
    if not any([username, password, account_no, account_password, business_registration_no, date_from, date_to]):
        return {"attempted": "0", "mode": flow_mode}
    try:
        raw = await _evaluate_page(
            page,
            """
            (input) => {
              // shinhanQueryFlow
              const visible = (el) => !!(el && !el.disabled && el.offsetParent !== null);
              const digits = (value) => String(value || '').replace(/\\D+/g, '');
              const textOf = (el) => String(
                el?.innerText ||
                el?.value ||
                el?.getAttribute?.('title') ||
                el?.getAttribute?.('aria-label') ||
                el?.getAttribute?.('placeholder') ||
                ''
              ).replace(/\\s+/g, ' ').trim();
              const sameOriginDocuments = () => {
                const docs = [document];
                for (const frame of Array.from(document.querySelectorAll('iframe,frame'))) {
                  try {
                    if (frame.contentDocument) docs.push(frame.contentDocument);
                  } catch (_) {}
                }
                return docs;
              };
              const allElements = (selector) => {
                const result = [];
                for (const doc of sameOriginDocuments()) {
                  try {
                    result.push(...Array.from(doc.querySelectorAll(selector)));
                  } catch (_) {}
                }
                return result;
              };
              const bodyText = () => sameOriginDocuments()
                .map((doc) => String(doc.body?.innerText || ''))
                .join(' ')
                .replace(/\\s+/g, ' ')
                .trim();
              const fieldText = (el) => String([
                el?.id,
                el?.getAttribute?.('name'),
                el?.getAttribute?.('title'),
                el?.getAttribute?.('aria-label'),
                el?.getAttribute?.('placeholder'),
                el?.closest?.('label')?.innerText,
                el?.parentElement?.innerText
              ].filter(Boolean).join(' ')).replace(/\\s+/g, ' ').trim();
              const componentById = (id) => {
                if (!id) return null;
                const candidates = [
                  () => window.$p?.getComponentById?.(id),
                  () => window.WebSquare?.util?.getComponentById?.(id),
                  () => window.WebSquare?.ModelUtil?.getInstance?.(id),
                  () => window.scwin?.[id],
                  () => window[id]
                ];
                for (const getCandidate of candidates) {
                  try {
                    const component = getCandidate();
                    if (component) return component;
                  } catch (_) {}
                }
                return null;
              };
              const componentForElement = (el) => {
                const ids = [
                  el?.id,
                  el?.getAttribute?.('id'),
                  el?.getAttribute?.('data-comp-id'),
                  el?.closest?.('[id]')?.id
                ].filter(Boolean);
                for (const id of ids) {
                  const component = componentById(id);
                  if (component) return component;
                }
                return null;
              };
              const callComponent = (component, methods, ...args) => {
                if (!component) return false;
                for (const method of methods) {
                  try {
                    if (typeof component[method] === 'function') {
                      component[method](...args);
                      return true;
                    }
                  } catch (_) {}
                }
                return false;
              };
              const triggerWebSquareEvent = (el, eventName = 'click') => {
                const component = componentForElement(el);
                let triggered = false;
                const run = () => {
                  try {
                    if (component) {
                      callComponent(component, ['trigger', 'fireEvent', 'dispatchEvent'], eventName);
                      if (eventName === 'click') {
                        callComponent(component, ['trigger', 'fireEvent', 'dispatchEvent'], 'onclick');
                        callComponent(component, ['click', 'userClick']);
                      }
                    }
                  } catch (_) {}
                  try {
                    if (el && typeof el.click === 'function') el.click();
                  } catch (_) {}
                  try {
                    if (el) el.dispatchEvent(new MouseEvent(eventName, {bubbles: true, cancelable: true, view: window}));
                  } catch (_) {}
                };
                if (el || component) {
                  try { window.setTimeout(run, 30); } catch (_) { run(); }
                  triggered = true;
                }
                return triggered;
              };
              const revealForLogin = (el) => {
                if (!el) return false;
                let node = el;
                for (let i = 0; i < 5 && node; i += 1) {
                  try {
                    if (node.hidden) node.hidden = false;
                    if (node.style) {
                      if (node.style.display === 'none') node.style.display = 'block';
                      if (node.style.visibility === 'hidden') node.style.visibility = 'visible';
                    }
                  } catch (_) {}
                  node = node.parentElement;
                }
                return true;
              };
              const setValue = (el, value) => {
                if (!el || !value) return false;
                const component = componentForElement(el);
                let changed = false;
                if (component) {
                  changed = callComponent(component, ['setValue', 'setText', 'setInputValue'], value) || changed;
                }
                if (visible(el)) {
                  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                  if (setter) setter.call(el, value);
                  else el.value = value;
                  el.focus();
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                  changed = true;
                }
                return changed;
              };
              const scoreButton = (label, mode, purpose = 'query') => {
                const value = String(label || '').toLowerCase();
                if (!value) return 0;
                const deny = ['이체', '송금', '납부', '삭제', '해지', '출금'];
                if (deny.some((word) => value.includes(word))) return 0;
                if (purpose === 'notice_confirm') {
                  if (value === '확인') return 130;
                  if (value.includes('확인') || value.includes('닫기')) return 90;
                  return 0;
                }
                if (purpose === 'login') {
                  if (value.includes('로그인')) return 120;
                  if (value.includes('확인') || value.includes('다음')) return 70;
                  return 0;
                }
                if (purpose === 'account_page') {
                  if (value.includes('계좌조회')) return 120;
                  if (value.includes('거래내역')) return 110;
                  if (value.includes('조회내역')) return 90;
                  if (value.includes('간편조회')) return 80;
                  return 0;
                }
                if (mode === 'corporate_quick') {
                  if (value.includes('법인빠른조회') || value.includes('법인 빠른조회')) return 120;
                  if (value.includes('빠른조회') && value.includes('계좌')) return 100;
                  if (value.includes('간편서비스') && value.includes('계좌')) return 70;
                } else {
                  if (value.includes('계좌조회')) return 80;
                }
                if (value === '조회') return 110;
                if (value.includes('조회') && !value.includes('월별')) return 80;
                if (value.includes('조회') || value.includes('검색') || value.includes('확인')) return 45;
                return 0;
              };
              const clickBest = (mode, purpose = 'query') => {
                const candidates = allElements('a,button,input[type=button],input[type=submit]')
                  .filter((el) => visible(el) || (purpose === 'login' && /btn.*login|idlogin|login/i.test(String(el.id || el.name || ''))))
                  .map((el, index) => ({el, index, label: textOf(el)}))
                  .map((item) => {
                    const idScore = purpose === 'login' && /btn.*idlogin|btn.*login|idlogin/i.test(String(item.el.id || item.el.name || '')) ? 150 : 0;
                    return {...item, score: Math.max(scoreButton(item.label, mode, purpose), idScore)};
                  })
                  .filter((item) => item.score > 0)
                  .sort((a, b) => b.score - a.score);
                const item = candidates[0];
                if (!item) return '';
                item.el.focus();
                if (purpose === 'login') revealForLogin(item.el);
                triggerWebSquareEvent(item.el, 'click');
                return item.label.slice(0, 40);
              };
              const inputs = (includeHiddenLogin = false) => allElements('input,textarea').filter((el) => {
                if (visible(el)) return true;
                if (!includeHiddenLogin) return false;
                return /login|id|user|pw|pass|ibx_|비밀번호|비밀/i.test(String(el.id || el.name || el.className || el.title || el.getAttribute?.('placeholder') || '')) && componentForElement(el);
              });
              const firstInput = (patterns, type = '') => inputs().find((el) => {
                if (type && String(el.type || '').toLowerCase() !== type) return false;
                const text = fieldText(el);
                return patterns.some((pattern) => pattern.test(text));
              }) || null;
              const firstLoginInput = (patterns, type = '') => inputs(true).find((el) => {
                if (type && String(el.type || '').toLowerCase() !== type) return false;
                const text = fieldText(el);
                return patterns.some((pattern) => pattern.test(text));
              }) || null;
              const fillByPattern = (patterns, value, type = '') => setValue(firstInput(patterns, type), value);
              const fillLoginByPattern = (patterns, value, type = '') => {
                const el = firstLoginInput(patterns, type);
                revealForLogin(el);
                return setValue(el, value);
              };
              const hasInput = (patterns, type = '') => !!firstInput(patterns, type);
              const hasLoginInput = (patterns, type = '') => !!firstLoginInput(patterns, type);
              const hasLoginNotice = () => /이용 가능한 서비스가 제한|단순 계좌 조회/i.test(bodyText());
              const isRedirectLoginPage = () => {
                try {
                  const currentMenu = String(window.shbComm?.menu?.getCurrentMenuCode?.() || window.shbComm?.menu?.currentMenuCode || '');
                  const redirectUrl = String(window.shbComm?.menu?.redirectUrl || '');
                  return currentMenu === '210000000000' && !!redirectUrl;
                } catch (_) {
                  return false;
                }
              };
              const hasLoginFields = () => {
                const text = bodyText();
                const loginPanelText = /이용자\\s*ID\\s*로그인|이용자ID\\s*로그인|아이디\\s*로그인/i.test(text);
                const visibleFields = hasInput([
                  /아이디|이용자.?id|user|login.*id|cust.*id|member.*id/i
                ]) && hasInput([
                  /비밀번호|password|passwd|login.*pw/i
                ], 'password');
                if (!loginPanelText && !visibleFields && !isRedirectLoginPage()) return false;
                return hasLoginInput([
                  /아이디|이용자.?id|user|login.*id|cust.*id|member.*id/i
                ]) && hasLoginInput([
                  /비밀번호|password|passwd|login.*pw/i
                ], 'password');
              };
              const hasAccountQueryFields = () => (
                hasInput([/계좌번호|account|acct/i]) ||
                hasInput([/계좌.*비밀번호|계좌.*암호|account.*password|account.*pw|acct.*pw/i], 'password') ||
                allElements('select,input[type=radio],input[type=checkbox]').some(visible)
              );
              const fillAccountNumber = () => {
                const target = digits(input.accountNo);
                if (!target) return false;
                const candidates = inputs()
                  .filter((el) => String(el.type || '').toLowerCase() !== 'password')
                  .filter((el) => {
                    const text = fieldText(el);
                    return /계좌번호|계좌.*직접|직접입력|account|acct|acno|acctno/i.test(text);
                  });
                let filled = false;
                for (const candidate of candidates) {
                  filled = setValue(candidate, target) || filled;
                }
                return filled;
              };
              const selectAccount = () => {
                const target = digits(input.accountNo);
                const suffix = target.slice(-4);
                if (!target) return false;
                for (const select of allElements('select').filter(visible)) {
                  const options = Array.from(select.options || []);
                  let match = options.find((option) => {
                    const optionDigits = digits(option.textContent || option.value);
                    return optionDigits && (optionDigits === target || optionDigits.endsWith(suffix));
                  });
                  if (!match && options.length === 2) {
                    match = options.find((option) => digits(option.textContent || option.value));
                  }
                  if (match) {
                    select.value = match.value;
                    select.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                  }
                }
                const choice = allElements('input[type=radio],input[type=checkbox]')
                  .filter(visible)
                  .find((el) => {
                    const rowText = String(el.closest('tr,li,div,label')?.innerText || '');
                    const rowDigits = digits(rowText);
                    return rowDigits && (rowDigits === target || rowDigits.endsWith(suffix));
                  });
                if (choice) {
                  choice.click();
                  return true;
                }
                return false;
              };
              const transKeySeq = (value) => {
                const lowerMap = {'1':1,'2':2,'3':4,'4':5,'5':6,'6':7,'7':8,'8':9,'9':11,'0':12,'q':13,'w':14,'e':16,'r':17,'t':18,'y':19,'u':20,'i':21,'o':23,'p':24,'a':25,'s':27,'d':28,'f':29,'g':30,'h':31,'j':32,'k':34,'z':35,'x':37,'c':38,'v':39,'b':40,'n':41,'m':42,'l':44,'-':45,'=':46,'[':48,']':49,';':50,"'":51,',':52,'.':53,'/':54};
                const shiftMap = {'~':0,'!':1,'@':2,'#':4,'$':5,'%':6,'^':7,'&':8,'*':9,'(':11,')':12,'_':45,'+':46,'{':48,'}':49,':':50,'"':51,'<':52,'>':53,'?':54};
                const seq = [];
                for (const ch of String(value || '')) {
                  if (/[A-Z]/.test(ch)) seq.push(55, lowerMap[ch.toLowerCase()]);
                  else if (Object.prototype.hasOwnProperty.call(shiftMap, ch)) seq.push(55, shiftMap[ch]);
                  else if (Object.prototype.hasOwnProperty.call(lowerMap, ch)) seq.push(lowerMap[ch]);
                  else return [];
                }
                return seq.filter((item) => Number.isFinite(item));
              };
              const setTransKeyPassword = (el, value) => {
                if (!el || !value) return false;
                const ownerWindow = el.ownerDocument?.defaultView || window;
                const tkRoot = ownerWindow.tk || window.tk;
                const transkeyRoot = ownerWindow.transkey || window.transkey;
                const tkObj = transkeyRoot?.[el.id] || transkeyRoot?.[el.name];
                if (!tkRoot || !tkObj || typeof tkRoot.getKeyByIndex !== 'function' || typeof tkRoot.getEncData !== 'function') {
                  return false;
                }
                const seq = transKeySeq(value);
                if (!seq.length) return false;
                try {
                  if (typeof tkRoot.onKeyboard === 'function') tkRoot.onKeyboard(el);
                  if (!tkObj.allocate && typeof tkObj.allocation === 'function') tkObj.allocation();
                  tkObj.inputObj = el;
                  tkRoot.now = tkObj;
                  try {
                    if (typeof tkRoot.setHiddenField === 'function') tkRoot.setHiddenField(el);
                  } catch (_) {}
                  if (tkObj.hidden) tkObj.hidden.value = '';
                  if (tkObj.hmac) tkObj.hmac.value = '';
                  el.value = '';
                  let shiftOn = false;
                  for (const code of seq) {
                    if (code === 55 || code === 56) {
                      shiftOn = true;
                      continue;
                    }
                    const originalType = tkObj.keyTypeIndex;
                    tkObj.keyTypeIndex = shiftOn ? 'u ' : 'l ';
                    const key = tkRoot.getKeyByIndex(code, 'qwerty');
                    const point = key?.xpoints && key?.ypoints ? [key.xpoints[0], key.ypoints[0]] : null;
                    if (!point) return false;
                    const encrypted = tkRoot.getEncData(point[0], point[1]);
                    if (!encrypted) return false;
                    if (tkObj.hidden) tkObj.hidden.value += '$' + encrypted;
                    el.value += '*';
                    tkObj.keyTypeIndex = originalType;
                    shiftOn = false;
                  }
                  try {
                    if (typeof tkRoot.inputFillEncData === 'function') tkRoot.inputFillEncData(el);
                    else if (typeof tkRoot.fillEncData === 'function') tkRoot.fillEncData();
                  } catch (_) {}
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                  return !!(tkObj.hidden && String(tkObj.hidden.value || '').length > 0);
                } catch (_) {
                  return false;
                }
              };
              const fillAccountSecret = () => {
                const patterns = [
                  /계좌.*비밀번호|계좌.*암호|account.*password|account.*pw|acct.*pw|숫자\\s*4자리|4자리/i
                ];
                const target = firstInput(patterns, 'password');
                if (setTransKeyPassword(target, input.accountPassword)) return true;
                if (fillByPattern(patterns, input.accountPassword, 'password')) return true;
                const passwords = inputs().filter((el) => String(el.type || '').toLowerCase() === 'password');
                const candidate = passwords.find((el) => /계좌|숫자\\s*4자리|4자리/i.test(fieldText(el))) || passwords[0] || null;
                if (setTransKeyPassword(candidate, input.accountPassword)) return true;
                return setValue(candidate, input.accountPassword);
              };
              const fillDateRange = () => {
                const dotFrom = String(input.dateFrom || '').replace(/-/g, '.');
                const dotTo = String(input.dateTo || '').replace(/-/g, '.');
                const fromOk = fillByPattern([
                  /시작일|조회시작|조회기간.*시작|from|start|fr[_-]?dt|from[_-]?date/i
                ], dotFrom || input.dateFrom);
                const toOk = fillByPattern([
                  /종료일|조회종료|조회기간.*종료|to|end|to[_-]?dt|to[_-]?date/i
                ], dotTo || input.dateTo);
                if (fromOk || toOk) return {from: fromOk, to: toOk};
                const dateInputs = inputs().filter((el) => {
                  const type = String(el.type || '').toLowerCase();
                  const value = String(el.value || '');
                  const text = fieldText(el);
                  return type === 'date' || /\\d{4}[.\\-/]\\d{1,2}[.\\-/]\\d{1,2}/.test(value) || /일자|날짜|기간|date|dt/i.test(text);
                });
                const first = dateInputs[0] || null;
                const second = dateInputs[1] || null;
                return {
                  from: setValue(first, dotFrom || input.dateFrom),
                  to: setValue(second, dotTo || input.dateTo)
                };
              };
              const result = {
                attempted: '1',
                mode: input.mode,
                stage: 'unknown',
                navigation_clicked: '0',
                websquare_triggered: '0',
                account_page_navigation: '0',
                account_page_direct_hash: '0',
                notice_confirm: '0',
                login_success: '0',
                username: '0',
                login_secret: '0',
                account_no: '0',
                account_direct_input: '0',
                account_selected: '0',
                account_resolved: '0',
                account_secret: '0',
                business_registration_no: '0',
                date_from: '0',
                date_to: '0',
                query_submitted: '0'
              };
              if (input.mode === 'individual_simple') {
                if (hasLoginFields()) {
                  result.stage = 'login';
                  result.username = fillLoginByPattern([
                    /아이디|이용자.?id|user|login.*id|cust.*id|member.*id/i
                  ], input.username) ? '1' : '0';
                  result.login_secret = fillLoginByPattern([
                    /비밀번호|password|passwd|login.*pw/i
                  ], input.password, 'password') ? '1' : '0';
                  if (result.username === '1' && result.login_secret === '1') {
                    const loginLabel = clickBest(input.mode, 'login');
                    result.navigation_clicked = loginLabel ? '1' : '0';
                    result.websquare_triggered = loginLabel ? '1' : '0';
                  }
                  return result;
                }
                if (hasLoginNotice()) {
                  result.stage = 'login_notice_confirm';
                  result.login_success = '1';
                  result.notice_confirm = clickBest(input.mode, 'notice_confirm') ? '1' : '0';
                  result.navigation_clicked = result.notice_confirm;
                  return result;
                }
                if (!hasAccountQueryFields()) {
                  result.stage = 'account_page_navigation';
                  result.login_success = '1';
                  const accountPageLabel = clickBest(input.mode, 'account_page');
                  result.account_page_navigation = accountPageLabel ? '1' : '0';
                  result.navigation_clicked = result.account_page_navigation;
                  try {
                    if (String(window.location.href || '').includes('/rib/easy/index.jsp')) {
                      try {
                        if (window.shbComm?.menu) {
                          window.shbComm.menu.redirectUrl = '210101000000';
                        }
                      } catch (_) {}
                      try {
                        if (window.shbComm && typeof window.shbComm.goPage === 'function') {
                          window.setTimeout(() => {
                            try { window.shbComm.goPage('210101000000'); } catch (_) {}
                          }, 30);
                        }
                      } catch (_) {}
                      window.location.hash = '210101000000';
                      window.dispatchEvent(new HashChangeEvent('hashchange'));
                      window.setTimeout(() => {
                        try { window.location.hash = '210101000000'; } catch (_) {}
                      }, 120);
                      result.account_page_navigation = '1';
                      result.navigation_clicked = '1';
                      result.websquare_triggered = '1';
                      result.account_page_direct_hash = '1';
                    }
                  } catch (_) {}
                  return result;
                }
                result.stage = 'account_query';
                result.login_success = '1';
                result.username = fillByPattern([
                  /아이디|이용자.?id|user|login.*id|cust.*id|member.*id/i
                ], input.username) ? '1' : '0';
                result.login_secret = fillByPattern([
                  /비밀번호|password|passwd|login.*pw/i
                ], input.password, 'password') ? '1' : '0';
              } else {
                result.stage = 'corporate_quick';
                result.navigation_clicked = clickBest(input.mode, 'account_page') ? '1' : '0';
              }
              result.account_selected = selectAccount() ? '1' : '0';
              result.account_no = fillAccountNumber() ? '1' : '0';
              result.account_direct_input = result.account_no;
              result.account_resolved = (result.account_selected === '1' || result.account_no === '1') ? '1' : '0';
              result.account_secret = fillAccountSecret() ? '1' : '0';
              result.business_registration_no = fillByPattern([
                /사업자|사업자등록|business|bizno|registration/i
              ], input.businessRegistrationNo) ? '1' : '0';
              const dateResult = fillDateRange();
              result.date_from = dateResult.from ? '1' : '0';
              result.date_to = dateResult.to ? '1' : '0';
              const submittedLabel = clickBest(input.mode);
              result.query_submitted = submittedLabel ? '1' : '0';
              return result;
            }
            """,
            {
                "mode": flow_mode,
                "username": username,
                "password": password,
                "accountNo": account_no,
                "accountPassword": account_password,
                "businessRegistrationNo": business_registration_no,
                "dateFrom": date_from,
                "dateTo": date_to,
            },
            timeout_ms=30000,
        )
    except Exception as exc:
        return {"attempted": "failed", "mode": flow_mode, **_safe_browser_error_fields(exc)}
    if not isinstance(raw, dict):
        return {"attempted": "failed", "mode": flow_mode}
    result: dict[str, str] = {"mode": flow_mode}
    for key in (
        "attempted",
        "stage",
        "navigation_clicked",
        "websquare_triggered",
        "account_page_navigation",
        "account_page_direct_hash",
        "notice_confirm",
        "login_success",
        "username",
        "login_secret",
        "account_no",
        "account_direct_input",
        "account_selected",
        "account_resolved",
        "account_secret",
        "business_registration_no",
        "date_from",
        "date_to",
        "query_submitted",
        "error_code",
        "error_type",
    ):
        if key == "stage":
            result[key] = str(raw.get(key) or "unknown")[:40]
        elif key in {"error_code", "error_type"}:
            value = str(raw.get(key) or "").strip()
            if value:
                result[key] = value[:120]
        else:
            result[key] = "1" if str(raw.get(key) or "") == "1" else "0"
    return result


async def _try_shinhan_individual_login_step(
    page: Any,
    *,
    username: str,
    password: str,
) -> dict[str, str]:
    """Run a small Shinhan ID-login step before the larger query state machine."""
    if not username or not password:
        return {"attempted": "0"}
    await _close_shinhan_security_notice(page)
    keyboard_result = await _try_shinhan_individual_keyboard_login_step(
        page,
        username=username,
        password=password,
    )
    if (
        keyboard_result.get("attempted") == "1"
        and keyboard_result.get("keyboard_secret") == "1"
        and keyboard_result.get("navigation_clicked") == "1"
    ):
        return keyboard_result
    try:
        raw = await _evaluate_page(
            page,
            """
	            async (input) => {
		              const byId = (id) => document.getElementById(id);
		              const visible = (el) => !!(el && !el.disabled && el.offsetParent !== null);
		              const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
		              const startedAt = performance.now();
		              const authText = () => String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
		              const authUrl = () => String(window.location.href || '');
		              const loggedInMarker = () => {
		                const currentText = authText();
		                const currentUrl = authUrl();
		                if (!/이용자\\s*ID\\s*로그인|이용자ID\\s*로그인|아이디\\s*로그인/i.test(currentText)
		                  && /로그아웃|조회기간|계좌조회|거래내역|출금가능|잔액|빠른조회/i.test(currentText)) {
		                  return 'post_login_text';
		                }
		                if (/#210101|acct|inq|조회/.test(currentUrl) && !/login/i.test(currentUrl)) {
		                  return 'post_login_url';
		                }
		                return '';
		              };
		              const firstVisible = (selectors) => {
	                for (const selector of selectors) {
	                  try {
	                    const found = Array.from(document.querySelectorAll(selector)).find((el) => visible(el));
	                    if (found) return found;
	                  } catch (_) {}
	                }
	                return null;
	              };
	              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
	              const loginIdEl = byId('ibx_loginId') || byId('ibx_loginId_cib') || byId('mf_wfm_main_ibx_loginId')
	                || firstVisible([
	                  'input[id$="_ibx_loginId"]',
	                  'input[id*="loginId"]',
	                  'input[title*="ID"]',
	                  'input[placeholder*="로그인ID"]'
	                ]);
	              const loginPasswordEl = byId('비밀번호') || byId('비밀번호_cib') || byId('wq_uuid_769_scr_pwd')
	                || firstVisible([
	                  'input[id$="_scr_pwd"]',
	                  'input[id*="scr_pwd"]',
	                  'input[title*="비밀번호"]',
	                  'input[placeholder*="비밀번호"]',
	                  'input[type="password"]'
	                ]);
              const isRedirectLoginPage = (() => {
                try {
                  const currentMenu = String(window.shbComm?.menu?.getCurrentMenuCode?.() || window.shbComm?.menu?.currentMenuCode || '');
                  const redirectUrl = String(window.shbComm?.menu?.redirectUrl || '');
                  return currentMenu === '210000000000' && !!redirectUrl;
                } catch (_) {
                  return false;
                }
              })();
              const hasLoginPanel = /이용자\\s*ID\\s*로그인|이용자ID\\s*로그인|아이디\\s*로그인/i.test(text)
                || !!loginIdEl
                || !!loginPasswordEl
                || isRedirectLoginPage;
              const componentById = (id) => {
                if (!id) return null;
                const candidates = [
                  () => window.$p?.getComponentById?.(id),
                  () => window.WebSquare?.util?.getComponentById?.(id),
                  () => window.WebSquare?.ModelUtil?.getInstance?.(id),
                  () => window[id]
                ];
                for (const getter of candidates) {
                  try {
                    const component = getter();
                    if (component) return component;
                  } catch (_) {}
                }
                return null;
              };
              const call = (component, methods, ...args) => {
                if (!component) return false;
                for (const method of methods) {
                  try {
                    if (typeof component[method] === 'function') {
                      component[method](...args);
                      return true;
                    }
                  } catch (_) {}
                }
                return false;
              };
              const setField = (id, value) => {
                if (!value) return false;
                const el = byId(id);
                const component = componentById(id);
                let ok = call(component, ['setValue', 'setText', 'setInputValue'], value);
                if (el) {
                  try {
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                    if (setter) setter.call(el, value);
                    else el.value = value;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    ok = true;
                  } catch (_) {}
                }
                return ok;
              };
              const transKeySeq = (value) => {
                const lowerMap = {'1':1,'2':2,'3':4,'4':5,'5':6,'6':7,'7':8,'8':9,'9':11,'0':12,'q':13,'w':14,'e':16,'r':17,'t':18,'y':19,'u':20,'i':21,'o':23,'p':24,'a':25,'s':27,'d':28,'f':29,'g':30,'h':31,'j':32,'k':34,'z':35,'x':37,'c':38,'v':39,'b':40,'n':41,'m':42,'l':44,'-':45,'=':46,'[':48,']':49,';':50,"'":51,',':52,'.':53,'/':54};
                const shiftMap = {'~':0,'!':1,'@':2,'#':4,'$':5,'%':6,'^':7,'&':8,'*':9,'(':11,')':12,'_':45,'+':46,'{':48,'}':49,':':50,'"':51,'<':52,'>':53,'?':54};
                const seq = [];
                for (const ch of String(value || '')) {
                  if (/[A-Z]/.test(ch)) {
                    seq.push(55, lowerMap[ch.toLowerCase()]);
                  } else if (Object.prototype.hasOwnProperty.call(shiftMap, ch)) {
                    seq.push(55, shiftMap[ch]);
                  } else if (Object.prototype.hasOwnProperty.call(lowerMap, ch)) {
                    seq.push(lowerMap[ch]);
                  } else {
                    return [];
                  }
                }
                return seq.filter((item) => Number.isFinite(item));
              };
              const setTransKeyPassword = async (id, value) => {
                if (!value) return false;
                const inputEl = byId(id);
                const tkObj = window.transkey?.[id];
                if (!inputEl || !tkObj || !window.tk || typeof window.tk.getKeyByIndex !== 'function' || typeof window.tk.getEncData !== 'function') {
                  return false;
                }
	                try {
	                  if (typeof window.tk.onKeyboard === 'function') window.tk.onKeyboard(inputEl);
	                  if (!tkObj.allocate && typeof tkObj.allocation === 'function') tkObj.allocation();
	                } catch (_) {}
	                for (let i = 0; i < 20 && !tkObj.allocate; i += 1) {
	                  await sleep(100);
	                }
	                const seq = transKeySeq(value);
	                if (!seq.length) return false;
	                try {
	                  tkObj.inputObj = inputEl;
	                  window.tk.now = tkObj;
	                  try {
	                    if (typeof window.tk.setHiddenField === 'function') window.tk.setHiddenField(inputEl);
	                  } catch (_) {}
	                  if (tkObj.hidden) tkObj.hidden.value = '';
	                  if (tkObj.hmac) tkObj.hmac.value = '';
	                  inputEl.value = '';
	                  let shiftOn = false;
                  for (const code of seq) {
                    if (code === 55 || code === 56) {
                      shiftOn = true;
                      continue;
                    }
                    const originalType = tkObj.keyTypeIndex;
                    tkObj.keyTypeIndex = shiftOn ? 'u ' : 'l ';
                    const key = window.tk.getKeyByIndex(code, 'qwerty');
                    const point = key?.xpoints && key?.ypoints ? [key.xpoints[0], key.ypoints[0]] : null;
                    if (!point) return false;
                    const encrypted = window.tk.getEncData(point[0], point[1]);
	                    if (!encrypted) return false;
	                    if (tkObj.hidden) tkObj.hidden.value += '$' + encrypted;
	                    inputEl.value += '*';
	                    tkObj.keyTypeIndex = originalType;
	                    shiftOn = false;
	                  }
	                  try {
	                    if (typeof window.tk.inputFillEncData === 'function') {
	                      const filled = window.tk.inputFillEncData(inputEl);
	                      if (filled && tkObj.hidden && filled.hidden) tkObj.hidden.value = filled.hidden;
	                      if (filled && tkObj.hmac && filled.hmac) tkObj.hmac.value = String(filled.hmac);
	                    } else if (typeof window.tk.fillEncData === 'function') {
	                      window.tk.fillEncData();
	                    }
	                  } catch (_) {}
	                  inputEl.dispatchEvent(new Event('input', {bubbles: true}));
	                  inputEl.dispatchEvent(new Event('change', {bubbles: true}));
	                  return !!(tkObj.hidden && String(tkObj.hidden.value || '').length > 0);
	                } catch (_) {
	                  return false;
	                }
              };
              const clickLogin = () => {
                try {
                  if (window.shbComm) window.shbComm.ASTX_INSTALL = true;
                  if (window.$ASTX2 && typeof window.$ASTX2.getPCLOGData === 'function') {
                    window.$ASTX2.getPCLOGData = function(_a, successCb) {
                      if (typeof successCb === 'function') successCb({pclog_data: ''});
                    };
                  }
                } catch (_) {}
                try {
                  if (window.shbObj && typeof window.shbObj.fncIdLogin === 'function') {
                    window.setTimeout(() => {
                      try { window.shbObj.fncIdLogin(); } catch (_) {}
                    }, 30);
                    return true;
                  }
                } catch (_) {}
	                const candidates = ['btn_idLogin', 'btn_idLogin_cib', 'mf_wfm_main_btn_login'];
                for (const id of candidates) {
                  const el = byId(id);
                  const component = componentById(id);
                  if (el || component) {
                    try {
                      window.setTimeout(() => {
                        try { call(component, ['trigger', 'fireEvent', 'dispatchEvent'], 'onclick'); } catch (_) {}
                        try { call(component, ['trigger', 'fireEvent', 'dispatchEvent'], 'click'); } catch (_) {}
                        try { call(component, ['click', 'userClick']); } catch (_) {}
                        try {
                          if (el) {
                            el.click();
                            el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                          }
                        } catch (_) {}
                      }, 30);
                    } catch (_) {
                      try { if (el) el.click(); } catch (_) {}
                    }
                    return true;
                  }
                }
                const fallback = Array.from(document.querySelectorAll('a,button,input[type=button],input[type=submit]'))
                  .filter((el) => visible(el) && String(el.innerText || el.value || '').includes('로그인'))[0];
                if (fallback) {
                  try { window.setTimeout(() => fallback.click(), 30); } catch (_) { fallback.click(); }
                  return true;
                }
                return false;
              };
              const openAccountInquiry = () => {
                try {
                  if (window.shbComm?.menu) {
                    window.shbComm.menu.redirectUrl = '210101000000';
                  }
                } catch (_) {}
                try {
                  if (window.shbComm && typeof window.shbComm.goPage === 'function') {
                    window.setTimeout(() => {
                      try { window.shbComm.goPage('210101000000'); } catch (_) {}
                    }, 30);
                    return true;
                  }
                } catch (_) {}
                try {
                  window.location.hash = '210101000000';
                  window.dispatchEvent(new HashChangeEvent('hashchange'));
                  return true;
                } catch (_) {}
                return false;
              };
              if (!loginIdEl && !loginPasswordEl && String(window.location.href || '').includes('#210000000000')) {
                const opened = openAccountInquiry();
                return {
                  attempted: opened ? '1' : '0',
                  stage: opened ? 'account_page_navigation' : 'hidden_login_panel',
                  username: '0',
                  login_secret: '0',
                  transkey_secret: '0',
                  navigation_clicked: opened ? '1' : '0',
                  websquare_triggered: opened ? '1' : '0',
                  account_page_navigation: opened ? '1' : '0'
                };
              }
              if (!hasLoginPanel) return {attempted: '0', stage: 'not_login_panel'};
	              const loginId = loginIdEl?.id || 'ibx_loginId';
	              const loginPassword = loginPasswordEl?.id || '비밀번호';
	              const usernameOkPrimary = setField(loginId, input.username);
	              const usernameOkCib = loginId === 'ibx_loginId_cib' ? false : setField('ibx_loginId_cib', input.username);
	              const usernameOk = usernameOkPrimary || usernameOkCib;
	              const transkeyOkPrimary = await setTransKeyPassword(loginPassword, input.password);
	              const transkeyOkCib = loginPassword === '비밀번호_cib' ? false : await setTransKeyPassword('비밀번호_cib', input.password);
	              const transkeyOk = transkeyOkPrimary || transkeyOkCib;
	              const passwordOkPrimary = transkeyOkPrimary || setField(loginPassword, input.password);
	              const passwordOkCib = transkeyOkCib || setField('비밀번호_cib', input.password);
	              const passwordOk = passwordOkPrimary || passwordOkCib;
	              const submitted = usernameOk && passwordOk ? clickLogin() : false;
	              let loginSuccess = '0';
	              let loginSuccessReason = '';
	              if (submitted) {
	                for (let i = 0; i < 30; i += 1) {
	                  await sleep(500);
	                  loginSuccessReason = loggedInMarker();
	                  if (loginSuccessReason) {
	                    loginSuccess = '1';
	                    break;
	                  }
	                  const currentText = authText();
	                  if (/비밀번호.*(불일치|오류|틀)|로그인.*(실패|오류)|보안프로그램.*설치/i.test(currentText)) {
	                    loginSuccessReason = 'login_error_or_security_notice';
	                    break;
	                  }
	                }
	              }
	              return {
	                attempted: '1',
	                stage: 'login',
	                username: usernameOk ? '1' : '0',
	                login_secret: passwordOk ? '1' : '0',
	                transkey_secret: transkeyOk ? '1' : '0',
	                navigation_clicked: submitted ? '1' : '0',
	                websquare_triggered: submitted ? '1' : '0',
	                login_submitted: submitted ? '1' : '0',
	                login_success: loginSuccess,
	                login_success_reason: loginSuccessReason,
	                login_elapsed_ms: String(Math.round(performance.now() - startedAt))
	              };
	            }
	            """,
            {"username": username, "password": password},
            timeout_ms=25000,
            await_promise=True,
        )
    except Exception as exc:
        return {"attempted": "failed", **_safe_browser_error_fields(exc)}
    if not isinstance(raw, dict):
        return {"attempted": "failed"}
    result: dict[str, str] = {"attempted": "1" if str(raw.get("attempted") or "") == "1" else str(raw.get("attempted") or "0")[:20]}
    for key in (
        "stage",
        "username",
        "login_secret",
        "transkey_secret",
        "navigation_clicked",
        "websquare_triggered",
        "login_submitted",
        "login_success",
        "account_page_navigation",
        "login_success_reason",
        "login_elapsed_ms",
        "error_code",
        "error_type",
    ):
        value = str(raw.get(key) or "").strip()
        if key == "stage":
            result[key] = value[:40] or "unknown"
        elif key in {"error_code", "error_type"}:
            if value:
                result[key] = value[:120]
        elif key == "login_success_reason":
            if value:
                result[key] = value[:120]
        elif key == "login_elapsed_ms":
            if value.isdigit():
                result[key] = value[:12]
        else:
            result[key] = "1" if value == "1" else "0"
    return result


async def _prefer_shinhan_idpw_login_after_auth_challenge(page: Any, portal_url: str) -> dict[str, str]:
    """Reset a Shinhan certificate prompt back to the saved ID/PW login flow."""
    result: dict[str, str] = {"attempted": "1"}
    runner = getattr(page, "_run_browser_command", None)
    if callable(runner):
        try:
            close_result = await runner(
                "browser_close_tab",
                {"url_pattern": "fincert|yeskey|cert", "keep_last": False},
                command_timeout_seconds=10,
                queue_wait_timeout_seconds=5,
            )
            result["certificate_tab_close_attempted"] = "1"
            close_payload = close_result if isinstance(close_result, dict) and "closed" in close_result else None
            if not isinstance(close_payload, dict) and isinstance(close_result, dict):
                close_payload = close_result.get("data")
            if not isinstance(close_payload, dict) and isinstance(close_result, dict):
                close_payload = close_result.get("result") if isinstance(close_result, dict) else None
            if isinstance(close_payload, dict):
                result["certificate_tab_closed"] = "1" if int(close_payload.get("closed") or 0) > 0 else "0"
        except TypeError:
            try:
                await runner("browser_close_tab", {"url_pattern": "fincert|yeskey|cert", "keep_last": False})
                result["certificate_tab_close_attempted"] = "1"
            except Exception:
                result["certificate_tab_close_attempted"] = "failed"
        except Exception:
            result["certificate_tab_close_attempted"] = "failed"
    if portal_url and hasattr(page, "goto"):
        try:
            await page.goto(portal_url, wait_until="domcontentloaded", timeout=30000)
            result["portal_reloaded_for_idpw"] = "1"
        except Exception:
            result["portal_reloaded_for_idpw"] = "failed"
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
    try:
        selected = await _evaluate_page(
            page,
            """
            () => {
              const visible = (el) => !!(el && !el.disabled && el.offsetParent !== null);
              const textOf = (el) => String(
                el?.innerText ||
                el?.value ||
                el?.getAttribute?.('title') ||
                el?.getAttribute?.('aria-label') ||
                ''
              ).replace(/\\s+/g, ' ').trim();
              const score = (el) => {
                const label = textOf(el).toLowerCase();
                const id = String(el?.id || el?.name || '').toLowerCase();
                const value = `${label} ${id}`;
                if (/금융인증|공동인증|인증서|fincert|certificate/.test(value)) return 0;
                if (/이용자\\s*id\\s*로그인|아이디\\s*로그인|id\\s*\\/\\s*pw|idpw|idlogin/.test(value)) return 120;
                if (/id/.test(value) && /로그인|login/.test(value)) return 80;
                return 0;
              };
              const item = Array.from(document.querySelectorAll('a,button,input[type=button],input[type=submit]'))
                .filter((el) => visible(el) || /idlogin|login/.test(String(el.id || el.name || '').toLowerCase()))
                .map((el) => ({el, score: score(el)}))
                .filter((item) => item.score > 0)
                .sort((a, b) => b.score - a.score)[0];
              if (!item) return {selected: '0'};
              try { item.el.focus(); } catch (_) {}
              try { item.el.click(); } catch (_) {}
              try { item.el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window})); } catch (_) {}
              return {selected: '1'};
            }
            """,
            timeout_ms=10000,
        )
        result["idpw_login_panel_selected"] = "1" if isinstance(selected, dict) and selected.get("selected") == "1" else "0"
    except Exception:
        result["idpw_login_panel_selected"] = "failed"
    return result


def _pc_agent_tabs_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get("tabs"),
        payload.get("data", {}).get("tabs") if isinstance(payload.get("data"), dict) else None,
        payload.get("result", {}).get("tabs") if isinstance(payload.get("result"), dict) else None,
        payload.get("result", {}).get("result", {}).get("tabs")
        if isinstance(payload.get("result"), dict) and isinstance(payload.get("result", {}).get("result"), dict)
        else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


async def _visible_page_url(page: Any) -> str:
    try:
        return str(await _evaluate_page(page, "window.location.href", timeout_ms=8000) or "")
    except Exception:
        return ""


async def _detect_shinhan_auth_challenge(page: Any, pages: Any = None) -> dict[str, str]:
    """Detect certificate/identity UI from URLs, frames, and bounded page text.

    Shinhan opens YESKEY in a cross-origin iframe or a separate popup, so the
    main document's text alone is not sufficient.  Only URLs and redacted
    labels are inspected; certificate passwords and other secrets are never
    read or returned.
    """
    urls: list[str] = []
    runner = getattr(page, "_run_browser_command", None)
    if callable(runner):
        try:
            tabs_payload = await runner(
                "browser_tabs",
                {},
                queue_wait_timeout_seconds=5,
                command_timeout_seconds=10,
            )
            for tab in _pc_agent_tabs_from_payload(tabs_payload):
                for key in ("url", "title"):
                    value = str(tab.get(key) or "").strip().lower()
                    if value:
                        urls.append(value)
        except Exception:
            pass
    for candidate in [page, *(list(pages or []))]:
        url = await _visible_page_url(candidate)
        if url:
            urls.append(url.lower())
        for frame in list(getattr(candidate, "frames", []) or []):
            frame_url = str(getattr(frame, "url", "") or "").lower()
            if frame_url:
                urls.append(frame_url)
    url_text = " ".join(urls)
    if "4user.yeskey.or.kr/fincert" in url_text or "fincert" in url_text:
        return {
            "screen_state": "certificate_password_required",
            "screen_reason_code": "SHINHAN_FINCERT_IFRAME_DETECTED",
            "screen_suggested_action": "complete_financial_certificate_then_retry_same_work_key",
            "suggested_action": "complete_financial_certificate_then_retry_same_work_key",
            "screen_requires_operator": "1",
            "last_observed_stage": "financial certificate iframe",
        }
    if "bank.shinhan.com" in url_text and any(token in url_text for token in ("permission", "popup", "cert")):
        return {
            "screen_state": "identity_check_required",
            "screen_reason_code": "SHINHAN_PERMISSION_POPUP_DETECTED",
            "screen_suggested_action": "complete_financial_certificate_then_retry_same_work_key",
            "suggested_action": "complete_financial_certificate_then_retry_same_work_key",
            "screen_requires_operator": "1",
            "last_observed_stage": "permission popup",
        }
    for candidate in [page, *(list(pages or []))]:
        try:
            text = _safe_portal_text(str(await _evaluate_page(
                candidate,
                "document.body ? String(document.body.innerText || '').slice(0, 2000) : ''",
                timeout_ms=1500,
            ) or "")).lower()
        except Exception:
            continue
        if re.search(r"금융인증서|인증서\s*(비밀번호|암호)|인증서\s*선택", text):
            return {
                "screen_state": "certificate_password_required",
                "screen_reason_code": "SHINHAN_FINANCIAL_CERTIFICATE_PROMPT",
                "screen_suggested_action": "complete_financial_certificate_then_retry_same_work_key",
                "suggested_action": "complete_financial_certificate_then_retry_same_work_key",
                "screen_requires_operator": "1",
                "last_observed_stage": "financial certificate iframe",
            }
        if re.search(r"본인인증|추가\s*인증|권한을 허용|접근 권한", text):
            return {
                "screen_state": "identity_check_required",
                "screen_reason_code": "SHINHAN_IDENTITY_CHECK_PROMPT",
                "screen_suggested_action": "complete_financial_certificate_then_retry_same_work_key",
                "suggested_action": "complete_financial_certificate_then_retry_same_work_key",
                "screen_requires_operator": "1",
                "last_observed_stage": "identity check",
            }
    return {}


async def _select_bank_page(pages: Any, portal_url: str) -> tuple[Any | None, str, bool]:
    page_list = list(pages or [])
    if not page_list:
        return None, "", False
    first_page = page_list[0]
    first_url = ""
    for page in page_list:
        current_url = await _visible_page_url(page)
        if not first_url:
            first_url = current_url
        if _portal_url_reusable(current_url, portal_url):
            return page, current_url, True
    return first_page, first_url, False


def _bank_session_recovery_plan(error_code: str = "") -> str:
    code = str(error_code or "").strip().upper()
    if code in {"CDP_NOT_READY", "PC_AGENT_SESSION_NOT_FOUND", "BANK_BROWSER_SESSION_NOT_FOUND"}:
        return "reuse_work_key_then_recreate_same_profile_once"
    if code in {"PC_AGENT_UNAVAILABLE", "PC_AGENT_REQUIRED", "PC_AGENT_LOGIN_REQUIRED"}:
        return "connect_pc_agent_then_retry_same_work_key"
    return "retry_same_work_key_before_new_session"


_BANK_SESSION_RECOVERABLE_ERROR_CODES = {
    "BANK_BROWSER_SESSION_NOT_FOUND",
    "CDP_NOT_READY",
    "COMMAND_TIMEOUT",
    "PC_AGENT_SESSION_NOT_FOUND",
    "RUNTIME_EVALUATE_TIMEOUT",
    "STALE_TARGET",
}


class _ShinhanResumeSignal(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _bank_session_error_code(exc: Exception) -> str:
    code = str(getattr(exc, "error_code", "") or "").strip().upper()
    if code:
        return code
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = str(detail.get("error_code") or "").strip().upper()
        if code:
            return code
    return ""


def _is_bank_session_recoverable_error(exc: Exception) -> bool:
    code = _bank_session_error_code(exc)
    if code in _BANK_SESSION_RECOVERABLE_ERROR_CODES:
        return True
    message = str(exc or "").lower()
    return any(
        token in message
        for token in (
            "cdp_not_ready",
            "target closed",
            "session closed",
            "connection closed",
            "browser has been closed",
            "page closed",
        )
    )


async def _try_fill_bank_login(
    page: Any,
    *,
    username: str,
    password: str,
    account_no: str,
    account_password: str,
    business_registration_no: str,
) -> dict[str, str]:
    """Fill read-only bank quick-service login fields when saved secrets exist.

    Secrets are only passed into the connected browser DOM. Returned diagnostics
    contain booleans/counts only and never include plaintext field values.
    """
    if not any([username, password, account_no, account_password, business_registration_no]):
        return {"attempted": "0"}
    try:
        raw = await _evaluate_page(
            page,
            """
            (creds) => {
              const visible = (el) => !!(el && !el.disabled && el.offsetParent !== null);
              const setValue = (el, value) => {
                if (!visible(el) || !value) return false;
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.focus();
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
              };
              const first = (selectors) => {
                for (const selector of selectors) {
                  const el = Array.from(document.querySelectorAll(selector)).find(visible);
                  if (el) return el;
                }
                return null;
              };
              const filled = {};
              filled.username = setValue(first([
                "input[autocomplete='username']",
                "input[name*='user' i]",
                "input[name*='id' i]",
                "input[id*='user' i]",
                "input[id*='id' i]",
                "input[title*='아이디']",
                "input[placeholder*='아이디']"
              ]), creds.username);
              const passwords = Array.from(document.querySelectorAll("input[type='password']")).filter(visible);
              filled.password = setValue(first([
                "input[autocomplete='current-password']",
                "input[name*='password' i]",
                "input[id*='password' i]",
                "input[title*='비밀번호']",
                "input[placeholder*='비밀번호']"
              ]) || passwords[0], creds.password);
              filled.accountNo = setValue(first([
                "input[name*='account' i]",
                "input[id*='account' i]",
                "input[name*='계좌']",
                "input[id*='계좌']",
                "input[name*='계좌번호']",
                "input[id*='계좌번호']",
                "input[title*='계좌']",
                "input[placeholder*='계좌']"
              ]), creds.accountNo);
              filled.accountPassword = setValue(first([
                "input[name*='account' i][type='password']",
                "input[id*='account' i][type='password']",
                "input[name*='계좌'][type='password']",
                "input[id*='계좌'][type='password']",
                "input[name*='계좌비밀번호']",
                "input[id*='계좌비밀번호']",
                "input[title*='계좌비밀번호']",
                "input[placeholder*='계좌비밀번호']"
              ]) || passwords[1], creds.accountPassword);
              filled.businessNo = setValue(first([
                "input[name*='business' i]",
                "input[id*='business' i]",
                "input[name*='사업자']",
                "input[id*='사업자']",
                "input[name*='사업자번호']",
                "input[id*='사업자번호']",
                "input[title*='사업자']",
                "input[placeholder*='사업자']"
              ]), creds.businessRegistrationNo);
              const deny = ['이체', '송금', '삭제', '해지', '납부'];
              const submit = Array.from(document.querySelectorAll("button,input[type='button'],input[type='submit'],a"))
                .filter(visible)
                .map((el) => ({
                  el,
                  label: String(el.innerText || el.value || el.title || el.getAttribute('aria-label') || '').trim()
                }))
                .find((item) => {
                  if (!item.label) return false;
                  if (deny.some((word) => item.label.includes(word))) return false;
                  return ['로그인', '조회', '확인', '다음'].some((word) => item.label.includes(word));
                });
              if (submit && Object.values(filled).some(Boolean)) {
                submit.el.focus();
                submit.el.click();
                filled.submitted = true;
              } else {
                filled.submitted = false;
              }
              return filled;
            }
            """,
            {
                "username": username,
                "password": password,
                "accountNo": account_no,
                "accountPassword": account_password,
                "businessRegistrationNo": business_registration_no,
            },
            timeout_ms=10000,
        )
    except Exception:
        return {"attempted": "failed"}
    if not isinstance(raw, dict):
        return {"attempted": "failed"}
    return {
        "attempted": "1",
        "username": "1" if raw.get("username") else "0",
        "login_secret": "1" if raw.get("password") else "0",
        "account_no": "1" if raw.get("accountNo") else "0",
        "account_secret": "1" if raw.get("accountPassword") else "0",
        "business_registration_no": "1" if raw.get("businessNo") else "0",
        "submitted": "1" if raw.get("submitted") else "0",
    }


def _safe_error_detail(value: Any, *, max_items: int = 8) -> dict[str, str]:
    """Keep route diagnostics useful without exposing payloads or credentials."""
    if not isinstance(value, dict):
        return {}
    allowed_keys = {
        "status",
        "error_code",
        "message",
        "late_success_from_error_code",
        "default_agent_id",
        "default_hostname",
    }
    result: dict[str, str] = {}
    for key in allowed_keys:
        if key not in value:
            continue
        raw = str(value.get(key) or "").strip()
        if not raw:
            continue
        raw = re.sub(r"\b\d{2,}[-.\s]?\d{2,}[-.\s]?\d{2,}\b", "[redacted-number]", raw)
        raw = re.sub(r"\b\d{6,}\b", "[redacted-number]", raw)
        result[key] = raw[:240]
        if len(result) >= max_items:
            break
    return result


def _disable_local_agent_auto_recovery(page: Any) -> None:
    """Avoid long Browser Bridge recreate loops during bank collection diagnostics."""
    recovered = getattr(page, "_recovered_error_codes", None)
    if isinstance(recovered, set):
        recovered.update({"CDP_NOT_READY", "RUNTIME_EVALUATE_TIMEOUT", "STALE_TARGET", "COMMAND_TIMEOUT"})


# ── HTML table parser ────────────────────────────────────────────────────────

class _TableParser(HTMLParser):
    """Extract all <table> cell values from raw HTML (stdlib only).

    Nested-table safe: each <table> push saves/resets cell state so that
    an outer <td> wrapping an inner <table> does not inflate _cell_depth
    and block data collection inside the inner table.
    Every completed table (any depth) with at least one row is collected.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        # Stack of in-progress table rows, one frame per nesting level.
        self._table_stack: list[list[list[str]]] = []
        # Stack of in-progress current rows, parallel to _table_stack.
        self._row_stack: list[list[str]] = []
        # Cell-state stack: saved (cell_depth, cell_buf) on <table> entry.
        self._cell_state_stack: list[tuple[int, list[str]]] = []
        self._cell_depth: int = 0
        self._cell_buf: list[str] = []

    @property
    def _in_table(self) -> bool:
        return bool(self._table_stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            # Save outer cell state so a <td> wrapping this table is not corrupted.
            self._cell_state_stack.append((self._cell_depth, self._cell_buf))
            self._cell_depth = 0
            self._cell_buf = []
            self._table_stack.append([])
            self._row_stack.append([])
        elif tag == "tr" and self._in_table:
            self._row_stack[-1] = []
        elif tag in {"td", "th"} and self._in_table:
            self._cell_depth += 1
            if self._cell_depth == 1:
                self._cell_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._table_stack:
                finished = self._table_stack.pop()
                self._row_stack.pop()
                if finished:
                    self.tables.append(finished)
            # Restore outer cell state regardless of whether stack had a frame.
            if self._cell_state_stack:
                self._cell_depth, self._cell_buf = self._cell_state_stack.pop()
        elif tag == "tr" and self._in_table:
            row = self._row_stack[-1]
            if row:
                self._table_stack[-1].append(list(row))
            self._row_stack[-1] = []
        elif tag in {"td", "th"} and self._in_table:
            if self._cell_depth == 1:
                text = html.unescape(" ".join(self._cell_buf)).strip()
                self._row_stack[-1].append(text)
                self._cell_buf = []
            self._cell_depth = max(0, self._cell_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._cell_depth == 1:
            self._cell_buf.append(data)


def _extract_tables(raw_html: str) -> list[list[list[str]]]:
    parser = _TableParser()
    try:
        parser.feed(raw_html)
    except Exception:
        pass
    return parser.tables


def _clean_amount(text: str) -> int:
    cleaned = re.sub(r"[^0-9]", "", text)
    return int(cleaned) if cleaned else 0


def _clean_date(text: str) -> str:
    """Normalise date strings like 2026.08.01 / 20260801 → 2026-08-01."""
    match = re.search(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})", text)
    if match:
        y, m, d = match.group(1), match.group(2).zfill(2), match.group(3).zfill(2)
        return f"{y}-{m}-{d}"
    # Compact YYYYMMDD
    match2 = re.search(r"(\d{4})(\d{2})(\d{2})", text)
    if match2:
        return f"{match2.group(1)}-{match2.group(2)}-{match2.group(3)}"
    return text.strip()


_DATE_HEADERS = {"거래일자", "거래일", "날짜", "일자", "date"}
_MEMO_HEADERS = {"적요", "거래내용", "내용", "거래구분", "memo", "description"}
_COUNTERPARTY_HEADERS = {"보낸분/받는분", "거래처", "상대계좌명", "보낸분", "받는분", "상대방", "counterparty"}
_DEPOSIT_HEADERS = {"입금", "입금액", "입금금액", "credit", "크레딧"}
_WITHDRAWAL_HEADERS = {"출금", "출금액", "출금금액", "debit", "데빗"}
_BALANCE_HEADERS = {"잔액", "잔고", "거래후잔액", "balance"}
_TIME_HEADERS = {"거래시간", "시간", "time"}


def _normalize_header_cell(cell: str) -> str:
    normalized = str(cell or "").strip().lower()
    normalized = re.sub(r"(오름차순|내림차순)?\s*정렬", "", normalized)
    normalized = re.sub(r"\([^)]*\)", "", normalized)
    normalized = re.sub(r"[^0-9a-z가-힣/]+", "", normalized)
    return normalized


def _match_header(cell: str, synonym_set: set[str]) -> bool:
    normalized = _normalize_header_cell(cell)
    synonyms = {_normalize_header_cell(s) for s in synonym_set}
    if normalized in synonyms:
        return True
    return any(token and token in normalized for token in synonyms)


def _table_has_transaction_header(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    header = rows[0]
    has_date = any(_match_header(cell, _DATE_HEADERS) for cell in header)
    has_amount = any(
        _match_header(cell, _DEPOSIT_HEADERS) or _match_header(cell, _WITHDRAWAL_HEADERS)
        for cell in header
    )
    return has_date and has_amount


def _parse_table_with_header(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Parse a table whose first non-empty row is a header row."""
    if not rows or len(rows) < 2:
        return []
    header = rows[0]

    col_date = col_time = col_memo = col_counterparty = -1
    col_deposit = col_withdrawal = col_balance = -1

    for i, cell in enumerate(header):
        if col_date < 0 and _match_header(cell, _DATE_HEADERS):
            col_date = i
        elif col_time < 0 and _match_header(cell, _TIME_HEADERS):
            col_time = i
        elif col_memo < 0 and _match_header(cell, _MEMO_HEADERS):
            col_memo = i
        elif col_counterparty < 0 and _match_header(cell, _COUNTERPARTY_HEADERS):
            col_counterparty = i
        elif col_deposit < 0 and _match_header(cell, _DEPOSIT_HEADERS):
            col_deposit = i
        elif col_withdrawal < 0 and _match_header(cell, _WITHDRAWAL_HEADERS):
            col_withdrawal = i
        elif col_balance < 0 and _match_header(cell, _BALANCE_HEADERS):
            col_balance = i

    if col_date < 0:
        return []

    result: list[dict[str, Any]] = []
    for row in rows[1:]:
        def _cell(idx: int, _row: list[str] = row) -> str:
            if idx < 0 or idx >= len(_row):
                return ""
            return str(_row[idx]).strip()

        date_raw = _cell(col_date)
        if not date_raw:
            continue

        date_str = _clean_date(date_raw)
        time_str = _cell(col_time)
        occurred_at = f"{date_str} {time_str}".strip() if time_str else date_str

        deposit_raw = _cell(col_deposit)
        withdrawal_raw = _cell(col_withdrawal)
        deposit_amt = _clean_amount(deposit_raw)
        withdrawal_amt = _clean_amount(withdrawal_raw)

        if not deposit_amt and not withdrawal_amt:
            continue

        direction = "in" if deposit_amt else "out"
        amount = deposit_amt if deposit_amt else withdrawal_amt
        balance_raw = _cell(col_balance)
        balance = _clean_amount(balance_raw) if balance_raw else None
        memo = _cell(col_memo)
        counterparty = _cell(col_counterparty)

        entry: dict[str, Any] = {
            "occurred_at": occurred_at,
            "direction": direction,
            "amount": amount,
        }
        if balance:
            entry["balance"] = balance
        if memo:
            entry["memo"] = memo
            entry["raw_memo"] = memo
        if counterparty:
            entry["counterparty"] = counterparty
        result.append(entry)

    return result


def _parse_tables_with_diagnostics(tables: list[list[list[str]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    safe_tables = [
        [
            [str(cell or "").strip() for cell in row]
            for row in table
            if isinstance(row, list)
        ]
        for table in tables
        if isinstance(table, list)
    ]
    safe_tables = [[row for row in table if any(str(cell or "").strip() for cell in row)] for table in safe_tables]
    safe_tables = [table for table in safe_tables if table]
    diag: dict[str, Any] = {
        "table_count": len(safe_tables),
        "headers_found": [t[0] if t else [] for t in safe_tables[:3]],
        "parse_failure": False,
        "transaction_header_found": False,
    }
    for table in safe_tables:
        if _table_has_transaction_header(table):
            diag["transaction_header_found"] = True
        rows = _parse_table_with_header(table)
        if rows:
            return rows, diag
    if safe_tables and not diag["transaction_header_found"]:
        diag["parse_failure"] = True
    return [], diag


def parse_bank_portal_html(raw_html: str) -> list[dict[str, Any]]:
    """Extract transaction rows from a bank portal HTML snippet.

    Compatible with Shinhan 간편서비스 and IBK 빠른서비스 table layouts.
    Returns an empty list when no recognisable transaction table is found.
    Raw HTML is not stored anywhere by this function.
    """
    rows, _ = parse_bank_portal_html_with_diagnostics(raw_html)
    return rows


def parse_bank_portal_html_with_diagnostics(
    raw_html: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Like parse_bank_portal_html but also returns a diagnostics dict.

    Diagnostics (safe — no personal data):
        table_count     how many <table> elements were found
        headers_found   list of header rows (up to 3 tables) for debugging
        parse_failure   True when tables exist but none had a date column
    """
    return _parse_tables_with_diagnostics(_extract_tables(raw_html))


def _decode_download_content(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-16", "utf-16-le"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _download_csv_delimiter(text: str) -> str:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    if first_line.count("\t") > first_line.count(","):
        return "\t"
    if first_line.count(";") > first_line.count(","):
        return ";"
    return ","


def _first_csv_value(row: dict[str, str], *keys: str) -> str:
    normalized = {_normalize_header_cell(key): value for key, value in row.items()}
    for key in keys:
        value = normalized.get(_normalize_header_cell(key))
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _parse_bank_download_delimited(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(text.splitlines(), delimiter=_download_csv_delimiter(text))
    if not reader.fieldnames:
        return []
    rows: list[dict[str, Any]] = []
    for source_row in reader:
        raw = {str(key or "").strip(): str(value or "").strip() for key, value in source_row.items()}
        if not any(raw.values()):
            continue
        combined_at = _first_csv_value(raw, "거래일시", "일시")
        date_part = _first_csv_value(raw, "거래일자", "거래일", "일자", "날짜", "date")
        time_part = _first_csv_value(raw, "거래시간", "시간", "time")
        occurred_at = _clean_date(combined_at or date_part)
        if time_part and occurred_at and re.match(r"^\d{4}-\d{2}-\d{2}$", occurred_at):
            occurred_at = f"{occurred_at} {time_part}"
        incoming = _clean_amount(_first_csv_value(raw, "입금액", "입금", "입금금액", "맡기신금액", "받으신금액", "deposit", "credit"))
        outgoing = _clean_amount(_first_csv_value(raw, "출금액", "출금", "출금금액", "찾으신금액", "지급금액", "withdrawal", "debit"))
        signed_amount = str(_first_csv_value(raw, "거래금액", "금액", "amount")).strip()
        if not incoming and not outgoing and signed_amount:
            amount_value = int(_clean_amount(signed_amount))
            is_out = signed_amount.replace(",", "").strip().startswith("-") or (
                _first_csv_value(raw, "입출금", "구분", "거래구분", "direction").lower() in {"출금", "지급", "out", "debit"}
            )
            incoming = 0 if is_out else amount_value
            outgoing = amount_value if is_out else 0
        if not occurred_at or (not incoming and not outgoing):
            continue
        memo = _first_csv_value(raw, "적요", "거래내용", "내용", "기재내용", "메모", "memo", "description")
        counterparty = _first_csv_value(raw, "보낸분/받는분", "보낸분", "받는분", "거래처", "상대계좌예금주", "counterparty")
        balance_raw = _first_csv_value(raw, "잔액", "잔고", "거래후잔액", "balance")
        row: dict[str, Any] = {
            "occurred_at": occurred_at,
            "direction": "in" if incoming else "out",
            "amount": incoming or outgoing,
            "source": "bank-browser-download",
        }
        if memo:
            row["memo"] = memo
            row["raw_memo"] = memo
        if counterparty:
            row["counterparty"] = counterparty
        if balance_raw:
            row["balance"] = _clean_amount(balance_raw)
        rows.append(row)
    return rows


def parse_bank_download_content(content: bytes | str, filename: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse a downloaded bank statement without persisting raw file content."""
    raw_text = _decode_download_content(content) if isinstance(content, bytes) else str(content or "")
    text = raw_text.lstrip("\ufeff").strip()
    diagnostics: dict[str, Any] = {
        "download_filename": Path(str(filename or "bank-statement")).name[:120],
        "download_bytes": len(raw_text.encode("utf-8", errors="ignore")),
        "download_parser": "unknown",
        "download_parse_failure": False,
    }
    if not text:
        diagnostics["download_parse_failure"] = True
        return [], diagnostics
    lower_start = text[:300].lower()
    if "<table" in lower_start or "<html" in lower_start:
        rows, table_diag = parse_bank_portal_html_with_diagnostics(text)
        diagnostics.update(
            {
                "download_parser": "html_table",
                "download_table_count": table_diag.get("table_count", 0),
                "download_transaction_header_found": table_diag.get("transaction_header_found", False),
                "download_parse_failure": table_diag.get("parse_failure", False) and not rows,
            }
        )
        for row in rows:
            row.setdefault("source", "bank-browser-download")
        return rows, diagnostics
    rows = _parse_bank_download_delimited(text)
    diagnostics["download_parser"] = "delimited"
    diagnostics["download_parse_failure"] = not bool(rows)
    return rows, diagnostics


def _download_result_content(result: Any) -> tuple[bytes | None, str]:
    if result is None:
        return None, ""
    if isinstance(result, bytes):
        return result, ""
    if not isinstance(result, dict):
        return None, ""
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    filename = str(data.get("filename") or data.get("name") or data.get("path") or "bank-statement").strip()
    text_value = data.get("text") or data.get("content") or data.get("csv_text")
    if isinstance(text_value, str) and text_value:
        return text_value.encode("utf-8-sig"), filename
    encoded = data.get("content_base64") or data.get("file_base64") or data.get("base64")
    if isinstance(encoded, str) and encoded.strip():
        try:
            return base64.b64decode(encoded.encode("ascii")), filename
        except Exception:
            return None, filename
    path_value = str(data.get("path") or data.get("file_path") or "").strip()
    if path_value and os.path.isfile(path_value):
        try:
            return Path(path_value).read_bytes(), filename or Path(path_value).name
        except Exception:
            return None, filename
    return None, filename


async def _install_synthetic_statement_download(page: Any) -> str:
    try:
        selector = await _evaluate_page(
            page,
            """
            () => {
              const visible = (el) => !!(el && el.offsetParent !== null);
              const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const csvCell = (value) => {
                const text = clean(value).replace(/"/g, '""');
                return /[",\\n\\r]/.test(text) ? `"${text}"` : text;
              };
              const tables = Array.from(document.querySelectorAll('table')).filter(visible);
              const target = tables.find((table) => {
                const text = clean(table.innerText);
                return /거래일자|거래일|날짜/.test(text) && /입금|출금|잔액|거래금액/.test(text);
              });
              if (!target) return '';
              const rows = Array.from(target.rows || [])
                .map((row) => Array.from(row.cells || []).map((cell) => csvCell(cell.innerText || cell.textContent || '')))
                .filter((row) => row.some(Boolean));
              if (rows.length < 2) return '';
              const csv = rows.map((row) => row.join(',')).join('\\n');
              const old = document.querySelector('#aads-bank-statement-download');
              if (old) old.remove();
              const a = document.createElement('a');
              a.id = 'aads-bank-statement-download';
              a.download = `shinhan-bank-statement-${Date.now()}.csv`;
              a.href = URL.createObjectURL(new Blob(['\\ufeff' + csv], {type: 'text/csv;charset=utf-8'}));
              a.style.position = 'fixed';
              a.style.left = '-10000px';
              a.textContent = 'AADS 거래내역 다운로드';
              document.body.appendChild(a);
              return '#aads-bank-statement-download';
            }
            """,
            timeout_ms=10000,
        )
        return str(selector or "")
    except Exception:
        return ""


async def _bank_download_selectors(page: Any) -> list[str]:
    try:
        raw = await _evaluate_page(
            page,
            """
            () => {
              const visible = (el) => !!(el && !el.disabled && el.offsetParent !== null);
              const textOf = (el) => String(
                el?.innerText || el?.value || el?.title || el?.ariaLabel || el?.getAttribute?.('aria-label') || ''
              ).replace(/\\s+/g, ' ').trim();
              const candidates = Array.from(document.querySelectorAll('a,button,input[type=button],input[type=submit]'))
                .filter(visible)
                .map((el) => ({el, label: textOf(el), id: String(el.id || ''), cls: String(el.className || '')}))
                .filter((item) => /엑셀|excel|csv|다운로드|저장|내려받기|xls/i.test(`${item.label} ${item.id} ${item.cls}`))
                .filter((item) => !/이체|송금|납부|삭제|해지/i.test(item.label));
              return candidates.slice(0, 5).map((item, index) => {
                const attr = `aads-bank-download-${index}`;
                item.el.setAttribute('data-aads-bank-download', attr);
                return `[data-aads-bank-download="${attr}"]`;
              });
            }
            """,
            timeout_ms=10000,
        )
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item or "").strip()]
    except Exception:
        pass
    return []


async def _try_download_bank_statement(page: Any, date_from: str, date_to: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics: dict[str, Any] = {"download_attempted": "1", "download_status": "not_found"}
    selectors = await _bank_download_selectors(page)
    synthetic_selector = await _install_synthetic_statement_download(page)
    if synthetic_selector:
        selectors.append(synthetic_selector)
        diagnostics["download_synthetic_available"] = "1"
    download_method = getattr(page, "download", None)
    for selector in selectors:
        try:
            if callable(download_method):
                result = await download_method(selector, timeout_seconds=45)
            else:
                expect_download = getattr(page, "expect_download", None)
                click = getattr(page, "click", None)
                if not callable(expect_download) or not callable(click):
                    continue
                async with expect_download(timeout=45000) as download_info:
                    await click(selector)
                download = await download_info.value
                file_path = await download.path()
                result = {
                    "path": file_path,
                    "filename": getattr(download, "suggested_filename", "") or Path(str(file_path)).name,
                }
            content, filename = _download_result_content(result)
            diagnostics["download_status"] = "clicked"
            diagnostics["download_selector_used"] = selector[:120]
            if filename:
                diagnostics["download_filename"] = Path(filename).name[:120]
            if content is None:
                diagnostics["download_content_available"] = "0"
                continue
            rows, parse_diag = parse_bank_download_content(content, filename)
            diagnostics.update(parse_diag)
            diagnostics["download_content_available"] = "1"
            if date_from or date_to:
                rows = [row for row in rows if _row_in_date_range(row, date_from, date_to)]
            diagnostics["download_row_count"] = len(rows)
            if rows:
                diagnostics["download_status"] = "parsed"
                return rows, diagnostics
        except Exception as exc:
            diagnostics["download_status"] = "failed"
            diagnostics.update(_safe_browser_error_fields(exc))
    return [], diagnostics


async def _read_bank_portal_snapshot(page: Any) -> tuple[str, list[dict[str, Any]], dict[str, Any], str]:
    """Read a lightweight, redacted portal snapshot.

    Full WebSquare/enterprise-bank HTML can be huge and may timeout through
    PC Agent. Prefer bounded table/text extraction and only fall back to
    innerHTML for simple test pages or lightweight portals.
    """
    current_url = ""
    try:
        current_url = str(await _evaluate_page(page, "window.location.href", timeout_ms=8000) or "")
    except Exception:
        current_url = ""

    tables: list[list[list[str]]] = []
    try:
        raw_tables = await _evaluate_page(
            page,
            """
            () => Array.from(document.querySelectorAll('table'))
              .slice(0, 20)
              .map((table) => Array.from(table.rows || [])
                .slice(0, 200)
                .map((row) => Array.from(row.cells || [])
                  .slice(0, 24)
                  .map((cell) => String(cell.innerText || cell.textContent || '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .slice(0, 200))))
            """,
            timeout_ms=12000,
        )
        if isinstance(raw_tables, list):
            tables = raw_tables
    except Exception:
        tables = []

    rows, parse_diag = _parse_tables_with_diagnostics(tables)
    state_text = ""
    try:
        raw_text = await _evaluate_page(
            page,
            "document.body ? String(document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 4000) : ''",
            timeout_ms=8000,
        )
        if isinstance(raw_text, str):
            state_text = _safe_portal_text(raw_text)
    except Exception:
        state_text = ""

    if not tables and not state_text:
        try:
            html_content = str(await _evaluate_page(
                page,
                "document.body ? document.body.innerHTML : ''",
                timeout_ms=12000,
            ) or "")
            rows, parse_diag = parse_bank_portal_html_with_diagnostics(html_content)
            state_text = _safe_portal_text(html_content)
        except Exception:
            pass
    return current_url, rows, parse_diag, state_text


# ── Async browser collector ──────────────────────────────────────────────────

BANK_PORTAL_URLS: dict[str, str] = {
    "shinhan_business": "https://bank.shinhan.com/rib/easy/index.jsp",
    "ibk_business": "https://mybank.ibk.co.kr/uib/jsp/guest/qcs/qcs10/qcs1020/PQCS102000_i.jsp",
    "088": "https://bank.shinhan.com/rib/easy/index.jsp",
    "003": "https://mybank.ibk.co.kr/uib/jsp/guest/qcs/qcs10/qcs1020/PQCS102000_i.jsp",
}


def _infer_bank_service_code(bank_code: str, bank_name: str, institution_code: str) -> str:
    for value in (institution_code, bank_code):
        normalized = str(value or "").strip().lower()
        if normalized in BANK_PORTAL_URLS:
            return normalized
    normalized_name = str(bank_name or "").strip().lower()
    if "신한" in normalized_name:
        return "shinhan_business"
    if "ibk" in normalized_name or "기업" in normalized_name:
        return "ibk_business"
    return ""


async def collect_bank_via_browser_session_async(
    account: dict[str, Any],
    *,
    browser_session_id: str,
    browser_work_key: str,
    date_from: str,
    date_to: str,
    portal_url: str = "",
    auto_open_browser: bool = False,
    browser_agent_id: str = "",
    browser_preferred_port: int | None = None,
    force_recreate_browser: bool = False,
    login_username: str = "",
    login_password: str = "",
    account_no: str = "",
    account_password: str = "",
    business_registration_no: str = "",
    business_entity_type: str = "",
    browser_timeout_seconds: float = 120,
    _recovery_attempted: bool = False,
) -> dict[str, Any]:
    """Fetch transaction rows from a bank quick-service portal via PC Agent.

    Never initiates a headless credential login.  When no live browser
    session is available, returns status="action_required" unless
    auto_open_browser=True, in which case it opens the bank corporate page in
    a dedicated PC Agent Browser Bridge work session and waits for operator
    action / existing browser-auth state.

    Return schema:
        status      "collected" | "action_required" | "connector_not_ready" | "failed"
        rows        list of normalised transaction dicts (empty on non-collected)
        row_count   int
        diagnostics dict with safe audit fields only (no credentials)
        message     Korean status message
        error_code  optional machine-readable code
    """
    bank_code = str(account.get("bank_code") or "").strip().lower()
    bank_name = str(account.get("bank_name") or "").strip()
    account_id = str(account.get("id") or "").strip()
    institution_code = str(account.get("institution_code") or "").strip()

    inferred_service_code = _infer_bank_service_code(bank_code, bank_name, institution_code)
    if not portal_url:
        portal_url = (
            BANK_PORTAL_URLS.get(institution_code)
            or BANK_PORTAL_URLS.get(bank_code)
            or BANK_PORTAL_URLS.get(inferred_service_code)
            or ""
        )

    safe_diagnostics: dict[str, str] = {
        "auth_mode": "pc_agent_browser",
        "connector": "bank_browser",
        "bank_code": bank_code,
        "institution_code": institution_code or inferred_service_code,
        "bank_account_id": account_id,
        "browser_work_key": browser_work_key,
        "saved_login_username": "1" if str(login_username or "").strip() else "0",
        "saved_login_secret": "1" if str(login_password or "").strip() else "0",
        "saved_account_no": "1" if str(account_no or "").strip() else "0",
        "saved_account_secret": "1" if str(account_password or "").strip() else "0",
        "saved_business_registration_no": "1" if str(business_registration_no or "").strip() else "0",
        "bank_exclusive_lock_acquired": "1" if os.getenv("YEOLJEONG_BANK_LOCK_HELD") == "1" else "0",
    }
    shinhan_service = _is_shinhan_service(bank_code, bank_name, institution_code, portal_url)
    ibk_service = _is_ibk_service(bank_code, bank_name, institution_code, portal_url)
    shinhan_flow_mode = _shinhan_query_flow_mode(business_entity_type, account) if shinhan_service else ""
    if shinhan_flow_mode == "individual_simple":
        portal_url = BANK_PORTAL_URLS["shinhan_business"]
        safe_diagnostics["portal_url_policy"] = "shinhan_individual_simple_override"
    if shinhan_flow_mode:
        safe_diagnostics["shinhan_query_flow_mode"] = shinhan_flow_mode
    if ibk_service:
        safe_diagnostics["ibk_query_flow_mode"] = "ibk_quick"
    shinhan_stage_logs: list[dict[str, str]] = []
    if shinhan_service:
        safe_diagnostics["shinhan_stage_logs"] = shinhan_stage_logs
        safe_diagnostics["shinhan_stage_log_schema"] = SITE_STAGE_LOG_SCHEMA

    session_id_to_use = browser_session_id.strip() if browser_session_id else ""
    auto_opened_session = False
    try:
        launch_timeout_seconds = max(15.0, min(90.0, float(browser_timeout_seconds) / 2.0))
    except (TypeError, ValueError):
        launch_timeout_seconds = 60.0
    launch_queue_wait_seconds = max(5.0, min(20.0, launch_timeout_seconds / 3.0))

    if not session_id_to_use and browser_work_key:
        try:
            from app.browser_bridge.service import get_browser_bridge_service

            bridge = get_browser_bridge_service()
            existing = bridge.sessions.find_by_work_key(browser_work_key)
            existing_agent_id = _browser_session_agent_id(existing) if existing else ""
            if (
                existing
                and bridge._session_reusable(existing)
                and (not browser_agent_id or not existing_agent_id or existing_agent_id == browser_agent_id)
            ):
                session_id_to_use = existing.session_id
            elif existing and browser_agent_id and existing_agent_id and existing_agent_id != browser_agent_id:
                safe_diagnostics["browser_session_reuse_skipped"] = "agent_mismatch"
                safe_diagnostics["browser_session_existing_agent_id"] = existing_agent_id
                safe_diagnostics["browser_session_required_agent_id"] = browser_agent_id
        except Exception:
            pass

    if not session_id_to_use and auto_open_browser and browser_work_key:
        stage_started_at = time.monotonic()
        try:
            from app.browser_bridge.service import get_browser_bridge_service

            bridge = get_browser_bridge_service()
            session_label = (
                f"{bank_name or '신한은행'} 간편조회"
                if shinhan_flow_mode == "individual_simple"
                else f"{bank_name or '은행'} 기업페이지"
            )
            session = await bridge.ensure_work_session(
                work_key=browser_work_key,
                label=session_label,
                agent_id=str(browser_agent_id or ""),
                url=portal_url or "about:blank",
                preferred_port=browser_preferred_port,
                force_recreate=bool(force_recreate_browser),
                queue_wait_timeout_seconds=launch_queue_wait_seconds,
                command_timeout_seconds=launch_timeout_seconds,
            )
            session_id_to_use = str(getattr(session, "session_id", "") or "")
            auto_opened_session = bool(session_id_to_use)
            safe_diagnostics["auto_open_browser"] = "1"
            safe_diagnostics["cdp_preflight_session_id"] = session_id_to_use
            safe_diagnostics["cdp_preflight"] = "ready"
            if shinhan_service:
                _append_shinhan_stage_log(
                    shinhan_stage_logs,
                    stage="shinhan_browser_session",
                    status="success",
                    started_at=stage_started_at,
                    reason="auto_open_browser_ready",
                )
        except Exception as exc:
            safe_diagnostics["auto_open_browser"] = "failed"
            exc_error_code = str(getattr(exc, "error_code", "") or "").strip()
            safe_diagnostics["auto_open_error"] = exc_error_code or "PC_AGENT_UNAVAILABLE"
            safe_diagnostics["auto_open_error_message"] = str(exc)[:240]
            safe_diagnostics["session_recovery_plan"] = _bank_session_recovery_plan(
                exc_error_code or "PC_AGENT_UNAVAILABLE"
            )
            exc_detail = _safe_error_detail(getattr(exc, "detail", None))
            if exc_detail:
                safe_diagnostics["auto_open_error_detail"] = exc_detail
            if (
                not force_recreate_browser
                and (exc_error_code or "").upper() in {"CDP_NOT_READY", "COMMAND_TIMEOUT"}
            ):
                try:
                    recreate_started_at = time.monotonic()
                    session = await bridge.ensure_work_session(
                        work_key=browser_work_key,
                        label=session_label,
                        agent_id=str(browser_agent_id or ""),
                        url=portal_url or "about:blank",
                        preferred_port=browser_preferred_port,
                        force_recreate=True,
                        queue_wait_timeout_seconds=launch_queue_wait_seconds,
                        command_timeout_seconds=launch_timeout_seconds,
                    )
                    session_id_to_use = str(getattr(session, "session_id", "") or "")
                    auto_opened_session = bool(session_id_to_use)
                    if session_id_to_use:
                        safe_diagnostics["browser_session_id"] = session_id_to_use
                        safe_diagnostics["auto_open_browser"] = "1"
                        safe_diagnostics["session_recovery"] = "recreated_same_work_key"
                        safe_diagnostics["session_recovery_error"] = exc_error_code
                        if shinhan_service:
                            _append_shinhan_stage_log(
                                shinhan_stage_logs,
                                stage="shinhan_browser_session",
                                status="success",
                                started_at=recreate_started_at,
                                reason="recreated_same_work_key",
                            )
                except Exception as retry_exc:
                    retry_error_code = str(getattr(retry_exc, "error_code", "") or "").strip()
                    safe_diagnostics["session_recovery"] = "failed"
                    safe_diagnostics["session_recovery_error"] = retry_error_code or "PC_AGENT_UNAVAILABLE"
                    retry_detail = _safe_error_detail(getattr(retry_exc, "detail", None))
                    if retry_detail:
                        safe_diagnostics["session_recovery_error_detail"] = retry_detail
            if shinhan_service and not session_id_to_use:
                _append_shinhan_stage_log(
                    shinhan_stage_logs,
                    stage="shinhan_browser_session",
                    status="failed",
                    started_at=stage_started_at,
                    error_code=exc_error_code or "PC_AGENT_UNAVAILABLE",
                    reason="auto_open_browser_failed",
                )

    if not session_id_to_use:
        if shinhan_service:
            _append_shinhan_stage_log(
                shinhan_stage_logs,
                stage="shinhan_browser_session",
                status="failed",
                started_at=time.monotonic(),
                error_code="PC_AGENT_LOGIN_REQUIRED",
                reason="session_id_missing",
            )
        return {
            "status": "action_required",
            "error_code": "PC_AGENT_LOGIN_REQUIRED",
            "rows": [],
            "row_count": 0,
            "diagnostics": safe_diagnostics,
            "message": (
                f"{bank_name or '은행'} 브라우저 수집을 위해 PC Agent 세션이 필요합니다. "
                "PC Agent 연결 후 같은 은행 전용 세션으로 재시도하고, 그래도 실패하면 "
                "CSV 업로드로 대체 수집하십시오."
            ),
        }

    safe_diagnostics["browser_session_id"] = session_id_to_use
    safe_diagnostics.setdefault("cdp_preflight_session_id", session_id_to_use)
    safe_diagnostics.setdefault("cdp_preflight", "ready")
    if auto_opened_session:
        safe_diagnostics["auto_opened_session"] = "1"
    if shinhan_service and not any(item.get("stage") == "shinhan_browser_session" for item in shinhan_stage_logs):
        _append_shinhan_stage_log(
            shinhan_stage_logs,
            stage="shinhan_browser_session",
            status="success",
            started_at=time.monotonic(),
            reason="session_ready",
        )

    try:
        from app.browser_bridge.service import get_browser_bridge_service

        bridge = get_browser_bridge_service()
        session = bridge.sessions.get(session_id_to_use)
        if not session:
            if auto_open_browser and browser_work_key:
                try:
                    session_label = (
                        f"{bank_name or '신한은행'} 간편조회"
                        if shinhan_flow_mode == "individual_simple"
                        else f"{bank_name or '은행'} 기업페이지"
                    )
                    session = await bridge.ensure_work_session(
                        work_key=browser_work_key,
                        label=session_label,
                        agent_id=str(browser_agent_id or ""),
                        url=portal_url or "about:blank",
                        preferred_port=browser_preferred_port,
                        force_recreate=True,
                        queue_wait_timeout_seconds=launch_queue_wait_seconds,
                        command_timeout_seconds=launch_timeout_seconds,
                    )
                    session_id_to_use = str(getattr(session, "session_id", "") or "")
                    if session_id_to_use:
                        safe_diagnostics["browser_session_id"] = session_id_to_use
                        safe_diagnostics["session_recovery"] = "recreated_from_missing_session"
                        safe_diagnostics["session_recovery_plan"] = _bank_session_recovery_plan(
                            "BANK_BROWSER_SESSION_NOT_FOUND"
                        )
                except Exception as exc:
                    safe_diagnostics["session_recovery"] = "failed"
                    exc_error_code = str(getattr(exc, "error_code", "") or "").strip()
                    safe_diagnostics["session_recovery_error"] = exc_error_code or "PC_AGENT_UNAVAILABLE"
                    safe_diagnostics["session_recovery_plan"] = _bank_session_recovery_plan(
                        exc_error_code or "PC_AGENT_UNAVAILABLE"
                    )
                    exc_detail = _safe_error_detail(getattr(exc, "detail", None))
                    if exc_detail:
                        safe_diagnostics["session_recovery_error_detail"] = exc_detail
            if session:
                safe_diagnostics["browser_session_id"] = session_id_to_use
            else:
                safe_diagnostics["session_recovery_plan"] = _bank_session_recovery_plan(
                    "BANK_BROWSER_SESSION_NOT_FOUND"
                )
                return {
                    "status": "connector_not_ready",
                    "error_code": "BANK_BROWSER_SESSION_NOT_FOUND",
                    "rows": [],
                    "row_count": 0,
                    "diagnostics": safe_diagnostics,
                    "message": "등록된 브라우저 세션을 찾지 못했습니다. 같은 은행 전용 세션 재확보도 실패했습니다.",
                }
        context = await bridge._context_for_session(session)
        pages = getattr(context, "pages", None)
        page, initial_url, matched_existing_page = await _select_bank_page(pages, portal_url)
        if page is None:
            page = await context.new_page()
            initial_url = ""
            matched_existing_page = False
        if initial_url:
            safe_diagnostics["initial_url"] = initial_url
        if matched_existing_page:
            safe_diagnostics["browser_tab_reused"] = "1"
        safe_diagnostics["browser_session_reuse_policy"] = "work_key_domain_first"
        _disable_local_agent_auto_recovery(page)

        if portal_url:
            portal_stage_started_at = time.monotonic()
            if _portal_url_reusable(initial_url, portal_url):
                safe_diagnostics["portal_navigation"] = "skipped_reusable_tab"
                if shinhan_service:
                    _append_shinhan_stage_log(
                        shinhan_stage_logs,
                        stage="shinhan_site_access",
                        status="success",
                        started_at=portal_stage_started_at,
                        reason="reused_bank_tab",
                    )
            else:
                try:
                    await page.goto(portal_url, wait_until="domcontentloaded", timeout=30000)
                    safe_diagnostics["portal_navigation"] = "navigated"
                    if shinhan_service:
                        _append_shinhan_stage_log(
                            shinhan_stage_logs,
                            stage="shinhan_site_access",
                            status="success",
                            started_at=portal_stage_started_at,
                            reason="goto_domcontentloaded",
                        )
                except Exception:
                    safe_diagnostics["portal_navigation"] = "failed"
                    if shinhan_service:
                        _append_shinhan_stage_log(
                            shinhan_stage_logs,
                            stage="shinhan_site_access",
                            status="failed",
                            started_at=portal_stage_started_at,
                            error_code="PORTAL_NAVIGATION_FAILED",
                            reason="page_goto_failed",
                        )
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass

        current_url = ""
        rows: list[dict[str, Any]] = []
        parse_diag: dict[str, Any] = {
            "table_count": 0,
            "headers_found": [],
            "parse_failure": False,
            "transaction_header_found": False,
        }
        state_text = ""
        skip_initial_snapshot = shinhan_flow_mode == "individual_simple" and all(
            str(value or "").strip()
            for value in (login_username, login_password, account_no, account_password)
        )
        if skip_initial_snapshot:
            current_url = await _visible_page_url(page)
            safe_diagnostics["initial_snapshot"] = "skipped_before_shinhan_state_machine"
        else:
            try:
                current_url, rows, parse_diag, state_text = await _read_bank_portal_snapshot(page)
            except Exception as exc:
                if (
                    auto_open_browser
                    and browser_work_key
                    and not _recovery_attempted
                    and _is_bank_session_recoverable_error(exc)
                ):
                    recovery_code = _bank_session_error_code(exc) or "BANK_BROWSER_PAGE_ERROR"
                    recovered = await collect_bank_via_browser_session_async(
                        account,
                        browser_session_id="",
                        browser_work_key=browser_work_key,
                        date_from=date_from,
                        date_to=date_to,
                        portal_url=portal_url,
                        auto_open_browser=True,
                        browser_agent_id=browser_agent_id,
                        browser_preferred_port=browser_preferred_port,
                        force_recreate_browser=True,
                        login_username=login_username,
                        login_password=login_password,
                        account_no=account_no,
                        account_password=account_password,
                        business_registration_no=business_registration_no,
                        business_entity_type=business_entity_type,
                        browser_timeout_seconds=browser_timeout_seconds,
                        _recovery_attempted=True,
                    )
                    recovered_diag = recovered.setdefault("diagnostics", {})
                    if isinstance(recovered_diag, dict):
                        recovered_diag.setdefault("previous_browser_session_id", session_id_to_use)
                        recovered_diag["session_recovery"] = "recreated_after_page_error"
                        recovered_diag["session_recovery_error"] = recovery_code
                        recovered_diag["session_recovery_plan"] = _bank_session_recovery_plan(recovery_code)
                    return recovered
                return {
                    "status": "failed",
                    "error_code": "BANK_BROWSER_PAGE_ERROR",
                    "rows": [],
                    "row_count": 0,
                    "diagnostics": {**safe_diagnostics, "current_url": current_url},
                    "message": f"브라우저 페이지에서 내용을 가져오지 못했습니다: {str(exc)[:200]}",
                }

        safe_diagnostics["current_url"] = current_url
        safe_diagnostics["last_observed_stage"] = "login page"
        if shinhan_service:
            simple_query_status = (
                "success"
                if "bank.shinhan.com" in current_url and "/rib/easy/index.jsp" in current_url
                else "unknown"
            )
            _append_shinhan_stage_log(
                shinhan_stage_logs,
                stage="shinhan_simple_query_page",
                status=simple_query_status,
                started_at=time.monotonic(),
                reason="visible_url_checked",
                current_url=current_url[:120],
            )

        # YESKEY may be visible only as a cross-origin iframe or popup. If
        # saved Shinhan ID/PW exists, reset to that login path before falling
        # back to an operator challenge.
        auth_challenge = await _detect_shinhan_auth_challenge(page, pages)
        retry_shinhan_idpw_login = False
        if auth_challenge:
            if shinhan_flow_mode == "individual_simple" and login_username and login_password:
                safe_diagnostics["shinhan_auth_challenge_detected_before_idpw"] = "1"
                safe_diagnostics["shinhan_auth_challenge_reason_code"] = str(
                    auth_challenge.get("screen_reason_code") or ""
                )[:120]
                safe_diagnostics["shinhan_auth_challenge_policy"] = "prefer_saved_idpw_login"
                reset_result = await _prefer_shinhan_idpw_login_after_auth_challenge(page, portal_url)
                safe_diagnostics["shinhan_idpw_login_reset"] = reset_result
                retry_shinhan_idpw_login = True
            else:
                safe_diagnostics.update(auth_challenge)
                return {
                    "status": "action_required",
                    "error_code": "BANK_BROWSER_AUTH_CHALLENGE_DETECTED",
                    "rows": [],
                    "row_count": 0,
                    "diagnostics": safe_diagnostics,
                    "message": "신한 금융인증/권한 확인이 필요합니다. ID/PW 저장값이 없으면 인증 완료 후 같은 work key로 재시도하십시오.",
                }

        if rows and (date_from or date_to):
            rows = [r for r in rows if _row_in_date_range(r, date_from, date_to)]

        safe_diagnostics["parser_table_count"] = parse_diag["table_count"]
        safe_diagnostics["parser_failure"] = parse_diag["parse_failure"]
        safe_diagnostics["parser_transaction_header_found"] = parse_diag.get("transaction_header_found", False)
        if parse_diag.get("headers_found"):
            safe_diagnostics["parser_headers_found"] = [
                [str(cell)[:40] for cell in header[:8]]
                for header in parse_diag.get("headers_found", [])[:3]
                if isinstance(header, list)
            ]

        screen_decision = classify_portal_state(current_url, state_text)
        if rows:
            screen_decision = classify_portal_state(
                current_url,
                "거래일자 입금금액 출금금액",
            )
        elif parse_diag["parse_failure"]:
            screen_decision = screen_decision if screen_decision.requires_operator else classify_portal_state(
                current_url,
                "parse_failed",
            )
        screen_state = screen_decision.as_dict()
        safe_diagnostics["screen_state"] = screen_state.get("state", "unknown")
        safe_diagnostics["screen_reason_code"] = screen_state.get("reason_code", "")
        safe_diagnostics["screen_suggested_action"] = screen_state.get("suggested_action", "no_action")
        safe_diagnostics["screen_requires_operator"] = "1" if screen_state.get("requires_operator") else "0"
        auth_states = {
            "captcha_required",
            "otp_required",
            "identity_check_required",
            "certificate_password_required",
            "login_required",
            "portal_error",
        }
        selector_candidates = await _safe_selector_candidates(page)
        if selector_candidates:
            safe_diagnostics["selector_candidates"] = selector_candidates
            if (
                not rows
                and safe_diagnostics.get("screen_state") not in auth_states
                and _selector_candidates_include_bank_login(selector_candidates)
            ):
                safe_diagnostics["screen_state"] = "login_required"
                safe_diagnostics["screen_reason_code"] = "BANK_LOGIN_INPUTS_VISIBLE"
                safe_diagnostics["screen_suggested_action"] = "fill_saved_login"
                safe_diagnostics["screen_requires_operator"] = "0"

        auto_navigation_triggered = False
        if (
            not rows
            and not skip_initial_snapshot
            and safe_diagnostics.get("screen_state") not in auth_states
        ):
            clicked_navigation_keys: list[str] = []
            navigation_labels: list[str] = []
            for _attempt in range(3):
                nav_result = await _try_open_transaction_view(page, clicked_navigation_keys)
                if nav_result.get("clicked") != "1":
                    break
                auto_navigation_triggered = True
                safe_diagnostics["transaction_view_navigation"] = "triggered"
                if nav_result.get("key"):
                    clicked_navigation_keys.append(str(nav_result["key"]))
                if nav_result.get("label"):
                    navigation_labels.append(str(nav_result["label"]))
                    clicked_navigation_keys.append(str(nav_result["label"]))
                    safe_diagnostics["transaction_view_navigation_label"] = nav_result["label"]
                    safe_diagnostics["transaction_view_navigation_labels"] = navigation_labels[:5]
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                try:
                    current_url, rows, parse_diag, state_text = await _read_bank_portal_snapshot(page)
                    if rows and (date_from or date_to):
                        rows = [r for r in rows if _row_in_date_range(r, date_from, date_to)]
                    safe_diagnostics["current_url"] = current_url
                    safe_diagnostics["parser_table_count"] = parse_diag["table_count"]
                    safe_diagnostics["parser_failure"] = parse_diag["parse_failure"]
                    safe_diagnostics["parser_transaction_header_found"] = parse_diag.get("transaction_header_found", False)
                    if rows:
                        safe_diagnostics["screen_state"] = "transaction_table"
                        safe_diagnostics["screen_reason_code"] = "TRANSACTION_TABLE_VISIBLE_AFTER_NAVIGATION"
                        safe_diagnostics["screen_suggested_action"] = "parse_table"
                        safe_diagnostics["screen_requires_operator"] = "0"
                        break
                    rechecked_selectors = await _safe_selector_candidates(page)
                    if rechecked_selectors:
                        safe_diagnostics["selector_candidates"] = rechecked_selectors
                        if _selector_candidates_include_bank_login(rechecked_selectors):
                            safe_diagnostics["screen_state"] = "login_required"
                            safe_diagnostics["screen_reason_code"] = "BANK_LOGIN_INPUTS_VISIBLE_AFTER_NAVIGATION"
                            safe_diagnostics["screen_suggested_action"] = "fill_saved_login"
                            safe_diagnostics["screen_requires_operator"] = "0"
                            break
                    rechecked_decision = classify_portal_state(current_url, state_text)
                    if rechecked_decision.state in auth_states:
                        rechecked_state = rechecked_decision.as_dict()
                        safe_diagnostics["screen_state"] = rechecked_state.get("state", "unknown")
                        safe_diagnostics["screen_reason_code"] = rechecked_state.get("reason_code", "")
                        safe_diagnostics["screen_suggested_action"] = rechecked_state.get("suggested_action", "no_action")
                        safe_diagnostics["screen_requires_operator"] = "1" if rechecked_state.get("requires_operator") else "0"
                        break
                    elif parse_diag.get("parse_failure"):
                        safe_diagnostics["screen_state"] = "parse_failed"
                        safe_diagnostics["screen_reason_code"] = "PARSE_FAILED_AFTER_NAVIGATION"
                        safe_diagnostics["screen_suggested_action"] = "retry_with_same_session"
                        safe_diagnostics["screen_requires_operator"] = "0"
                except Exception:
                    safe_diagnostics["transaction_view_navigation"] = "failed"
                    break

        login_fallback_triggered = False
        challenge_focus_triggered = False
        login_auto_fill_result: dict[str, str] = {"attempted": "0"}
        shinhan_flow_result: dict[str, str] = {"attempted": "0"}
        if not rows and shinhan_flow_mode:
            shinhan_steps: list[dict[str, str]] = []
            aggregate_flow: dict[str, str] = {"attempted": "0", "mode": shinhan_flow_mode}
            if shinhan_flow_mode == "individual_simple":
                safe_diagnostics["shinhan_dialog_auto_accept"] = (
                    "installed" if _install_dialog_auto_accept(page) else "unavailable"
                )
            for attempt_index in range(4):
                if shinhan_flow_mode == "individual_simple":
                    notice_closed = await _close_shinhan_security_notice(page)
                    if notice_closed:
                        safe_diagnostics["shinhan_security_notice_closed"] = "1"
                        try:
                            await page.wait_for_load_state("networkidle", timeout=2000)
                        except Exception:
                            pass
                step_started_at = time.monotonic()
                try:
                    if (
                        shinhan_flow_mode == "individual_simple"
                        and login_username
                        and login_password
                        and (attempt_index == 0 or retry_shinhan_idpw_login)
                    ):
                        if retry_shinhan_idpw_login:
                            safe_diagnostics["shinhan_idpw_login_retried_after_certificate"] = "1"
                        retry_shinhan_idpw_login = False
                        step_result = await _try_shinhan_individual_login_step(
                            page,
                            username=str(login_username or ""),
                            password=str(login_password or ""),
                        )
                        if step_result.get("attempted") != "1":
                            step_result = await _try_prepare_shinhan_query_flow(
                                page,
                                flow_mode=shinhan_flow_mode,
                                username=str(login_username or ""),
                                password=str(login_password or ""),
                                account_no=str(account_no or ""),
                                account_password=str(account_password or ""),
                                business_registration_no=str(business_registration_no or ""),
                                date_from=str(date_from or ""),
                                date_to=str(date_to or ""),
                            )
                    else:
                        step_result = await _try_prepare_shinhan_query_flow(
                            page,
                            flow_mode=shinhan_flow_mode,
                            username=str(login_username or ""),
                            password=str(login_password or ""),
                            account_no=str(account_no or ""),
                            account_password=str(account_password or ""),
                            business_registration_no=str(business_registration_no or ""),
                            date_from=str(date_from or ""),
                            date_to=str(date_to or ""),
                        )
                    returned_error = str(step_result.get("error_code") or "").upper()
                    if returned_error in _BANK_SESSION_RECOVERABLE_ERROR_CODES:
                        raise _ShinhanResumeSignal(returned_error)
                except Exception as exc:
                    if (
                        shinhan_flow_mode == "individual_simple"
                        and _is_bank_session_recoverable_error(exc)
                        and attempt_index < 3
                        and browser_work_key
                    ):
                        safe_diagnostics["resume_stage"] = "account_selection"
                        safe_diagnostics["resume_attempted"] = "1"
                        recovery_code = _bank_session_error_code(exc) or "CDP_NOT_READY"
                        try:
                            session = await bridge.ensure_work_session(
                                work_key=browser_work_key,
                                label=f"{bank_name or '신한은행'} 간편조회",
                                agent_id=str(browser_agent_id or ""),
                                url=portal_url or BANK_PORTAL_URLS["shinhan_business"],
                                preferred_port=browser_preferred_port,
                                force_recreate=False,
                                queue_wait_timeout_seconds=launch_queue_wait_seconds,
                                command_timeout_seconds=launch_timeout_seconds,
                            )
                            session_id_to_use = str(getattr(session, "session_id", "") or session_id_to_use)
                            context = await bridge._context_for_session(session)
                            pages = getattr(context, "pages", None)
                            page, _resume_url, _ = await _select_bank_page(pages, portal_url)
                            if page is None:
                                page = await context.new_page()
                            _disable_local_agent_auto_recovery(page)
                            safe_diagnostics["browser_session_id"] = session_id_to_use
                            safe_diagnostics["cdp_preflight_session_id"] = session_id_to_use
                            safe_diagnostics["session_recovery"] = "reacquired_same_work_key"
                            safe_diagnostics["session_recovery_error"] = recovery_code
                            continue
                        except Exception as resume_exc:
                            safe_diagnostics["resume_error"] = _bank_session_error_code(resume_exc) or "CDP_NOT_READY"
                    step_result = {
                        "attempted": "failed",
                        "stage": "account_selection",
                        **_safe_browser_error_fields(exc),
                    }
                step_result["attempt_index"] = str(attempt_index + 1)
                shinhan_steps.append(step_result)
                _append_shinhan_flow_stage_logs(
                    shinhan_stage_logs,
                    step_result=step_result,
                    started_at=step_started_at,
                    attempt_index=attempt_index,
                )
                auth_challenge = await _detect_shinhan_auth_challenge(page, getattr(context, "pages", None))
                if auth_challenge:
                    if shinhan_flow_mode == "individual_simple" and login_username and login_password:
                        safe_diagnostics["shinhan_auth_challenge_detected_after_idpw_step"] = "1"
                        safe_diagnostics["shinhan_auth_challenge_reason_code"] = str(
                            auth_challenge.get("screen_reason_code") or ""
                        )[:120]
                        safe_diagnostics["shinhan_auth_challenge_policy"] = "retry_saved_idpw_login"
                        reset_result = await _prefer_shinhan_idpw_login_after_auth_challenge(page, portal_url)
                        safe_diagnostics["shinhan_idpw_login_reset"] = reset_result
                        retry_shinhan_idpw_login = True
                        continue
                    else:
                        safe_diagnostics.update(auth_challenge)
                        safe_diagnostics["shinhan_query_flow_steps"] = shinhan_steps
                        return {
                            "status": "action_required",
                            "error_code": "BANK_BROWSER_AUTH_CHALLENGE_DETECTED",
                            "rows": [],
                            "row_count": 0,
                            "diagnostics": safe_diagnostics,
                            "message": "신한 금융인증/권한 확인이 필요합니다. ID/PW 저장값이 없으면 인증 완료 후 같은 work key로 재시도하십시오.",
                        }
                for key, value in step_result.items():
                    if key in {"mode", "stage"}:
                        aggregate_flow[key] = value
                    elif str(value) == "1":
                        aggregate_flow[key] = "1"
                    elif key not in aggregate_flow:
                        aggregate_flow[key] = str(value)
                actionable = any(
                    step_result.get(key) == "1"
                    for key in (
                        "navigation_clicked",
                        "websquare_triggered",
                        "login_success",
                        "account_page_navigation",
                        "account_page_direct_hash",
                        "notice_confirm",
                        "username",
                        "login_secret",
                        "account_no",
                        "account_direct_input",
                        "account_selected",
                        "account_resolved",
                        "account_secret",
                        "business_registration_no",
                        "date_from",
                        "date_to",
                        "query_submitted",
                    )
                )
                if step_result.get("attempted") != "1" or not actionable:
                    break
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                try:
                    rechecked_url, rechecked_rows, rechecked_diag, rechecked_text = await _read_bank_portal_snapshot(page)
                    rechecked_url = rechecked_url or current_url
                    if rechecked_rows and (date_from or date_to):
                        rechecked_rows = [
                            r for r in rechecked_rows if _row_in_date_range(r, date_from, date_to)
                        ]
                    safe_diagnostics["shinhan_query_recheck_attempted"] = "1"
                    safe_diagnostics["shinhan_query_recheck_table_count"] = rechecked_diag["table_count"]
                    if rechecked_url != current_url:
                        safe_diagnostics["shinhan_query_recheck_url_changed"] = "1"
                    current_url = rechecked_url
                    parse_diag = rechecked_diag
                    safe_diagnostics["current_url"] = current_url
                    safe_diagnostics["parser_table_count"] = rechecked_diag["table_count"]
                    safe_diagnostics["parser_failure"] = rechecked_diag["parse_failure"]
                    safe_diagnostics["parser_transaction_header_found"] = rechecked_diag.get("transaction_header_found", False)
                    if rechecked_rows:
                        rows = rechecked_rows
                        safe_diagnostics["screen_state"] = "transaction_table"
                        safe_diagnostics["screen_reason_code"] = "TRANSACTION_TABLE_VISIBLE_AFTER_SHINHAN_QUERY"
                        safe_diagnostics["screen_suggested_action"] = "parse_table"
                        safe_diagnostics["screen_requires_operator"] = "0"
                        _append_shinhan_stage_log(
                            shinhan_stage_logs,
                            stage="shinhan_query_success",
                            status="success",
                            started_at=step_started_at,
                            reason="transaction_table_visible",
                            attempt_index=str(attempt_index + 1),
                            row_count=str(len(rechecked_rows)),
                        )
                        _append_shinhan_stage_log(
                            shinhan_stage_logs,
                            stage="shinhan_data_collection",
                            status="success",
                            started_at=step_started_at,
                            reason="rows_parsed",
                            attempt_index=str(attempt_index + 1),
                            row_count=str(len(rechecked_rows)),
                        )
                        break
                    notice_state = await _shinhan_security_notice_state(page)
                    if str(notice_state.get("present") or "") == "1":
                        safe_diagnostics["shinhan_security_notice_after_step"] = "1"
                        safe_diagnostics["shinhan_security_notice_code_after_step"] = str(
                            notice_state.get("error_code") or "SHINHAN_SECURITY_NOTICE"
                        )[:120]
                        if await _close_shinhan_security_notice(page):
                            safe_diagnostics["shinhan_security_notice_closed_after_step"] = "1"
                        if attempt_index < 3:
                            continue
                    rechecked_decision = classify_portal_state(rechecked_url, rechecked_text)
                    rechecked_state = rechecked_decision.as_dict()
                    safe_diagnostics["screen_state"] = rechecked_state.get("state", "unknown")
                    safe_diagnostics["screen_reason_code"] = rechecked_state.get("reason_code", "")
                    safe_diagnostics["screen_suggested_action"] = rechecked_state.get("suggested_action", "no_action")
                    safe_diagnostics["screen_requires_operator"] = "1" if rechecked_state.get("requires_operator") else "0"
                    if step_result.get("query_submitted") == "1" or rechecked_decision.state in auth_states:
                        break
                except Exception:
                    safe_diagnostics["shinhan_query_recheck_attempted"] = "failed"
                    break
            shinhan_flow_result = aggregate_flow
            safe_diagnostics["shinhan_query_flow"] = shinhan_flow_result
            safe_diagnostics["shinhan_query_flow_steps"] = shinhan_steps
            if not rows and not any(item.get("stage") == "shinhan_data_collection" for item in shinhan_stage_logs):
                _append_shinhan_stage_log(
                    shinhan_stage_logs,
                    stage="shinhan_data_collection",
                    status="failed",
                    started_at=time.monotonic(),
                    error_code=str(safe_diagnostics.get("screen_reason_code") or "TRANSACTION_ROWS_NOT_OBSERVED"),
                    reason=str(safe_diagnostics.get("screen_state") or "rows_empty"),
                )

        if not rows and ibk_service:
            ibk_steps: list[dict[str, str]] = []
            aggregate_flow: dict[str, str] = {"attempted": "0", "mode": "ibk_quick"}
            for attempt_index in range(3):
                step_result = await _try_prepare_ibk_quick_flow(
                    page,
                    username=str(login_username or ""),
                    password=str(login_password or ""),
                    account_no=str(account_no or ""),
                    account_password=str(account_password or ""),
                    business_registration_no=str(business_registration_no or ""),
                    date_from=str(date_from or ""),
                    date_to=str(date_to or ""),
                )
                step_result["attempt_index"] = str(attempt_index + 1)
                ibk_steps.append(step_result)
                for key, value in step_result.items():
                    if key in {"mode", "stage"}:
                        aggregate_flow[key] = value
                    elif str(value) == "1":
                        aggregate_flow[key] = "1"
                    elif key not in aggregate_flow:
                        aggregate_flow[key] = str(value)
                actionable = any(
                    step_result.get(key) == "1"
                    for key in (
                        "username",
                        "login_secret",
                        "account_no",
                        "account_secret",
                        "business_registration_no",
                        "date_from",
                        "date_to",
                        "navigation_clicked",
                        "query_submitted",
                    )
                )
                if step_result.get("attempted") != "1" or not actionable:
                    break
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                try:
                    rechecked_url, rechecked_rows, rechecked_diag, rechecked_text = await _read_bank_portal_snapshot(page)
                    rechecked_url = rechecked_url or current_url
                    if rechecked_rows and (date_from or date_to):
                        rechecked_rows = [
                            row for row in rechecked_rows if _row_in_date_range(row, date_from, date_to)
                        ]
                    safe_diagnostics["ibk_query_recheck_attempted"] = "1"
                    safe_diagnostics["ibk_query_recheck_table_count"] = rechecked_diag["table_count"]
                    if rechecked_url != current_url:
                        safe_diagnostics["ibk_query_recheck_url_changed"] = "1"
                    current_url = rechecked_url
                    parse_diag = rechecked_diag
                    safe_diagnostics["current_url"] = current_url
                    safe_diagnostics["parser_table_count"] = rechecked_diag["table_count"]
                    safe_diagnostics["parser_failure"] = rechecked_diag["parse_failure"]
                    safe_diagnostics["parser_transaction_header_found"] = rechecked_diag.get("transaction_header_found", False)
                    if rechecked_rows:
                        rows = rechecked_rows
                        safe_diagnostics["screen_state"] = "transaction_table"
                        safe_diagnostics["screen_reason_code"] = "TRANSACTION_TABLE_VISIBLE_AFTER_IBK_QUERY"
                        safe_diagnostics["screen_suggested_action"] = "parse_table"
                        safe_diagnostics["screen_requires_operator"] = "0"
                        break
                    rechecked_decision = classify_portal_state(rechecked_url, rechecked_text)
                    rechecked_state = rechecked_decision.as_dict()
                    safe_diagnostics["screen_state"] = rechecked_state.get("state", "unknown")
                    safe_diagnostics["screen_reason_code"] = rechecked_state.get("reason_code", "")
                    safe_diagnostics["screen_suggested_action"] = rechecked_state.get("suggested_action", "no_action")
                    safe_diagnostics["screen_requires_operator"] = "1" if rechecked_state.get("requires_operator") else "0"
                    if step_result.get("query_submitted") == "1" or rechecked_decision.state in auth_states:
                        break
                except Exception:
                    safe_diagnostics["ibk_query_recheck_attempted"] = "failed"
                    break
            safe_diagnostics["ibk_query_flow"] = aggregate_flow
            safe_diagnostics["ibk_query_flow_steps"] = ibk_steps

        screen_state = _diagnostic_screen_state(safe_diagnostics)
        if not rows and screen_state.get("state") == "login_required":
            login_auto_fill_result = await _try_fill_bank_login(
                page,
                username=str(login_username or ""),
                password=str(login_password or ""),
                account_no=str(account_no or ""),
                account_password=str(account_password or ""),
                business_registration_no=str(business_registration_no or ""),
            )
            safe_diagnostics["bank_login_auto_fill"] = login_auto_fill_result
        if not rows and login_auto_fill_result.get("submitted") == "1":
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            try:
                rechecked_url, rechecked_rows, rechecked_diag, rechecked_text = await _read_bank_portal_snapshot(page)
                rechecked_url = rechecked_url or current_url
                if rechecked_rows and (date_from or date_to):
                    rechecked_rows = [
                        r for r in rechecked_rows if _row_in_date_range(r, date_from, date_to)
                    ]
                safe_diagnostics["login_recheck_attempted"] = "1"
                safe_diagnostics["login_recheck_table_count"] = rechecked_diag["table_count"]
                if rechecked_url != current_url:
                    safe_diagnostics["login_recheck_url_changed"] = "1"
                if rechecked_rows:
                    rows = rechecked_rows
                    parse_diag = rechecked_diag
                    safe_diagnostics["parser_table_count"] = rechecked_diag["table_count"]
                    safe_diagnostics["parser_failure"] = rechecked_diag["parse_failure"]
                    safe_diagnostics["parser_transaction_header_found"] = rechecked_diag.get("transaction_header_found", False)
                    safe_diagnostics["screen_state"] = "transaction_table"
                    safe_diagnostics["screen_reason_code"] = "TRANSACTION_TABLE_VISIBLE_AFTER_LOGIN"
                else:
                    rechecked_decision = classify_portal_state(rechecked_url, rechecked_text)
                    rechecked_state = rechecked_decision.as_dict()
                    safe_diagnostics["screen_state"] = rechecked_state.get("state", "unknown")
                    safe_diagnostics["screen_reason_code"] = rechecked_state.get("reason_code", "")
                    safe_diagnostics["screen_suggested_action"] = rechecked_state.get("suggested_action", "no_action")
                    safe_diagnostics["screen_requires_operator"] = "1" if rechecked_state.get("requires_operator") else "0"
            except Exception:
                safe_diagnostics["login_recheck_attempted"] = "failed"

        screen_state = _diagnostic_screen_state(safe_diagnostics)
        if not rows and screen_state.get("suggested_action") == "focus_password_manager":
            login_fallback_triggered = await _trigger_password_manager_fallback(page)
            safe_diagnostics["password_manager_fallback"] = (
                "triggered" if login_fallback_triggered else "unavailable"
            )
        elif not rows and screen_state.get("suggested_action") in {
            "focus_operator_input",
            "wait_for_push_approval",
        }:
            challenge_focus_triggered = await _focus_auth_challenge_input(
                page,
                str(screen_state.get("state") or ""),
            )
            safe_diagnostics["auth_challenge_focus"] = (
                "triggered" if challenge_focus_triggered else "unavailable"
            )

        if not rows and (login_fallback_triggered or challenge_focus_triggered):
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            try:
                rechecked_url, rechecked_rows, rechecked_diag, _rechecked_text = await _read_bank_portal_snapshot(page)
                rechecked_url = rechecked_url or current_url
                if rechecked_rows and (date_from or date_to):
                    rechecked_rows = [
                        r for r in rechecked_rows if _row_in_date_range(r, date_from, date_to)
                    ]
                safe_diagnostics["recheck_attempted"] = "1"
                safe_diagnostics["recheck_table_count"] = rechecked_diag["table_count"]
                if rechecked_url != current_url:
                    safe_diagnostics["recheck_url_changed"] = "1"
                if rechecked_rows:
                    rows = rechecked_rows
                    parse_diag = rechecked_diag
                    safe_diagnostics["parser_table_count"] = rechecked_diag["table_count"]
                    safe_diagnostics["parser_failure"] = rechecked_diag["parse_failure"]
                    safe_diagnostics["parser_transaction_header_found"] = rechecked_diag.get("transaction_header_found", False)
                    safe_diagnostics["screen_state"] = "transaction_table"
                    safe_diagnostics["screen_reason_code"] = "TRANSACTION_TABLE_VISIBLE_AFTER_RECHECK"
            except Exception:
                safe_diagnostics["recheck_attempted"] = "failed"

        download_blocked_states = {
            "captcha_required",
            "otp_required",
            "identity_check_required",
            "certificate_password_required",
            "login_required",
        }
        if not rows and safe_diagnostics.get("screen_state") not in download_blocked_states:
            download_rows, download_diag = await _try_download_bank_statement(page, date_from, date_to)
            safe_diagnostics.update(download_diag)
            if download_rows:
                rows = download_rows
                parse_diag = {
                    "table_count": int(download_diag.get("download_table_count") or 0),
                    "headers_found": [],
                    "parse_failure": False,
                    "transaction_header_found": True,
                }
                safe_diagnostics["screen_state"] = "transaction_download"
                safe_diagnostics["screen_reason_code"] = "TRANSACTION_ROWS_PARSED_FROM_DOWNLOAD"
                safe_diagnostics["screen_suggested_action"] = "record_download_rows"
                safe_diagnostics["screen_requires_operator"] = "0"

        if rows:
            if shinhan_service and not any(item.get("stage") == "shinhan_data_collection" for item in shinhan_stage_logs):
                _append_shinhan_stage_log(
                    shinhan_stage_logs,
                    stage="shinhan_data_collection",
                    status="success",
                    started_at=time.monotonic(),
                    reason="rows_parsed",
                    row_count=str(len(rows)),
                )
            msg = f"{bank_name or '은행'} 포털에서 {len(rows)}건 수집했습니다."
        elif safe_diagnostics.get("screen_state") == "no_records":
            if shinhan_service and not any(item.get("stage") == "shinhan_data_collection" for item in shinhan_stage_logs):
                _append_shinhan_stage_log(
                    shinhan_stage_logs,
                    stage="shinhan_data_collection",
                    status="success",
                    started_at=time.monotonic(),
                    reason="no_records",
                    row_count="0",
                )
            msg = f"{bank_name or '은행'} 포털에 해당 기간 거래 내역이 없습니다."
        elif parse_diag["parse_failure"]:
            msg = (
                f"{bank_name or '은행'} 포털 테이블을 인식하지 못했습니다 "
                f"(테이블 {parse_diag['table_count']}개 발견, 날짜 컬럼 없음). "
                "CSV 업로드로 대체 수집하거나 포털 레이아웃 변경을 확인하십시오."
            )
        elif parse_diag["table_count"] == 0:
            msg = (
                f"{bank_name or '은행'} 포털 페이지에서 테이블을 찾지 못했습니다. "
                "페이지가 완전히 로드되지 않았거나 로그인이 필요할 수 있습니다."
            )
        else:
            msg = f"{bank_name or '은행'} 포털에 해당 기간 거래 내역이 없습니다."

        actionable_states = {
            "captcha_required",
            "otp_required",
            "identity_check_required",
            "certificate_password_required",
            "login_required",
        }
        if not rows and safe_diagnostics.get("screen_state") == "no_records":
            return {
                "status": "collected",
                "rows": [],
                "row_count": 0,
                "diagnostics": safe_diagnostics,
                "message": msg,
            }

        if not rows and (
            login_fallback_triggered
            or challenge_focus_triggered
            or auto_navigation_triggered
            or safe_diagnostics.get("screen_state") in actionable_states
            or (auto_open_browser and (parse_diag["table_count"] == 0 or parse_diag["parse_failure"]))
        ):
            if safe_diagnostics.get("screen_state") in {
                "captcha_required",
                "otp_required",
                "identity_check_required",
                "certificate_password_required",
            }:
                error_code = "BANK_BROWSER_AUTH_CHALLENGE_DETECTED"
            elif login_fallback_triggered:
                error_code = "PC_AGENT_LOGIN_REQUIRED"
            else:
                error_code = "BANK_BROWSER_OPERATOR_ACTION_REQUIRED"
            return {
                "status": "action_required",
                "error_code": error_code,
                "rows": [],
                "row_count": 0,
                "diagnostics": safe_diagnostics,
                "message": (
                    f"{bank_name or '은행'} 기업페이지를 PC Agent 브라우저로 준비했습니다. "
                    "비밀번호관리자/Vault/인증 입력 focus 후 1회 재확인했습니다. "
                    "자동 처리되지 않는 OTP/CAPTCHA/본인인증 단계만 완료하면 같은 세션에서 다시 수집합니다."
                ),
            }

        return {
            "status": "collected",
            "rows": rows,
            "row_count": len(rows),
            "diagnostics": safe_diagnostics,
            "message": msg,
        }

    except Exception as exc:
        exc_error_code = str(getattr(exc, "error_code", "") or "").strip()
        if exc_error_code:
            safe_diagnostics["pc_agent_error_code"] = exc_error_code
        exc_detail = _safe_error_detail(getattr(exc, "detail", None))
        if exc_detail:
            safe_diagnostics["pc_agent_error_detail"] = exc_detail
        if (
            auto_open_browser
            and browser_work_key
            and not _recovery_attempted
            and _is_bank_session_recoverable_error(exc)
        ):
            recovery_code = _bank_session_error_code(exc) or exc_error_code or "BANK_BROWSER_SESSION_ERROR"
            recovered = await collect_bank_via_browser_session_async(
                account,
                browser_session_id="",
                browser_work_key=browser_work_key,
                date_from=date_from,
                date_to=date_to,
                portal_url=portal_url,
                auto_open_browser=True,
                browser_agent_id=browser_agent_id,
                browser_preferred_port=browser_preferred_port,
                force_recreate_browser=True,
                login_username=login_username,
                login_password=login_password,
                account_no=account_no,
                account_password=account_password,
                business_registration_no=business_registration_no,
                business_entity_type=business_entity_type,
                browser_timeout_seconds=browser_timeout_seconds,
                _recovery_attempted=True,
            )
            recovered_diag = recovered.setdefault("diagnostics", {})
            if isinstance(recovered_diag, dict):
                recovered_diag.setdefault("previous_browser_session_id", session_id_to_use)
                recovered_diag["session_recovery"] = "recreated_after_runtime_error"
                recovered_diag["session_recovery_error"] = recovery_code
                recovered_diag["session_recovery_plan"] = _bank_session_recovery_plan(recovery_code)
            return recovered
        error_code = (
            "BANK_BROWSER_PC_AGENT_TIMEOUT"
            if exc_error_code in {"COMMAND_TIMEOUT", "RUNTIME_EVALUATE_TIMEOUT"}
            or isinstance(exc, TimeoutError)
            else "BANK_BROWSER_UNEXPECTED_ERROR"
        )
        return {
            "status": "failed",
            "error_code": error_code,
            "rows": [],
            "row_count": 0,
            "diagnostics": safe_diagnostics,
            "message": f"은행 브라우저 수집 중 오류: {str(exc)[:300]}",
        }


def _row_in_date_range(row: dict[str, Any], date_from: str, date_to: str) -> bool:
    occurred = str(row.get("occurred_at") or "")
    if not occurred:
        return True
    date_part = occurred[:10]
    if date_from and date_part < date_from:
        return False
    if date_to and date_part > date_to:
        return False
    return True
