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
import re
from html.parser import HTMLParser
from typing import Any


# ── Work-key generation ──────────────────────────────────────────────────────

def bank_browser_work_key(account_id: str, business_id: str, branch_id: str) -> str:
    """Return a deterministic, opaque Browser Bridge work key.

    All three identifiers are hashed so no human-readable branch name,
    account alias, or account ID appears in the work key.
    """
    raw = f"{account_id}|{business_id}|{branch_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"yeoljeong-bank-browser-{digest}"


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


def _match_header(cell: str, synonym_set: set[str]) -> bool:
    return cell.strip().lower() in {s.lower() for s in synonym_set}


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
    tables = _extract_tables(raw_html)
    diag: dict[str, Any] = {
        "table_count": len(tables),
        "headers_found": [t[0] if t else [] for t in tables[:3]],
        "parse_failure": False,
    }
    for table in tables:
        rows = _parse_table_with_header(table)
        if rows:
            return rows, diag
    if tables:
        diag["parse_failure"] = True
    return [], diag


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
    }

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
            safe_diagnostics["auto_open_error"] = str(exc)[:200]

    if not session_id_to_use:
        return {
            "status": "action_required",
            "error_code": "BANK_BROWSER_SESSION_REQUIRED",
            "rows": [],
            "row_count": 0,
            "diagnostics": safe_diagnostics,
            "message": (
                f"{bank_name or '은행'} 브라우저 수집을 위해 PC Agent 세션이 필요합니다. "
                "관리자 화면에서 은행 웹 수집 세션을 연결하거나 "
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
        page = pages[0] if pages else await context.new_page()

        if portal_url:
            try:
                await page.goto(portal_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

        current_url = ""
        html_content = ""
        try:
            current_url = str(await page.evaluate("window.location.href") or "")
            html_content = str(await page.evaluate("document.body ? document.body.innerHTML : ''") or "")
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

        rows, parse_diag = parse_bank_portal_html_with_diagnostics(html_content)
        if rows and (date_from or date_to):
            rows = [r for r in rows if _row_in_date_range(r, date_from, date_to)]

        safe_diagnostics["parser_table_count"] = parse_diag["table_count"]
        safe_diagnostics["parser_failure"] = parse_diag["parse_failure"]

        if rows:
            msg = f"{bank_name or '은행'} 포털에서 {len(rows)}건 수집했습니다."
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

        if not rows and auto_open_browser and (parse_diag["table_count"] == 0 or parse_diag["parse_failure"]):
            return {
                "status": "action_required",
                "error_code": "BANK_BROWSER_OPERATOR_ACTION_REQUIRED",
                "rows": [],
                "row_count": 0,
                "diagnostics": safe_diagnostics,
                "message": (
                    f"{bank_name or '은행'} 기업페이지를 PC Agent 브라우저로 준비했습니다. "
                    "간편/빠른조회 승인 또는 거래내역 화면 이동 후 다시 수집하십시오."
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
        return {
            "status": "failed",
            "error_code": "BANK_BROWSER_UNEXPECTED_ERROR",
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
