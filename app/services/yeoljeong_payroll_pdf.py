"""급여명세서 합계 계산과 PDF 렌더러 (오비서/열정국밥).

화면(목록 응답의 totals)과 PDF 는 이 모듈의 payroll_totals 하나에서 숫자를 가져온다.
두 곳이 따로 계산하면 합계가 어긋나도 알아챌 수 없다.
글꼴은 계약서 PDF 와 같은 한글 TTF 를 쓴다. 한글 글리프가 없으면 만들지 않는다.
"""
from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

TOTAL_FIELDS = ("gross_pay", "taxable_pay", "non_tax_meal_allowance", "tax_withholding", "insurance_deduction", "other_deduction")


class PayrollPdfError(RuntimeError):
    pass


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def payroll_totals(statement: dict[str, Any]) -> dict[str, Any]:
    """명세 한 건의 금액 합계. net_pay 는 항상 총지급액 - 공제 합계로 다시 계산하고 저장값과의 일치 여부를 함께 낸다."""
    values = {field: _int(statement.get(field)) for field in TOTAL_FIELDS}
    deductions = values["tax_withholding"] + values["insurance_deduction"] + values["other_deduction"]
    net = values["gross_pay"] - deductions
    return {**values, "total_deductions": deductions, "net_pay": net, "consistent": net == _int(statement.get("net_pay"))}


def _won(value: int) -> str:
    return f"{value:,}원"


def payroll_pdf_rows(statement: dict[str, Any]) -> list[tuple[str, str]]:
    """PDF 에 찍히는 (항목, 값) 목록. 테스트와 PDF 가 같은 목록을 본다."""
    totals = payroll_totals(statement)
    return [
        ("정산월", str(statement.get("payroll_month") or "-")),
        ("직원", str(statement.get("employee_name") or "-")),
        ("지점", str(statement.get("branch") or "-")),
        ("지급일", str(statement.get("pay_date") or "-")),
        ("총지급액", _won(totals["gross_pay"])),
        ("과세급여", _won(totals["taxable_pay"])),
        ("비과세 식대", _won(totals["non_tax_meal_allowance"])),
        ("원천세", _won(totals["tax_withholding"])),
        ("4대보험 공제", _won(totals["insurance_deduction"])),
        ("기타 공제", _won(totals["other_deduction"])),
        ("공제 합계", _won(totals["total_deductions"])),
        ("실수령액", _won(totals["net_pay"])),
    ]


def render_payroll_statement_pdf(statement: dict[str, Any]) -> bytes:
    """확정 이상의 명세만 PDF 로 만든다. 같은 입력이면 같은 바이트가 나온다."""
    status = str(statement.get("status") or "").strip().lower()
    if status not in {"confirmed", "paid"}:
        raise PayrollPdfError("확정된 급여명세만 PDF 로 만들 수 있습니다")
    if not payroll_totals(statement)["consistent"]:
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
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=12, textColor=colors.HexColor("#64748b"))

    def para(text: str, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(escape(text), style)

    rows = payroll_pdf_rows(statement)
    table = Table([[para(name), para(value)] for name, value in rows], colWidths=[50 * mm, 120 * mm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
        title="급여명세서", invariant=1,
    )
    footer = f"명세 ID {statement.get('id') or '-'} · 버전 {statement.get('version') or 1} · 상태 {status}"
    document.build([para("급여명세서", title), table, Spacer(1, 8 * mm), para(footer, small)])
    return buffer.getvalue()
