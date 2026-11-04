"""급여명세서 합계 계산과 PDF 렌더러 (오비서/열정국밥).

화면(목록 응답의 totals)과 PDF 는 이 모듈의 payroll_totals 하나에서 숫자를 가져온다.
두 곳이 따로 계산하면 합계가 어긋나도 알아챌 수 없다.
글꼴은 계약서 PDF 와 같은 한글 TTF 를 쓴다. 한글 글리프가 없으면 만들지 않는다.

양식은 둘이다. 근로자(employment_tax_type != freelancer_33)는 '임금명세서'(근로기준법 제48조 제2항,
시행령 제27조의2), 프리랜서(freelancer_33)는 '용역비 지급명세서'. 선택 필드 pay_items·deduction_items·
work_summary 가 없는 과거 명세는 기존 합계 필드만으로 그대로 읽힌다.
"""
from __future__ import annotations

import io
import re
from typing import Any
from xml.sax.saxutils import escape

from app.services.yeoljeong_payroll_deductions import four_insurance_deduction_errors

TOTAL_FIELDS = ("gross_pay", "taxable_pay", "non_tax_meal_allowance", "tax_withholding", "insurance_deduction", "other_deduction")

FREELANCER_TAX_TYPE = "freelancer_33"

PAY_ITEM_CODES = {
    "base": "기본급", "overtime": "연장근로수당", "night": "야간근로수당", "holiday": "휴일근로수당",
    "weekly_holiday": "주휴수당", "meal": "식대", "bonus": "상여금", "other": "기타수당",
}
INSURANCE_DEDUCTION_CODES = ("national_pension", "health_insurance", "long_term_care", "employment_insurance")
TAX_DEDUCTION_CODES = ("income_tax", "local_income_tax")
DEDUCTION_ITEM_CODES = {
    "national_pension": "국민연금", "health_insurance": "건강보험", "long_term_care": "노인장기요양보험",
    "employment_insurance": "고용보험", "income_tax": "소득세", "local_income_tax": "지방소득세", "other": "기타 공제",
}
WORK_SUMMARY_FIELDS = ("work_days", "work_minutes", "overtime_minutes", "night_minutes", "holiday_minutes")
_MINUTE_FIELDS = WORK_SUMMARY_FIELDS[1:]
_ITEM_AMOUNT_LIMIT = 10**12
_MAX_ITEMS = 30
_MAX_MONTH_MINUTES = 31 * 24 * 60
_RRN_PATTERN = re.compile(r"\d{6}\s*[-–]?\s*[1-8]\d{6}")


class PayrollPdfError(RuntimeError):
    pass


class PayrollItemError(ValueError):
    """항목 입력이 형식·합계 규칙에 맞지 않을 때. 메시지는 그대로 한글 사유로 노출된다."""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def freelancer_withholding(gross_pay: Any) -> dict[str, int]:
    """프리랜서 3.3% 원천징수. 소득세 3%(10원 미만 절사) + 지방소득세 = 소득세의 10%(10원 미만 절사)."""
    gross = max(_int(gross_pay), 0)
    income_tax = gross * 3 // 100 // 10 * 10
    local_income_tax = income_tax // 10 // 10 * 10
    return {"income_tax": income_tax, "local_income_tax": local_income_tax, "total": income_tax + local_income_tax}


def is_freelancer(statement: dict[str, Any]) -> bool:
    return str(statement.get("employment_tax_type") or "").strip() == FREELANCER_TAX_TYPE


def _strict_amount(value: Any, where: str) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PayrollItemError(f"{where} 금액을 입력하십시오")
    if isinstance(value, bool) or isinstance(value, float):
        raise PayrollItemError(f"{where} 금액은 원 단위 정수여야 합니다")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        parsed = int(value.strip())
    else:
        raise PayrollItemError(f"{where} 금액은 원 단위 정수여야 합니다")
    if parsed < 0 or parsed > _ITEM_AMOUNT_LIMIT:
        raise PayrollItemError(f"{where} 금액은 0원 이상 범위 내 정수여야 합니다")
    return parsed


def _flag(value: Any) -> bool:
    """명시적 참(True/'true'/1)만 참. 거짓·빈 값은 키 자체를 남기지 않기 위해 호출부가 False 를 버린다."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "y", "yes"}
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)


def _clean_item_list(
    raw: Any, codes: dict[str, str], kind: str, *, with_note: bool, with_flags: bool = False
) -> list[dict[str, Any]] | None:
    if raw is None or raw == [] or raw == "":
        return None
    if not isinstance(raw, list):
        raise PayrollItemError(f"{kind}은 목록이어야 합니다")
    if len(raw) > _MAX_ITEMS:
        raise PayrollItemError(f"{kind}은 {_MAX_ITEMS}개를 넘을 수 없습니다")
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        where = f"{kind} {index}번"
        if not isinstance(item, dict):
            raise PayrollItemError(f"{where} 형식이 올바르지 않습니다")
        code = str(item.get("code") or "").strip()
        if code not in codes:
            raise PayrollItemError(f"{where} 구분(code)이 올바르지 않습니다")
        label = str(item.get("label") or "").strip() or codes[code]
        if _RRN_PATTERN.search(label):
            raise PayrollItemError(f"{where} 항목명에 주민등록번호 형식의 값을 입력할 수 없습니다")
        if len(label) > 40:
            raise PayrollItemError(f"{where} 항목명은 40자 이내로 입력하십시오")
        entry: dict[str, Any] = {"code": code, "label": label, "amount": _strict_amount(item.get("amount"), where)}
        if with_note:
            note = str(item.get("calc_note") or "").strip()
            if _RRN_PATTERN.search(note):
                raise PayrollItemError(f"{where} 계산방법에 주민등록번호 형식의 값을 입력할 수 없습니다")
            if len(note) > 200:
                raise PayrollItemError(f"{where} 계산방법은 200자 이내로 입력하십시오")
            entry["calc_note"] = note
        if with_flags:
            # 자동 산정 출처·입력 대기·0원 확인 표시. 이 값이 정규화에서 버려지면 관리자가 확인한 항목이
            # 다음 저장에서 다시 입력 대기로 돌아간다. 거짓 값은 키를 남기지 않는다.
            note = str(item.get("calc_note") or "").strip()
            if _RRN_PATTERN.search(note):
                raise PayrollItemError(f"{where} 계산방법에 주민등록번호 형식의 값을 입력할 수 없습니다")
            if note:
                entry["calc_note"] = note[:200]
            source = str(item.get("source") or "").strip().lower()
            if source in {"auto", "manual"}:
                entry["source"] = source
            confirmed = _flag(item.get("confirmed"))
            if confirmed:
                entry["confirmed"] = True
            if _flag(item.get("needs_input")) and not confirmed:
                entry["needs_input"] = True
        cleaned.append(entry)
    return cleaned


def _clean_work_summary(raw: Any) -> dict[str, int] | None:
    if raw is None or raw == {} or raw == "":
        return None
    if not isinstance(raw, dict):
        raise PayrollItemError("근로 현황(work_summary)은 객체여야 합니다")
    unknown = sorted(set(raw) - set(WORK_SUMMARY_FIELDS))
    if unknown:
        raise PayrollItemError(f"근로 현황에 알 수 없는 항목이 있습니다: {', '.join(unknown)}")
    cleaned: dict[str, int] = {}
    for field in WORK_SUMMARY_FIELDS:
        value = raw.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        cleaned[field] = _strict_amount(value, f"근로 현황 {field}")
    if cleaned.get("work_days", 0) > 31:
        raise PayrollItemError("근로일수는 31일을 넘을 수 없습니다")
    for field in _MINUTE_FIELDS:
        if cleaned.get(field, 0) > _MAX_MONTH_MINUTES:
            raise PayrollItemError(f"{field} 는 한 달 시간을 넘을 수 없습니다")
    total = cleaned.get("work_minutes")
    if total is not None:
        for field in _MINUTE_FIELDS[1:]:
            if cleaned.get(field, 0) > total:
                raise PayrollItemError("연장·야간·휴일 시간은 총 근로시간을 넘을 수 없습니다")
    return cleaned or None


def normalize_payroll_items(statement: dict[str, Any]) -> dict[str, Any]:
    """pay_items·deduction_items·work_summary 를 검증해 정리한 사본. 비어 있거나 없는 항목은 결과에서 빠진다."""
    result: dict[str, Any] = {}
    pay = _clean_item_list(statement.get("pay_items"), PAY_ITEM_CODES, "지급 항목", with_note=True)
    deduction = _clean_item_list(statement.get("deduction_items"), DEDUCTION_ITEM_CODES, "공제 항목", with_note=False, with_flags=True)
    work = _clean_work_summary(statement.get("work_summary"))
    if pay:
        result["pay_items"] = pay
    if deduction:
        result["deduction_items"] = deduction
    if work:
        result["work_summary"] = work
    return result


def _group_sum(items: list[dict[str, Any]], codes: tuple[str, ...]) -> int:
    return sum(item["amount"] for item in items if item["code"] in codes)


def payroll_items_errors(statement: dict[str, Any]) -> list[str]:
    """항목 합계와 기존 합계 필드가 어긋나는 사유(한글). 항목이 없는 과거 명세는 빈 목록."""
    try:
        items = normalize_payroll_items(statement)
    except PayrollItemError as exc:
        return [str(exc)]
    errors: list[str] = []
    gross = _int(statement.get("gross_pay"))
    pay = items.get("pay_items")
    if pay:
        total = sum(item["amount"] for item in pay)
        if total != gross:
            errors.append(f"지급 항목 합계({total:,}원)가 총지급액({gross:,}원)과 일치해야 합니다")
    deduction = items.get("deduction_items")
    if deduction:
        for label, codes, field in (
            ("소득세·지방소득세", TAX_DEDUCTION_CODES, "tax_withholding"),
            ("4대보험", INSURANCE_DEDUCTION_CODES, "insurance_deduction"),
            ("기타 공제", ("other",), "other_deduction"),
        ):
            total, expected = _group_sum(deduction, codes), _int(statement.get(field))
            if total != expected:
                errors.append(f"공제 항목의 {label} 합계({total:,}원)가 {field}({expected:,}원)와 일치해야 합니다")
    if is_freelancer(statement):
        if _int(statement.get("insurance_deduction")) or (deduction and _group_sum(deduction, INSURANCE_DEDUCTION_CODES)):
            errors.append("프리랜서(3.3%) 명세에는 4대보험 공제를 입력할 수 없습니다")
    return errors


def is_legacy_import_statement(statement: dict[str, Any]) -> bool:
    """과거 일괄 등록 명세(source_kind 가 import 계열이거나 import_task 가 있음). 필수 기재 검증 대상에서 빠진다.

    표식은 서버가 붙인 최상위 값만 본다. 중첩 statement_payload 는 클라이언트가 채울 수 있어 믿지 않는다."""
    return "import" in str(statement.get("source_kind") or "").strip().lower() or bool(statement.get("import_task"))


def wage_statement_required_errors(statement: dict[str, Any]) -> list[str]:
    """근로자 임금명세서 확정 전 필수 기재사항(임금지급일·구성항목·공제 상세·연장/야간/휴일 계산방법) 누락 사유(한글).

    프리랜서 명세와 과거 일괄 등록 명세는 대상이 아니다."""
    if is_freelancer(statement) or is_legacy_import_statement(statement):
        return []
    try:
        items = normalize_payroll_items(statement)
    except PayrollItemError as exc:
        return [str(exc)]
    errors: list[str] = []
    pay = items.get("pay_items") or []
    if not pay:
        errors.append("임금명세서 필수 기재사항(구성항목별 금액)이 비어 있습니다")
    deductions = sum(_int(statement.get(field)) for field in ("tax_withholding", "insurance_deduction", "other_deduction"))
    if deductions and not items.get("deduction_items"):
        errors.append("임금명세서 필수 기재사항(공제 항목별 금액)이 비어 있습니다")
    work = items.get("work_summary") or {}
    for field, code, label in (
        ("overtime_minutes", "overtime", "연장근로"), ("night_minutes", "night", "야간근로"), ("holiday_minutes", "holiday", "휴일근로"),
    ):
        coded = [item for item in pay if item["code"] == code]
        if (work.get(field, 0) > 0 or coded) and not (coded and all(item.get("calc_note") for item in coded)):
            errors.append(f"임금명세서 필수 기재사항({label}수당 계산방법)이 비어 있습니다")
    if not _text(statement.get("pay_date"), ""):
        errors.append("임금명세서 필수 기재사항(임금지급일)이 비어 있습니다")
    # 4대보험 근로자: 6개 공제 code 가 모두 있고 입력 대기가 없어야 한다. 다른 필수 기재사항 사유보다 뒤에 둔다.
    if str(statement.get("employment_tax_type") or "").strip() == "four_insurance":
        errors.extend(four_insurance_deduction_errors(items.get("deduction_items")))
    return errors


def payroll_totals(statement: dict[str, Any]) -> dict[str, Any]:
    """명세 한 건의 금액 합계. net_pay 는 항상 총지급액 - 공제 합계로 다시 계산하고 저장값과의 일치 여부를 함께 낸다."""
    values = {field: _int(statement.get(field)) for field in TOTAL_FIELDS}
    deductions = values["tax_withholding"] + values["insurance_deduction"] + values["other_deduction"]
    net = values["gross_pay"] - deductions
    item_errors = payroll_items_errors(statement)
    return {
        **values, "total_deductions": deductions, "net_pay": net,
        "consistent": net == _int(statement.get("net_pay")) and not item_errors, "item_errors": item_errors,
    }


def _won(value: int) -> str:
    return f"{value:,}원"


def _text(value: Any, fallback: str = "-") -> str:
    """PDF 에 찍는 문자열. 주민등록번호 모양은 어떤 필드에 들어 있어도 출력하지 않는다."""
    text = _RRN_PATTERN.sub("", str(value or "")).strip()
    return text or fallback


def _duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours}시간 {rest}분" if rest else f"{hours}시간"


def _birth_date(statement: dict[str, Any]) -> str:
    value = str(statement.get("employee_birth_date") or "").strip()
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else ""


def _pay_lines(statement: dict[str, Any], totals: dict[str, Any]) -> list[tuple[str, str, str]]:
    pay = normalize_payroll_items(statement).get("pay_items")
    if pay:
        return [(_text(item["label"]), _won(item["amount"]), _text(item["calc_note"])) for item in pay]
    taxable, meal = totals["taxable_pay"], totals["non_tax_meal_allowance"]
    if (taxable or meal) and taxable + meal == totals["gross_pay"]:
        return [("과세급여", _won(taxable), "-"), ("비과세 식대", _won(meal), "-")]
    return [("지급액", _won(totals["gross_pay"]), "-")]


def _work_rows(statement: dict[str, Any]) -> list[tuple[str, ...]]:
    work = normalize_payroll_items(statement).get("work_summary") or {}
    rows: list[tuple[str, ...]] = []
    if "work_days" in work:
        rows.append(("근로일수", f"{work['work_days']}일"))
    for field, label in (("work_minutes", "총 근로시간"), ("overtime_minutes", "연장근로시간"),
                         ("night_minutes", "야간근로시간"), ("holiday_minutes", "휴일근로시간")):
        if field in work:
            rows.append((label, _duration(work[field])))
    return rows or [("근로일수·근로시간", "기재 없음")]


def _wage_sections(statement: dict[str, Any]) -> list[dict[str, Any]]:
    totals = payroll_totals(statement)
    identity: list[tuple[str, ...]] = [
        ("정산월", _text(statement.get("payroll_month"))),
        ("성명", _text(statement.get("employee_name"))),
        ("사원번호", _text(statement.get("employee_no") or statement.get("employee_request_id"))),
    ]
    if _birth_date(statement):
        identity.append(("생년월일", _birth_date(statement)))
    identity += [("지점", _text(statement.get("branch"))), ("임금지급일", _text(statement.get("pay_date")))]
    deduction_items = normalize_payroll_items(statement).get("deduction_items")
    if deduction_items:
        deductions: list[tuple[str, ...]] = [(_text(item["label"]), _won(item["amount"])) for item in deduction_items]
    else:
        deductions = [
            ("원천세", _won(totals["tax_withholding"])),
            ("4대보험 공제", _won(totals["insurance_deduction"])),
            ("기타 공제", _won(totals["other_deduction"])),
        ]
    deductions.append(("공제 합계", _won(totals["total_deductions"])))
    return [
        {"title": None, "header": None, "rows": identity},
        {"title": "임금 지급 내역", "header": ("지급 항목", "금액", "계산방법"),
         "rows": [*_pay_lines(statement, totals), ("임금 총액", _won(totals["gross_pay"]), "-")]},
        {"title": "근로일수 및 근로시간", "header": None, "rows": _work_rows(statement)},
        {"title": "공제 내역", "header": ("공제 항목", "금액"), "rows": deductions},
        {"title": None, "header": None, "rows": [("실지급액", _won(totals["net_pay"]))]},
    ]


def _freelancer_tax_amounts(statement: dict[str, Any], totals: dict[str, Any]) -> tuple[int | None, int | None]:
    """(사업소득세, 지방소득세). 항목이 없고 합계도 3.3% 규칙값과 다르면 나눌 근거가 없으므로 (None, None)."""
    deduction = normalize_payroll_items(statement).get("deduction_items")
    if deduction:
        return _group_sum(deduction, ("income_tax",)), _group_sum(deduction, ("local_income_tax",))
    computed = freelancer_withholding(totals["gross_pay"])
    if computed["total"] == totals["tax_withholding"]:
        return computed["income_tax"], computed["local_income_tax"]
    return None, None


def _freelancer_sections(statement: dict[str, Any]) -> list[dict[str, Any]]:
    totals = payroll_totals(statement)
    basis = statement.get("settlement_basis") if isinstance(statement.get("settlement_basis"), dict) else {}
    start, end = _text(basis.get("period_start"), ""), _text(basis.get("period_end"), "")
    income_tax, local_tax = _freelancer_tax_amounts(statement, totals)
    if income_tax is None:
        tax_rows: list[tuple[str, ...]] = [("원천징수세액(소득세+지방소득세)", _won(totals["tax_withholding"]))]
    else:
        tax_rows = [("사업소득세(3%)", _won(income_tax)), ("지방소득세(0.3%)", _won(local_tax or 0))]
    other_items = [
        item for item in normalize_payroll_items(statement).get("deduction_items") or [] if item["code"] == "other"
    ]
    if other_items:
        tax_rows += [(_text(item["label"]), _won(item["amount"])) for item in other_items]
    elif totals["other_deduction"]:
        tax_rows.append(("기타 공제", _won(totals["other_deduction"])))
    tax_rows.append(("공제 합계", _won(totals["total_deductions"])))
    return [
        {"title": None, "header": None, "rows": [
            ("정산월", _text(statement.get("payroll_month"))),
            ("성명", _text(statement.get("employee_name"))),
            ("지점", _text(statement.get("branch"))),
            ("지급일", _text(statement.get("pay_date"))),
            ("용역계약 ID", _text(statement.get("source_contract_id") or basis.get("contract_id"))),
            ("용역기간", f"{start} ~ {end}" if start or end else "-"),
            ("업무범위", _text(basis.get("scope"))),
        ]},
        {"title": None, "header": None, "rows": [("지급 용역비", _won(totals["gross_pay"]))]},
        {"title": "원천징수 내역", "header": ("항목", "금액"), "rows": tax_rows},
        {"title": None, "header": None, "rows": [("실지급액", _won(totals["net_pay"]))]},
    ]


def payroll_form(statement: dict[str, Any]) -> str:
    return "freelancer" if is_freelancer(statement) else "wage"


def payroll_form_title(statement: dict[str, Any]) -> str:
    return "용역비 지급명세서" if is_freelancer(statement) else "임금명세서"


def _sections(statement: dict[str, Any]) -> list[dict[str, Any]]:
    return _freelancer_sections(statement) if is_freelancer(statement) else _wage_sections(statement)


def _flatten(sections: list[dict[str, Any]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for section in sections:
        for row in section["rows"]:
            rows.append((row[0], row[1]))
            if len(row) > 2 and row[2] not in ("", "-"):
                rows.append((f"계산방법 · {row[0]}", row[2]))
    return rows


def wage_statement_rows(statement: dict[str, Any]) -> list[tuple[str, str]]:
    """근로자 '임금명세서'에 찍히는 (항목, 값). 지급 항목의 계산방법은 (계산방법 · 항목명, 내용) 행으로 따로 나온다."""
    return _flatten(_wage_sections(statement))


def freelancer_statement_rows(statement: dict[str, Any]) -> list[tuple[str, str]]:
    """프리랜서 '용역비 지급명세서'에 찍히는 (항목, 값). '급여'·'4대보험' 문구는 쓰지 않는다."""
    return _flatten(_freelancer_sections(statement))


def payroll_pdf_rows(statement: dict[str, Any]) -> list[tuple[str, str]]:
    """PDF 에 찍히는 (항목, 값) 목록. 양식은 employment_tax_type 으로 정한다. 테스트와 PDF 가 같은 목록을 본다."""
    return _flatten(_sections(statement))


def render_payroll_statement_pdf(statement: dict[str, Any]) -> bytes:
    """확정 이상의 명세만 PDF 로 만든다. 같은 입력이면 같은 바이트가 나온다."""
    status = str(statement.get("status") or "").strip().lower()
    if status not in {"confirmed", "paid"}:
        raise PayrollPdfError("확정된 급여명세만 PDF 로 만들 수 있습니다")
    totals = payroll_totals(statement)
    if totals["item_errors"]:
        raise PayrollPdfError(f"항목과 합계가 달라 PDF 를 만들지 않습니다: {totals['item_errors'][0]}")
    if not totals["consistent"]:
        raise PayrollPdfError("실수령액이 총지급액-공제 합계와 달라 PDF 를 만들지 않습니다")
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        raise PayrollPdfError("reportlab 이 설치되어 있지 않습니다") from None
    from app.services import yeoljeong_contract_pdf as contract_pdf

    try:
        font = contract_pdf._register_font()
    except contract_pdf.ContractPdfError as exc:
        raise PayrollPdfError(str(exc)) from None
    base = ParagraphStyle("body", fontName=font, fontSize=10, leading=15, wordWrap="CJK")
    title = ParagraphStyle("title", parent=base, fontSize=16, leading=24, alignment=1, spaceAfter=10)
    heading = ParagraphStyle("heading", parent=base, fontSize=11, leading=16, spaceBefore=8, spaceAfter=4)
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=12, textColor=colors.HexColor("#64748b"))

    def para(text: str, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(escape(text), style)

    form_title = payroll_form_title(statement)
    story: list[Any] = [para(form_title, title)]
    for section in _sections(statement):
        if section["title"]:
            story.append(para(section["title"], heading))
        width = len(section["header"]) if section["header"] else max(len(row) for row in section["rows"])
        data = [[para(cell) for cell in row] for row in ([section["header"]] if section["header"] else [])]
        data += [[para(cell) for cell in row] + [para("") for _ in range(width - len(row))] for row in section["rows"]]
        widths = {2: [50 * mm, 120 * mm], 3: [45 * mm, 40 * mm, 85 * mm]}[width]
        table = Table(data, colWidths=widths)
        style = [
            ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")) if section["header"]
                     else ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")))
        table.setStyle(TableStyle(style))
        story += [table, Spacer(1, 3 * mm)]
    basis_note = (
        "사업소득 원천징수(소득세 3%, 지방소득세는 소득세의 10%, 각 10원 미만 절사) 후 지급한 용역비 내역입니다."
        if is_freelancer(statement)
        else "근로기준법 제48조 제2항, 같은 법 시행령 제27조의2에 따라 교부하는 임금명세서입니다."
    )
    footer = f"명세 ID {statement.get('id') or '-'} · 버전 {statement.get('version') or 1} · 상태 {status}"
    story += [Spacer(1, 4 * mm), para(basis_note, small), para(footer, small)]
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
        title=form_title, invariant=1,
    )
    document.build(story)
    return buffer.getvalue()
