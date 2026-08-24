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

import hashlib
import html
import importlib.util
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from app.services.auth_challenge_orchestrator import classify_portal_state
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


# ── Work-key generation ──────────────────────────────────────────────────────

def bank_browser_work_key(account_id: str, business_id: str, branch_id: str) -> str:
    """Return a deterministic, opaque Browser Bridge work key.

    All three identifiers are hashed so no human-readable branch name,
    account alias, or account ID appears in the work key.
    """
    raw = f"{account_id}|{business_id}|{branch_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"yeoljeong-bank-browser-{digest}"


def _diagnostic_screen_state(diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": str(diagnostics.get("screen_state") or "unknown"),
        "reason_code": str(diagnostics.get("screen_reason_code") or ""),
        "suggested_action": str(diagnostics.get("screen_suggested_action") or "no_action"),
        "requires_operator": str(diagnostics.get("screen_requires_operator") or "") == "1",
    }


async def _trigger_password_manager_fallback(page: Any) -> bool:
    """Focus a login field so the browser password manager can assist.

    This does not read field values, submit forms, or bypass OTP/CAPTCHA.
    It only dispatches focus/input events in the connected PC Agent session.
    """
    try:
        return bool(await page.evaluate(
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
            """
        ))
    except Exception:
        return False


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
        return bool(await page.evaluate(
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
        ))
    except Exception:
        return False


async def _safe_selector_candidates(page: Any) -> list[dict[str, str]]:
    """Collect non-secret selector hints for diagnostics."""
    try:
        raw = await page.evaluate(
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
            """
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
        raw = await page.evaluate(
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
        raw = await page.evaluate(
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
              const fieldText = (el) => String([
                el?.id,
                el?.getAttribute?.('name'),
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
                return true;
              };
              const scoreButton = (label, mode) => {
                const value = String(label || '').toLowerCase();
                if (!value) return 0;
                const deny = ['이체', '송금', '납부', '삭제', '해지', '출금'];
                if (deny.some((word) => value.includes(word))) return 0;
                if (mode === 'corporate_quick') {
                  if (value.includes('법인빠른조회') || value.includes('법인 빠른조회')) return 120;
                  if (value.includes('빠른조회') && value.includes('계좌')) return 100;
                  if (value.includes('간편서비스') && value.includes('계좌')) return 70;
                } else {
                  if (value.includes('간편조회서비스') && value.includes('계좌')) return 120;
                  if (value.includes('간편조회') && value.includes('계좌')) return 100;
                  if (value.includes('계좌조회')) return 80;
                  if (value.includes('로그인')) return 60;
                }
                if (value.includes('조회') || value.includes('검색') || value.includes('확인')) return 45;
                return 0;
              };
              const clickBest = (mode) => {
                const candidates = Array.from(document.querySelectorAll('a,button,input[type=button],input[type=submit]'))
                  .filter(visible)
                  .map((el, index) => ({el, index, label: textOf(el)}))
                  .map((item) => ({...item, score: scoreButton(item.label, mode)}))
                  .filter((item) => item.score > 0)
                  .sort((a, b) => b.score - a.score);
                const item = candidates[0];
                if (!item) return '';
                item.el.focus();
                item.el.click();
                return item.label.slice(0, 40);
              };
              const inputs = () => Array.from(document.querySelectorAll('input,textarea')).filter(visible);
              const firstInput = (patterns, type = '') => inputs().find((el) => {
                if (type && String(el.type || '').toLowerCase() !== type) return false;
                const text = fieldText(el);
                return patterns.some((pattern) => pattern.test(text));
              }) || null;
              const fillByPattern = (patterns, value, type = '') => setValue(firstInput(patterns, type), value);
              const selectAccount = () => {
                const target = digits(input.accountNo);
                const suffix = target.slice(-4);
                if (!target) return false;
                for (const select of Array.from(document.querySelectorAll('select')).filter(visible)) {
                  const options = Array.from(select.options || []);
                  const match = options.find((option) => {
                    const optionDigits = digits(option.textContent || option.value);
                    return optionDigits && (optionDigits === target || optionDigits.endsWith(suffix));
                  });
                  if (match) {
                    select.value = match.value;
                    select.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                  }
                }
                const choice = Array.from(document.querySelectorAll('input[type=radio],input[type=checkbox]'))
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
              const result = {
                attempted: '1',
                mode: input.mode,
                navigation_clicked: clickBest(input.mode) ? '1' : '0',
                username: '0',
                login_secret: '0',
                account_no: '0',
                account_selected: '0',
                account_secret: '0',
                business_registration_no: '0',
                date_from: '0',
                date_to: '0',
                query_submitted: '0'
              };
              if (input.mode === 'individual_simple') {
                result.username = fillByPattern([
                  /아이디|user|login.*id|cust.*id|member.*id/i
                ], input.username) ? '1' : '0';
                result.login_secret = fillByPattern([
                  /비밀번호|password|passwd|login.*pw/i
                ], input.password, 'password') ? '1' : '0';
              }
              result.account_no = fillByPattern([
                /계좌번호|account|acct/i
              ], input.accountNo) ? '1' : '0';
              result.account_selected = selectAccount() ? '1' : '0';
              result.account_secret = fillByPattern([
                /계좌.*비밀번호|계좌.*암호|account.*password|account.*pw|acct.*pw/i
              ], input.accountPassword, 'password') ? '1' : '0';
              result.business_registration_no = fillByPattern([
                /사업자|사업자등록|business|bizno|registration/i
              ], input.businessRegistrationNo) ? '1' : '0';
              result.date_from = fillByPattern([
                /시작일|조회시작|조회기간.*시작|from|start|fr[_-]?dt|from[_-]?date/i
              ], input.dateFrom) ? '1' : '0';
              result.date_to = fillByPattern([
                /종료일|조회종료|조회기간.*종료|to|end|to[_-]?dt|to[_-]?date/i
              ], input.dateTo) ? '1' : '0';
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
        )
    except Exception:
        return {"attempted": "failed", "mode": flow_mode}
    if not isinstance(raw, dict):
        return {"attempted": "failed", "mode": flow_mode}
    result: dict[str, str] = {"mode": flow_mode}
    for key in (
        "attempted",
        "navigation_clicked",
        "username",
        "login_secret",
        "account_no",
        "account_selected",
        "account_secret",
        "business_registration_no",
        "date_from",
        "date_to",
        "query_submitted",
    ):
        result[key] = "1" if str(raw.get(key) or "") == "1" else "0"
    return result


async def _visible_page_url(page: Any) -> str:
    try:
        return str(await page.evaluate("window.location.href") or "")
    except Exception:
        return ""


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
        raw = await page.evaluate(
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


async def _read_bank_portal_snapshot(page: Any) -> tuple[str, list[dict[str, Any]], dict[str, Any], str]:
    """Read a lightweight, redacted portal snapshot.

    Full WebSquare/enterprise-bank HTML can be huge and may timeout through
    PC Agent. Prefer bounded table/text extraction and only fall back to
    innerHTML for simple test pages or lightweight portals.
    """
    current_url = ""
    try:
        current_url = str(await page.evaluate("window.location.href") or "")
    except Exception:
        current_url = ""

    tables: list[list[list[str]]] = []
    try:
        raw_tables = await page.evaluate(
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
            """
        )
        if isinstance(raw_tables, list):
            tables = raw_tables
    except Exception:
        tables = []

    rows, parse_diag = _parse_tables_with_diagnostics(tables)
    state_text = ""
    try:
        raw_text = await page.evaluate(
            "document.body ? String(document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 4000) : ''"
        )
        if isinstance(raw_text, str):
            state_text = _safe_portal_text(raw_text)
    except Exception:
        state_text = ""

    if not tables and not state_text:
        try:
            html_content = str(await page.evaluate("document.body ? document.body.innerHTML : ''") or "")
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
    }
    shinhan_service = _is_shinhan_service(bank_code, bank_name, institution_code, portal_url)
    shinhan_flow_mode = _shinhan_query_flow_mode(business_entity_type, account) if shinhan_service else ""
    if shinhan_flow_mode:
        safe_diagnostics["shinhan_query_flow_mode"] = shinhan_flow_mode

    session_id_to_use = browser_session_id.strip() if browser_session_id else ""
    auto_opened_session = False

    if not session_id_to_use and browser_work_key:
        try:
            from app.browser_bridge.service import get_browser_bridge_service

            bridge = get_browser_bridge_service()
            existing = bridge.sessions.find_by_work_key(browser_work_key)
            if existing and bridge._session_reusable(existing):
                session_id_to_use = existing.session_id
        except Exception:
            pass

    if not session_id_to_use and auto_open_browser and browser_work_key:
        try:
            from app.browser_bridge.service import get_browser_bridge_service

            bridge = get_browser_bridge_service()
            session = await bridge.ensure_work_session(
                work_key=browser_work_key,
                label=f"{bank_name or '은행'} 기업페이지",
                agent_id=str(browser_agent_id or ""),
                url=portal_url or "about:blank",
                preferred_port=browser_preferred_port,
                force_recreate=bool(force_recreate_browser),
            )
            session_id_to_use = str(getattr(session, "session_id", "") or "")
            auto_opened_session = bool(session_id_to_use)
            safe_diagnostics["auto_open_browser"] = "1"
        except Exception as exc:
            safe_diagnostics["auto_open_browser"] = "failed"
            exc_error_code = str(getattr(exc, "error_code", "") or "").strip()
            safe_diagnostics["auto_open_error"] = exc_error_code or "PC_AGENT_UNAVAILABLE"
            safe_diagnostics["auto_open_error_message"] = str(exc)[:240]
            exc_detail = _safe_error_detail(getattr(exc, "detail", None))
            if exc_detail:
                safe_diagnostics["auto_open_error_detail"] = exc_detail

    if not session_id_to_use:
        return {
            "status": "action_required",
            "error_code": "PC_AGENT_LOGIN_REQUIRED",
            "rows": [],
            "row_count": 0,
            "diagnostics": safe_diagnostics,
            "message": (
                f"{bank_name or '은행'} 브라우저 수집을 위해 PC Agent 세션이 필요합니다. "
                "PC Agent를 연결하고 관리자 화면에서 은행 웹 수집 세션을 열거나 "
                "CSV 업로드로 대체 수집하십시오."
            ),
        }

    safe_diagnostics["browser_session_id"] = session_id_to_use
    if auto_opened_session:
        safe_diagnostics["auto_opened_session"] = "1"

    try:
        from app.browser_bridge.service import get_browser_bridge_service

        bridge = get_browser_bridge_service()
        session = bridge.sessions.get(session_id_to_use)
        if not session:
            return {
                "status": "connector_not_ready",
                "error_code": "BANK_BROWSER_SESSION_NOT_FOUND",
                "rows": [],
                "row_count": 0,
                "diagnostics": safe_diagnostics,
                "message": "등록된 브라우저 세션을 찾지 못했습니다. 세션이 만료되었을 수 있습니다.",
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

        if portal_url:
            if _portal_url_reusable(initial_url, portal_url):
                safe_diagnostics["portal_navigation"] = "skipped_reusable_tab"
            else:
                try:
                    await page.goto(portal_url, wait_until="domcontentloaded", timeout=30000)
                    safe_diagnostics["portal_navigation"] = "navigated"
                except Exception:
                    safe_diagnostics["portal_navigation"] = "failed"
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass

        current_url = ""
        try:
            current_url, rows, parse_diag, state_text = await _read_bank_portal_snapshot(page)
        except Exception as exc:
            return {
                "status": "failed",
                "error_code": "BANK_BROWSER_PAGE_ERROR",
                "rows": [],
                "row_count": 0,
                "diagnostics": {**safe_diagnostics, "current_url": current_url},
                "message": f"브라우저 페이지에서 내용을 가져오지 못했습니다: {str(exc)[:200]}",
            }

        safe_diagnostics["current_url"] = current_url

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
        if not rows and safe_diagnostics.get("screen_state") not in auth_states:
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
            shinhan_flow_result = await _try_prepare_shinhan_query_flow(
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
            safe_diagnostics["shinhan_query_flow"] = shinhan_flow_result
        if not rows and shinhan_flow_result.get("query_submitted") == "1":
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
                if rechecked_rows:
                    rows = rechecked_rows
                    parse_diag = rechecked_diag
                    safe_diagnostics["parser_table_count"] = rechecked_diag["table_count"]
                    safe_diagnostics["parser_failure"] = rechecked_diag["parse_failure"]
                    safe_diagnostics["parser_transaction_header_found"] = rechecked_diag.get("transaction_header_found", False)
                    safe_diagnostics["screen_state"] = "transaction_table"
                    safe_diagnostics["screen_reason_code"] = "TRANSACTION_TABLE_VISIBLE_AFTER_SHINHAN_QUERY"
                    safe_diagnostics["screen_suggested_action"] = "parse_table"
                    safe_diagnostics["screen_requires_operator"] = "0"
                else:
                    rechecked_decision = classify_portal_state(rechecked_url, rechecked_text)
                    rechecked_state = rechecked_decision.as_dict()
                    safe_diagnostics["screen_state"] = rechecked_state.get("state", "unknown")
                    safe_diagnostics["screen_reason_code"] = rechecked_state.get("reason_code", "")
                    safe_diagnostics["screen_suggested_action"] = rechecked_state.get("suggested_action", "no_action")
                    safe_diagnostics["screen_requires_operator"] = "1" if rechecked_state.get("requires_operator") else "0"
            except Exception:
                safe_diagnostics["shinhan_query_recheck_attempted"] = "failed"

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

        if rows:
            msg = f"{bank_name or '은행'} 포털에서 {len(rows)}건 수집했습니다."
        elif safe_diagnostics.get("screen_state") == "no_records":
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
        error_code = (
            "BANK_BROWSER_PC_AGENT_TIMEOUT"
            if exc_error_code in {"COMMAND_TIMEOUT", "RUNTIME_EVALUATE_TIMEOUT"}
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
