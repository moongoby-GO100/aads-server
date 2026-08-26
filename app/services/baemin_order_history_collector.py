"""Baemin Self Service order-history collector.

This module parses the logged-in `self.baemin.com/orders/history` page into the
existing delivery sales/settlements JSONB ledgers.  It intentionally stores the
parsed data contract, not raw page HTML.
"""
from __future__ import annotations

import hashlib
import random
import re
import time
from datetime import datetime
from typing import Any


BAEMIN_ORDER_HISTORY_URL = "https://self.baemin.com/orders/history"
SCHEMA_VERSION = "baemin_order_history.v2"
ORDER_NO_RE = re.compile(r"\b[A-Z][0-9A-Z]{8,}\b")
BAEMIN_DATETIME_RE = re.compile(
    r"(20\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*\([^)]+\)\s*(오전|오후)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?"
)


class BackfillLimits:
    def __init__(
        self,
        max_records: int = 300,
        max_runtime_seconds: int = 12 * 60,
        order_detail_jitter: tuple[float, float] = (1.0, 1.8),
        page_jitter: tuple[float, float] = (2.0, 4.0),
    ) -> None:
        self.max_records = max_records
        self.max_runtime_seconds = max_runtime_seconds
        self.order_detail_jitter = order_detail_jitter
        self.page_jitter = page_jitter


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _lines(value: Any) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _money(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"-?\s*[\d,]+", text)
    if not match:
        return None
    number = match.group(0).replace(" ", "").replace(",", "")
    try:
        return int(number)
    except ValueError:
        return None


def _first_money_after(text: str, labels: tuple[str, ...]) -> int | None:
    for label in labels:
        pattern = re.compile(re.escape(label) + r"[^\d-]{0,40}(-?\s*[\d,]+)\s*원")
        match = pattern.search(text)
        if match:
            return _money(match.group(1))
    return None


def _first_label_value(text: str, labels: tuple[str, ...]) -> str:
    lines = _lines(text)
    for index, line in enumerate(lines):
        compact = line.replace(" ", "")
        for label in labels:
            label_compact = label.replace(" ", "")
            if compact == label_compact and index + 1 < len(lines):
                return _clean(lines[index + 1])
            if compact.startswith(label_compact):
                value = line[len(label) :].strip(" :：-")
                if value:
                    return _clean(value)
    for label in labels:
        match = re.search(re.escape(label) + r"\s*[:：]?\s*([^\n]+)", text)
        if match:
            return _clean(match.group(1))
    return ""


def _parse_baemin_datetime(value: Any) -> str:
    text = str(value or "")
    match = BAEMIN_DATETIME_RE.search(text)
    if not match:
        return ""
    year, month, day, meridiem, hour, minute, second = match.groups()
    hour_int = int(hour)
    if meridiem == "오후" and hour_int < 12:
        hour_int += 12
    if meridiem == "오전" and hour_int == 12:
        hour_int = 0
    return (
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        f"T{hour_int:02d}:{int(minute):02d}:{int(second or 0):02d}+09:00"
    )


def _occurred_on(ordered_at: str) -> str:
    return ordered_at[:10] if ordered_at else ""


def _checkpoint_order_no(checkpoint: dict[str, Any]) -> str:
    orders = checkpoint.get("orders") if isinstance(checkpoint.get("orders"), dict) else {}
    return _clean(checkpoint.get("last_order_no") or orders.get("last_order_no"))


def _apply_order_checkpoint(records: dict[str, list[dict[str, Any]]], checkpoint: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    last_order_no = _checkpoint_order_no(checkpoint)
    if not last_order_no:
        return records
    sales = records.get("sales") or []
    index = next((idx for idx, row in enumerate(sales) if _clean(row.get("order_no")) == last_order_no), -1)
    if index < 0:
        return records
    allowed_order_nos = {
        _clean(row.get("order_no"))
        for row in sales[index + 1 :]
        if _clean(row.get("order_no"))
    }
    filtered = dict(records)
    filtered["sales"] = sales[index + 1 :]
    filtered["settlements"] = [
        row for row in records.get("settlements", []) if _clean(row.get("order_no")) in allowed_order_nos
    ]
    return filtered


def _stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(_clean(part) for part in parts).encode("utf-8")).hexdigest()


def _segment_by_order_no(text: str) -> list[str]:
    matches = list(ORDER_NO_RE.finditer(text or ""))
    if not matches:
        return []
    segments: list[str] = []
    for index, match in enumerate(matches):
        start = 0 if index == 0 else matches[index - 1].end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segments.append(text[start:end])
    return segments


def _order_no(text: str) -> str:
    match = ORDER_NO_RE.search(text or "")
    return match.group(0) if match else ""


def _order_status(text: str) -> str:
    for status in ("배달완료", "주문취소", "주문접수", "배달중", "조리중", "결제완료"):
        if status in text:
            return status
    return ""


def _pick_known_value(text: str, candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if candidate in text:
            return candidate
    return ""


def _parse_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in _lines(text):
        if "개" not in line:
            continue
        quantity_match = re.search(r"(\d+)\s*개", line)
        if not quantity_match:
            continue
        name = re.sub(r"\s*\d+\s*개.*$", "", line).strip()
        if not name or name in {"주문정보", "정산정보"}:
            continue
        items.append({"name": name, "quantity": int(quantity_match.group(1)), "options": []})
    if not items:
        menu_summary = _first_label_value(text, ("메뉴", "주문메뉴", "메뉴명"))
        if menu_summary:
            items.append({"name": menu_summary, "quantity": 1, "options": []})
    return items


def parse_baemin_order_history_text(
    source_text: str,
    business_id: str,
    branch: str,
    *,
    collected_at: str | None = None,
) -> dict[str, Any]:
    """Parse copied/body text from Baemin order history into ledger records."""
    collected_at = collected_at or datetime.now().astimezone().isoformat(timespec="seconds")
    sales: list[dict[str, Any]] = []
    settlements: list[dict[str, Any]] = []
    detail_failed = 0
    settlement_pending = 0
    for segment in _segment_by_order_no(source_text):
        order_no = _order_no(segment)
        if not order_no:
            detail_failed += 1
            continue
        ordered_at = _parse_baemin_datetime(segment)
        accepted_at = _parse_baemin_datetime(_first_label_value(segment, ("접수시각",)))
        delivered_at = _parse_baemin_datetime(_first_label_value(segment, ("배달시각", "배달완료시각")))
        order_amount = _first_money_after(segment, ("주문금액", "총 결제금액", "결제금액"))
        payment_total = _first_money_after(segment, ("총 결제금액", "결제금액")) or order_amount
        settlement_amount = _first_money_after(segment, ("입금예정금액", "입금 예정 금액", "정산금액"))
        settlement_status_message = _first_label_value(segment, ("미확정 안내", "안내"))
        if settlement_amount is None and ("입금예정금액은" in segment or "다음날부터 확인" in segment):
            settlement_pending += 1
            settlement_status = "pending"
        else:
            settlement_status = "ready" if settlement_amount is not None else "unknown"
        settlement = {
            "status": settlement_status,
            "order_brokerage_amount": _first_money_after(segment, ("주문중개", "(A)주문중개")),
            "order_amount": order_amount,
            "brokerage_fee_amount": _first_money_after(segment, ("중개이용료", "중개 수수료")),
            "delivery_amount": _first_money_after(segment, ("(B)배달", "배달 합계")),
            "delivery_fee_amount": _first_money_after(segment, ("배달비",)),
            "etc_amount": _first_money_after(segment, ("(C)그외", "그외 합계")),
            "payment_fee_amount": _first_money_after(segment, ("결제정산수수료", "결제 정산 수수료")),
            "vat_amount": _first_money_after(segment, ("(D)부가세", "부가세")),
            "expected_deposit_amount": settlement_amount,
            "expected_deposit_on": _first_label_value(segment, ("입금예정일", "입금 예정일")),
            "status_message": settlement_status_message,
        }
        extra = {
            "primary_payment_method": _first_label_value(segment, ("주결제방법", "주 결제 방법")),
            "sub_payment_method": _first_label_value(segment, ("보조결제방법", "보조 결제 방법")),
            "store_request": _first_label_value(segment, ("가게 요청사항",)),
            "delivery_request": _first_label_value(segment, ("배달 요청사항",)),
            "processing_history": _first_label_value(segment, ("처리내역", "처리 내역")),
            "ordered_at": ordered_at,
            "accepted_at": accepted_at,
            "delivered_at": delivered_at,
        }
        record_id = _stable_id(business_id, branch, "baemin", "sales", order_no)
        sales_record = {
            "id": record_id,
            "source_id": order_no,
            "business_id": business_id,
            "branch": branch,
            "service": "baemin",
            "platform": "baemin",
            "record_type": "sales",
            "occurred_on": _occurred_on(ordered_at),
            "collected_at": collected_at,
            "order_id": order_no,
            "order_no": order_no,
            "ordered_at": ordered_at,
            "accepted_at": accepted_at,
            "delivered_at": delivered_at,
            "order_status": _order_status(segment),
            "order_channel": _pick_known_value(segment, ("배민배달(배민클럽)", "배민배달", "가게배달")),
            "store_no": _first_label_value(segment, ("가게번호", "매장 식별번호", "가게 식별번호")),
            "menu_summary": _first_label_value(segment, ("메뉴", "주문메뉴", "메뉴명")),
            "payment_type": _pick_known_value(segment, ("바로결제", "만나서결제")),
            "delivery_type": _pick_known_value(segment, ("알뜰배달", "한집배달", "가게배달", "배달")),
            "gross_amount": order_amount or 0,
            "order_amount": order_amount,
            "payment_total_amount": payment_total,
            "instant_discount_amount": _first_money_after(segment, ("즉시할인", "즉시 할인")),
            "partner_coupon_discount_amount": _first_money_after(segment, ("파트너부담 쿠폰할인", "파트너 부담 쿠폰 할인")),
            "has_order_extra_info": "주문 추가 정보" in segment,
            "items": _parse_items(segment),
            "settlement": settlement,
            "extra": extra,
            "source_url": BAEMIN_ORDER_HISTORY_URL,
            "source_collected_at": collected_at,
            "schema_version": SCHEMA_VERSION,
        }
        sales.append(sales_record)
        settlements.append(
            {
                "id": _stable_id(business_id, branch, "baemin", "settlements", order_no),
                "source_id": order_no,
                "business_id": business_id,
                "branch": branch,
                "service": "baemin",
                "platform": "baemin",
                "record_type": "settlements",
                "occurred_on": _occurred_on(ordered_at),
                "collected_at": collected_at,
                "order_id": order_no,
                "order_no": order_no,
                "sales_amount": order_amount or 0,
                "fee_amount": sum(
                    abs(value or 0)
                    for value in (
                        settlement["brokerage_fee_amount"],
                        settlement["delivery_fee_amount"],
                        settlement["payment_fee_amount"],
                        settlement["vat_amount"],
                    )
                ),
                "vat_amount": settlement["vat_amount"] or 0,
                "settlement_amount": settlement_amount or 0,
                "settlement_status": settlement_status,
                "settlement": settlement,
                "source_url": BAEMIN_ORDER_HISTORY_URL,
                "source_collected_at": collected_at,
                "schema_version": SCHEMA_VERSION,
            }
        )
    return {
        "records": {"sales": sales, "settlements": settlements, "reviews": [], "ads": []},
        "diagnostics": {
            "source": "baemin_order_history_text",
            "schema_version": SCHEMA_VERSION,
            "orders_seen": len(sales),
            "orders_saved": len(sales),
            "detail_failed": detail_failed,
            "settlement_pending": settlement_pending,
        },
    }


async def _body_text(page: Any) -> str:
    try:
        return str(await page.evaluate("document.body ? document.body.innerText : ''") or "")
    except Exception:
        return ""


async def _sleep_jitter(bounds: tuple[float, float]) -> None:
    low, high = bounds
    if high <= 0:
        return
    await page_safe_sleep(random.uniform(max(0.0, low), max(low, high)))


async def page_safe_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


async def _set_history_window(page: Any, date_from: str, date_to: str) -> bool:
    if not date_from and not date_to:
        return False
    try:
        return bool(
            await page.evaluate(
                """
                ({dateFrom, dateTo}) => {
                  const candidates = [...document.querySelectorAll('input')];
                  const setValue = (input, value) => {
                    if (!input || !value) return false;
                    input.focus();
                    input.value = value;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                  };
                  const dateInputs = candidates.filter(input => {
                    const attr = `${input.type || ''} ${input.name || ''} ${input.id || ''} ${input.placeholder || ''}`;
                    return /date|기간|시작|종료|from|to/i.test(attr);
                  });
                  let changed = false;
                  changed = setValue(dateInputs[0], dateFrom) || changed;
                  changed = setValue(dateInputs[1], dateTo) || changed;
                  const buttons = [...document.querySelectorAll('button,[role="button"]')];
                  const search = buttons.find(button => /조회|검색|적용/.test(button.innerText || button.textContent || ''));
                  if (search) search.click();
                  return changed;
                }
                """,
                {"dateFrom": date_from, "dateTo": date_to},
            )
        )
    except Exception:
        return False


async def _expand_visible_order_details(page: Any, limits: BackfillLimits) -> int:
    try:
        count = int(
            await page.evaluate(
                """
                () => {
                  const texts = ['주문 추가 정보', '상세', '더보기', '보기'];
                  const buttons = [...document.querySelectorAll('button,a,[role="button"]')];
                  let clicked = 0;
                  for (const button of buttons) {
                    const text = (button.innerText || button.textContent || '').trim();
                    if (!texts.some(label => text.includes(label))) continue;
                    if (button.disabled || button.getAttribute('aria-disabled') === 'true') continue;
                    button.click();
                    clicked += 1;
                  }
                  return clicked;
                }
                """
            )
        )
    except Exception:
        return 0
    if count:
        await _sleep_jitter(limits.order_detail_jitter)
    return count


async def _scroll_history_page(page: Any, limits: BackfillLimits) -> bool:
    try:
        before = await page.evaluate("document.body ? document.body.innerText.length : 0")
        await page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
        await _sleep_jitter(limits.page_jitter)
        after = await page.evaluate("document.body ? document.body.innerText.length : 0")
        return int(after or 0) > int(before or 0)
    except Exception:
        return False


async def _collect_row_texts(page: Any, max_orders: int) -> list[str]:
    try:
        rows = await page.evaluate(
            """
            ({maxOrders}) => {
              const orderNoPattern = /\\b[A-Z][0-9A-Z]{8,}\\b/;
              const visibleText = el => (el && el.innerText ? el.innerText.trim() : "");
              const nodes = [...document.querySelectorAll('tr,[role="row"],li,section,article,div')];
              const seen = new Set();
              const out = [];
              for (const node of nodes) {
                const text = visibleText(node);
                if (!orderNoPattern.test(text) || text.length < 20) continue;
                const orderNo = text.match(orderNoPattern)[0];
                if (seen.has(orderNo)) continue;
                seen.add(orderNo);
                out.push(text);
                if (out.length >= maxOrders) break;
              }
              return out;
            }
            """,
            {"maxOrders": max_orders},
        )
        return [str(row) for row in rows if str(row or "").strip()] if isinstance(rows, list) else []
    except Exception:
        return []


async def collect_baemin_order_history(
    page: Any,
    account: dict[str, Any],
    date_from: str = "",
    date_to: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect logged-in Baemin order history rows from a Playwright-like page."""
    options = options or {}
    limits = options.get("limits")
    if not isinstance(limits, BackfillLimits):
        limits = BackfillLimits(max_records=int(options.get("max_orders") or options.get("maxOrders") or 300))
    max_orders = min(300, max(1, int(limits.max_records)))
    started = time.monotonic()
    checkpoint = options.get("checkpoint") if isinstance(options.get("checkpoint"), dict) else {}
    try:
        await page.goto(BAEMIN_ORDER_HISTORY_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    except Exception:
        pass
    window_applied = await _set_history_window(page, date_from, date_to)
    detail_clicks = await _expand_visible_order_details(page, limits)
    scroll_pages = 0
    while time.monotonic() - started < limits.max_runtime_seconds and scroll_pages < 3:
        row_texts = await _collect_row_texts(page, max_orders)
        if len(row_texts) >= max_orders:
            break
        if not await _scroll_history_page(page, limits):
            break
        scroll_pages += 1
        detail_clicks += await _expand_visible_order_details(page, limits)
    row_texts = await _collect_row_texts(page, max_orders)
    source_text = "\n\n".join(row_texts) if row_texts else await _body_text(page)
    parsed = parse_baemin_order_history_text(
        source_text,
        str(account.get("business_id") or ""),
        str(account.get("branch") or ""),
    )
    records = _apply_order_checkpoint(parsed.get("records") or {}, checkpoint)
    diagnostics = dict(parsed.get("diagnostics") or {})
    diagnostics.update(
        {
            "source_url": BAEMIN_ORDER_HISTORY_URL,
            "date_from": str(date_from or account.get("_date_from") or ""),
            "date_to": str(date_to or account.get("_date_to") or ""),
            "row_candidates": len(row_texts),
            "window_applied": window_applied,
            "detail_clicks": detail_clicks,
            "scroll_pages": scroll_pages,
            "checkpoint_in": checkpoint,
            "checkpoint_out": {
                "last_order_no": (records.get("sales") or [{}])[-1].get("order_no", "") if records.get("sales") else "",
                "orders_seen": len(records.get("sales") or []),
            },
            "checkpoint_applied": bool(_checkpoint_order_no(checkpoint)),
        }
    )
    total = sum(len(rows) for rows in records.values() if isinstance(rows, list))
    return {
        "status": "succeeded" if total else "partial",
        "error_code": "" if total else "BAEMIN_ORDER_HISTORY_NO_ROWS",
        "records": records,
        "diagnostics": diagnostics,
        "message": "" if total else "배민 주문내역 페이지에서 주문번호를 찾지 못했습니다.",
    }


async def collect_order_history(
    page: Any,
    *,
    business_id: str,
    branch: str,
    date_from: str,
    date_to: str,
    checkpoint: dict[str, Any] | None = None,
    limits: BackfillLimits | None = None,
) -> dict[str, Any]:
    account = {"business_id": business_id, "branch": branch, "_date_from": date_from, "_date_to": date_to}
    return await collect_baemin_order_history(
        page,
        account,
        date_from,
        date_to,
        {"checkpoint": checkpoint or {}, "limits": limits or BackfillLimits()},
    )
