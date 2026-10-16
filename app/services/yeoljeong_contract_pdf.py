"""서명 완료 계약서 PDF 렌더러 (오비서/열정국밥).

원본은 계약서의 ``signed_snapshot`` 하나뿐이다. 서명 뒤 계약서 레코드가 바뀌어도
PDF 는 봉인 시점 내용이어야 하므로, 렌더링 전에 스냅샷 해시를 다시 계산해
``signed_snapshot_sha256`` 과 대조하고 어긋나면 만들지 않는다.

라이브러리는 reportlab(순수 파이썬)만 쓴다. 진아서버 venv 에는 OS 패키지를
설치할 수 없으므로 weasyprint·wkhtmltopdf·chromium 계열은 쓰지 않는다.
한글 글리프가 없는 폰트로 그리면 네모 칸만 남은 "빈칸 PDF" 가 나오므로,
Hangul 글리프를 가진 TTF 를 찾지 못하면 실패로 처리한다.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from app.services import yeoljeong_company_seal as company_seal
from app.services.obys_contract_clauses import (
    CONTRACT_TITLES, contract_clauses, korean_date, money, value as clause_value,
)

logger = logging.getLogger(__name__)

FONT_ENV = "OBYS_CONTRACT_PDF_FONT_PATH"
FONT_NAME = "ObysContractKR"
BUNDLED_FONT = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NanumGothic-Regular.ttf"
SEAL_FONT_FILES = (
    ("ObysSealHangul", BUNDLED_FONT.with_name("ObysSealHangul-Cafe24OhsquareAir-subset.ttf")),
    ("ObysSealHanja", BUNDLED_FONT.with_name("ObysSealHanja-ZenAntique-subset.ttf")),
)
SYSTEM_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf"),
)
# 한글 검증용 코드포인트: '가', '계', '약'
_HANGUL_PROBE = (0xAC00, 0xACC4, 0xC57D)

CONTRACT_TYPE_LABELS = {
    "part_time": "단시간(아르바이트) 근로계약",
    "regular": "정규직 근로계약",
    "manager": "관리자 근로계약",
    "freelancer": "3.3% 프리랜서 용역계약",
    "confidentiality": "보안 및 개인정보 보호 서약",
}

# (키, 라벨) — 계약서 본문에 표시할 순서. 목록에 없는 키는 "기타 기재사항"에 EXTRA_LABELS 이름으로 표시하고, 매핑 없는 영문 스네이크 키는 인쇄하지 않는다.
PARTY_FIELDS = (
    ("employer_name", "상호"),
    ("employer_representative", "대표자"),
    ("employer_registration_no", "사업자등록번호"),
    ("employer_address", "사업장 주소"),
    ("employer_phone", "사업장 연락처"),
)
EMPLOYEE_FIELDS = (
    ("employee_name", "성명"),
    ("employee_birth_date", "생년월일"),
    ("employee_address", "주소"),
    ("employee_nationality", "국적"),
    ("visa_status", "체류자격"),
    ("foreign_registration_no_masked", "외국인등록번호"),
)
TERM_FIELDS = (
    ("start_date", "근로(용역) 개시일"),
    ("end_date", "종료일"),
    ("workplace", "근무장소"),
    ("job_description", "업무내용"),
    ("work_time", "근무시간"),
    ("rest_time", "휴게시간"),
    ("weekly_hours", "주 소정근로시간"),
    ("work_days", "근무일/요일"),
    ("daily_work_schedule", "근로일별 근로시간"),
    ("holidays", "휴일/주휴"),
    ("wage_type", "임금 산정 방식"),
    ("wage", "임금(용역비)"),
    ("base_salary", "기본급"),
    ("non_tax_meal_allowance", "비과세 식대"),
    ("taxable_allowance", "기타 과세수당"),
    ("wage_composition", "임금 구성/공제"),
    ("pay_date", "급여지급일"),
    ("pay_method", "지급방법"),
    ("bank_name", "지급 은행"),
    ("bank_account_holder", "예금주"),
    ("bank_account_number", "계좌번호"),
    ("bank_account_masked", "계좌번호"),
    ("overtime_terms", "연장·야간·휴일근로"),
    ("leave_terms", "연차/휴가/결근"),
    ("insurance_terms", "4대보험/세무 처리"),
    ("probation_terms", "수습"),
    ("freelancer_scope", "용역 업무범위/산출물"),
    ("freelancer_settlement_terms", "용역비 정산/해지"),
    ("special_terms", "특약사항"),
)
# 3.3% 용역계약(freelancer) 전용 칸 이름. 키는 TERM_FIELDS 와 같고 표시 이름만 바꾼다.
# 근로계약 서식은 TERM_FIELDS 를 그대로 쓰므로 영향이 없다. 봉인 스냅샷에는 손대지 않는다.
FREELANCER_TERM_LABELS = {
    "start_date": "용역 개시일",
    "workplace": "수행 장소",
    "work_time": "수행 시간",
    "rest_time": "휴식",
    "weekly_hours": "수행 회차 기준",
    "work_days": "수행 일정",
    "daily_work_schedule": "회차별 수행 일시",
    "holidays": "휴일 규정",
    "wage_type": "용역비 산정 방식",
    "wage": "용역비",
    "wage_composition": "용역비 구성/원천징수",
    "pay_date": "용역비 지급일",
    "overtime_terms": "추가 수행",
    "leave_terms": "휴가 규정",
    "insurance_terms": "세무 처리",
}
# 용역계약에서는 의미가 없어 인쇄하지 않는 키(수습 = 해당 없음).
FREELANCER_SKIPPED_KEYS = {"probation_terms", "probation_period", "workplace_size_category", "meal_provision"}
# 값이 0 이면 인쇄하지 않는 금액 키(용역계약 한정).
FREELANCER_ZERO_HIDDEN_KEYS = {"base_salary", "non_tax_meal_allowance", "taxable_allowance"}
# 기타 기재사항에서 원문 키 대신 보여줄 이름(용역·근로 공통, 칸 이름만 바꾸고 값은 건드리지 않는다).
EXTRA_LABELS = {
    "employment_tax_type": "세무 처리 구분",
    "terms": "추가 특약",
    "termination_terms": "계약 해지 및 기성 정산",
    "confidentiality_terms": "비밀유지 및 자료보호",
    "foreign_worker": "외국인 근로 여부",
    "minor_guardian_consent": "미성년자 보호자 동의",
    "meal_uniform_terms": "식사·복장",
    "workplace_size_category": "사업장 상시근로자 규모",
    "meal_provision": "식사 제공 방식",
    "probation_period": "수습 기간",
}
# 영문 소문자_스네이크 내부 키. EXTRA_LABELS 에 없으면 PDF 에 찍지 않는다.
_INTERNAL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# 내부 코드값 → 한글. 오비서 화면(index.html wageTypeLabels 등)과 같은 문구를 쓴다.
CODE_VALUE_LABELS = {
    "wage_type": {"hourly": "시급", "monthly": "월급", "daily": "일급", "case_fee": "건별 용역비"},
    "employment_tax_type": {
        "four_insurance": "4대보험 가입 근로자",
        "freelancer_33": "3.3% 프리랜서 원천징수",
    },
    "workplace_size_category": {"under_5": "상시 5인 미만", "five_plus": "상시 5인 이상"},
    "minor_guardian_consent": {"confirmed": "동의 확인"},
    "meal_provision": {"employer_meal": "사용자 식사 제공", "cash_no_meal": "식사 미제공 · 현금 식대"},
}
FREELANCER_DOCUMENT_KIND = "freelancer_service_contract"
# PDF 에 싣지 않는 키: 내부 식별자·감사 원문·서명 증적(별도 섹션에 표시)
HIDDEN_FIELDS = {
    "id",
    "tenant_id",
    "business_id",
    "employee_request_id",
    "employee_email",
    "employee_phone",
    "created_at",
    "created_by",
    "requested_by",
    "requested_at",
    "status",
    "signed_at",
    "signer_name",
    "signer_email",
    "signature_sha256",
    "signature_consent",
    "signature_audit",
    "document_kind",
    "template_version",
    "print_title",
    "contract_type",
    "employee_email_masked",
    "branch",
    "contract_date",
    "memo",
    "updated_at",
    "deleted_at",
    "onboarding_document_summary",
    "health_certificate_valid_until",
}


class ContractPdfError(RuntimeError):
    """PDF 를 만들 수 없음 — 서명은 유지하고 실패만 기록한다."""


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    """``_signed_contract_snapshot`` 과 같은 직렬화 규칙으로 해시를 다시 계산한다."""
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_font_path() -> Path:
    configured = str(os.getenv(FONT_ENV) or "").strip()
    candidates = [Path(configured)] if configured else [BUNDLED_FONT, *SYSTEM_FONT_CANDIDATES]
    for path in candidates:
        if path.is_file():
            return path
    if configured:
        raise ContractPdfError(f"CJK 폰트 파일이 없습니다: {FONT_ENV}={configured}")
    raise ContractPdfError("CJK 폰트 파일을 찾지 못했습니다 — 한글이 깨진 PDF 를 만들지 않습니다")


def _register_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont, TTFError

    path = resolve_font_path()
    try:
        font = TTFont(FONT_NAME, str(path))
    except (TTFError, OSError) as exc:
        raise ContractPdfError(f"CJK 폰트를 읽을 수 없습니다: {path.name}: {exc}") from None
    glyphs = getattr(font.face, "charToGlyph", {}) or {}
    if not all(glyphs.get(code) for code in _HANGUL_PROBE):
        raise ContractPdfError(f"폰트에 한글 글리프가 없습니다: {path.name}")
    pdfmetrics.registerFont(font)
    bold_path = BUNDLED_FONT.with_name("NanumGothic-Bold.ttf")
    bold_name = FONT_NAME
    if bold_path.is_file():
        bold_name = FONT_NAME + "Bold"
        pdfmetrics.registerFont(TTFont(bold_name, str(bold_path)))
    pdfmetrics.registerFontFamily(FONT_NAME, normal=FONT_NAME, bold=bold_name)
    return FONT_NAME


def _seal_font_picker(base_font: str) -> Callable[[str], str]:
    """도장 글자마다 그 글자를 가진 첫 글꼴을 고른다: 도장 한글(Cafe24 Ohsquare Air) → 도장 한자(Zen Antique) → 본문 글꼴. 파일이 없으면 본문 글꼴만 쓴다."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFError, TTFont

    chain: list[tuple[str, Any]] = []
    for name, path in SEAL_FONT_FILES:
        try:
            font = TTFont(name, str(path))
            pdfmetrics.registerFont(font)
        except (TTFError, OSError) as exc:
            logger.warning("도장 글꼴을 쓰지 못해 본문 글꼴로 그립니다: %s: %s", path.name, exc)
            continue
        chain.append((name, font.face.charToGlyph))

    def pick(ch: str) -> str:
        code = ord(ch)
        for name, glyphs in chain:
            if glyphs.get(code):
                return name
        return base_font

    return pick


def _signature_png(signature_data_uri: str, expected_sha256: str) -> bytes:
    prefix = "data:image/png;base64,"
    value = str(signature_data_uri or "")
    if not value.startswith(prefix):
        raise ContractPdfError("자필서명 이미지가 없습니다")
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
    except ValueError:
        raise ContractPdfError("자필서명 이미지를 해석할 수 없습니다") from None
    if not expected_sha256 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ContractPdfError("자필서명 이미지가 봉인된 서명 해시와 일치하지 않습니다")
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(raw)) as probe:
            probe.verify()
    except Exception:
        raise ContractPdfError("자필서명 PNG 를 읽을 수 없습니다") from None
    return raw


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (list, tuple)):
        return "\n".join(_text(item) for item in value if _text(item))
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_text(item)}" for key, item in value.items() if _text(item))
    return str(value).strip()


def _mask_email(email: str) -> str:
    email = str(email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return f"{local[:2]}{'*' * max(1, len(local) - 2)}@{domain}"


def is_freelancer_snapshot(snapshot: dict[str, Any]) -> bool:
    return (
        str(snapshot.get("contract_type") or "") == "freelancer"
        or str(snapshot.get("document_kind") or "") == FREELANCER_DOCUMENT_KIND
    )


def display_value(key: str, value: Any) -> Any:
    """내부 코드값(case_fee 등)을 한글로 바꾼다. 모르는 값은 원문 그대로 둔다."""
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, str):
        mapping = CODE_VALUE_LABELS.get(key)
        if mapping and value.strip() in mapping:
            return mapping[value.strip()]
        if value.strip() == "not_applicable":
            return "해당 없음"
    return value


def _is_zero_amount(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip()) == 0
        except ValueError:
            return False
    return False


def term_rows(snapshot: dict[str, Any]) -> list[tuple[str, Any]]:
    """'계약 조건' 표 행. 용역계약은 용역계약 용어와 생략 규칙을 적용한다."""
    freelancer = is_freelancer_snapshot(snapshot)
    rows: list[tuple[str, Any]] = []
    for key, name in TERM_FIELDS:
        value = snapshot.get(key)
        if not _text(value):
            continue
        if key == "bank_account_masked" and _text(snapshot.get("bank_account_number")):
            continue  # 전체 번호가 있으면 계좌번호 칸은 한 번만, 전체 번호로 찍는다
        if freelancer:
            if key in FREELANCER_SKIPPED_KEYS or (key in FREELANCER_ZERO_HIDDEN_KEYS and _is_zero_amount(value)):
                continue
            name = FREELANCER_TERM_LABELS.get(key, name)
        rows.append((name, display_value(key, value)))
    return rows


def extra_rows(snapshot: dict[str, Any]) -> list[tuple[str, Any]]:
    """'기타 기재사항' 표 행. 한글 이름이 없는 영문 내부 키는 인쇄하지 않고 키 이름만 경고로 남긴다."""
    freelancer = is_freelancer_snapshot(snapshot)
    known = {key for key, _ in PARTY_FIELDS + EMPLOYEE_FIELDS + TERM_FIELDS} | HIDDEN_FIELDS
    rows: list[tuple[str, Any]] = []
    for key in sorted(snapshot):
        if key in known or not _text(snapshot[key]):
            continue
        if freelancer and key in FREELANCER_SKIPPED_KEYS:
            continue
        name = EXTRA_LABELS.get(key)
        if name is None:
            if _INTERNAL_KEY_RE.match(str(key)):
                logger.warning("계약서 PDF 기타 기재사항: 한글 이름이 없는 내부 키를 인쇄하지 않음: %s", key)
                continue
            name = key
        rows.append((name, display_value(key, snapshot[key])))
    return rows


def _body_covered_fields(snapshot: dict[str, Any]) -> set[str]:
    """Prove complete consumption by the actual clause renderer, one field at a time.

    Replace only this field with a probe. Coverage requires an otherwise identical
    clause whose complete probe representation can be replaced by this field's
    complete representation. Unrelated numbers/text cannot establish coverage.
    Unknown transformations or conditional changes conservatively keep the seal.
    """
    original = contract_clauses(snapshot)
    keys = set()
    candidates = {key for key, _ in TERM_FIELDS} | set(EXTRA_LABELS)
    for key in candidates:
        raw = snapshot.get(key)
        if not _text(raw) or _text(raw) != _text(clause_value(snapshot, key)):
            continue
        if key == "weekly_hours" and not (
            clause_value(snapshot, "work_time") and clause_value(snapshot, "rest_time")
        ):
            continue  # preserve standalone hours in sparse historical seals
        marker = "OBYS_FIELD_COVERAGE_PROBE_" + key
        while marker in repr(snapshot):
            marker += "_"
        rendered = str(raw)
        if key == "wage":
            try:
                number = float(raw)
                if not number.is_integer():
                    continue  # rounded output cannot represent a fractional seal
            except (TypeError, ValueError, OverflowError):
                continue
            probe = 987654321
            while money(probe) in repr(original):
                probe += 1
            marker, rendered = money(probe), money(raw)
        elif key == "wage_type":
            probe = marker
            rendered = CODE_VALUE_LABELS.get(key, {}).get(raw, raw)
        elif key == "employment_tax_type":
            probe = marker
            rendered = CODE_VALUE_LABELS[key].get(raw, raw)
        else:
            probe = marker
        changed = dict(snapshot)
        changed[key] = probe
        camel = re.sub(r"_([a-z])", lambda match: match[1].upper(), key)
        if camel in changed:
            changed[camel] = probe
        alternatives = {str(rendered)}
        if key in {"start_date", "end_date"}:
            alternatives.add(korean_date(raw))
        for title, body in contract_clauses(changed):
            if marker in body and any(
                (title, body.replace(marker, representation)) in original
                for representation in alternatives
            ):
                keys.add(key)
                break
    # These fields are printed directly in the identity grid, without clauseValue.
    keys.update({"bank_name", "bank_account_holder", "bank_account_number"})
    if not snapshot.get("bank_account_number"):
        keys.add("bank_account_masked")
    salary_keys = ("base_salary", "non_tax_meal_allowance", "taxable_allowance")
    if snapshot.get("contract_type") == "regular" and any(snapshot.get(k) for k in salary_keys):
        for key in salary_keys:
            try:
                if float(snapshot.get(key)).is_integer():
                    keys.add(key)
            except (TypeError, ValueError, OverflowError):
                pass
    return keys


def render_signed_contract_pdf(contract: dict[str, Any], seal: dict[str, Any] | None = None) -> bytes:
    """봉인 스냅샷으로 서명본 PDF 바이트를 만든다. 같은 입력이면 같은 바이트가 나온다.

    ``seal`` 은 사용자(사업주) 도장 입력(kind/company_name/representative)이다. 봉인 내용이 아니라 해시에 들어가지 않는다.
    """
    snapshot = contract.get("signed_snapshot")
    recorded_sha = str(contract.get("signed_snapshot_sha256") or "")
    if not isinstance(snapshot, dict) or not snapshot:
        raise ContractPdfError("봉인 스냅샷(signed_snapshot)이 없습니다")
    if snapshot_sha256(snapshot) != recorded_sha:
        raise ContractPdfError("봉인 스냅샷 해시가 일치하지 않습니다 — PDF 를 만들지 않습니다")
    signature_png = _signature_png(
        str(contract.get("signature_data_uri") or ""),
        str(snapshot.get("signature_sha256") or ""),
    )
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Flowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.pdfgen.canvas import Canvas
    except ImportError:
        raise ContractPdfError("reportlab 이 설치되어 있지 않습니다") from None

    font = _register_font()
    seal_font_for = _seal_font_picker(font)
    from reportlab.pdfbase import pdfmetrics
    bold_font = pdfmetrics.getFont(font).fontName
    if BUNDLED_FONT.with_name("NanumGothic-Bold.ttf").is_file():
        bold_font = FONT_NAME + "Bold"
    # cv41-paper: 13px / 1.6, identity 12px / 1.45; 1 CSS px = .75pt.
    base = ParagraphStyle("body", fontName=font, fontSize=9.75, leading=15.6, wordWrap="CJK")
    small = ParagraphStyle("small", parent=base, fontSize=8.25, leading=12, textColor=colors.HexColor("#64748b"))
    label = ParagraphStyle("label", parent=base, fontName=bold_font, fontSize=9, leading=13.05, textColor=colors.HexColor("#45545b"))
    heading = ParagraphStyle("heading", parent=base, fontName=bold_font, fontSize=10.5, spaceBefore=10, spaceAfter=6, keepWithNext=True)
    title_style = ParagraphStyle("title", parent=base, fontName=bold_font, fontSize=15, leading=24, alignment=1, spaceAfter=4.5)
    clause_style = ParagraphStyle("clause", parent=base, leftIndent=18, firstLineIndent=-18, spaceAfter=4.5)

    def para(value: Any, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(escape(_text(value)).replace("\n", "<br/>"), style)

    def table(rows: list[tuple[str, Any]]) -> Table:
        data = [[para(name + "\n" + _text(value)) for name, value in rows]]
        grid = Table(data, colWidths=[170 * mm / len(rows)] * len(rows))
        grid.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return grid

    def rows_for(fields: tuple[tuple[str, str], ...]) -> list[tuple[str, Any]]:
        return [(name, snapshot.get(key)) for key, name in fields if _text(snapshot.get(key))]

    def cell(key: str, fallback: str = "-") -> Paragraph:
        return para(snapshot.get(key) or fallback)

    contract_type = str(snapshot.get("contract_type") or "")
    freelancer = is_freelancer_snapshot(snapshot)
    party = "수급인" if freelancer else "서약자" if contract_type == "confidentiality" else "근로자"
    consent = snapshot.get("signature_consent") if isinstance(snapshot.get("signature_consent"), dict) else {}
    audit = snapshot.get("signature_audit") if isinstance(snapshot.get("signature_audit"), dict) else {}
    identity = [
        [para("사용자", label), para("상호", label), cell("employer_name", "열정국밥"), para("대표자", label), cell("employer_representative")],
        ["", para("사업자등록번호", label), cell("employer_registration_no"), para("연락처", label), cell("employer_phone")],
        ["", para("주소", label), cell("employer_address"), "", ""],
        [para(party, label), para("성명", label), cell("employee_name"), para("생년월일·국적", label), para(f"{korean_date(snapshot.get('employee_birth_date')) or '-'} / {snapshot.get('employee_nationality') or '대한민국'}")],
        ["", para("주소", label), cell("employee_address"), "", ""],
        ["", para("연락처", label), para("비공개"), para("이메일", label), para(snapshot.get("employee_email_masked") or _mask_email(snapshot.get("employee_email") or ""))],
        ["", para("정산계좌" if freelancer else "급여계좌", label), para(" / ".join(str(x) for x in [snapshot.get("bank_name"), snapshot.get("bank_account_number") or snapshot.get("bank_account_masked"), "예금주 " + str(snapshot["bank_account_holder"]) if snapshot.get("bank_account_holder") else ""] if x) or "미등록"), "", ""],
    ]
    width = 170 * mm
    grid = Table(identity, colWidths=[width * n for n in (.09, .17, .285, .17, .285)])
    commands = [
        ("GRID", (0, 0), (-1, -1), .75, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("SPAN", (0, 0), (0, 2)), ("SPAN", (0, 3), (0, 6)),
        ("BACKGROUND", (0, 0), (1, -1), colors.HexColor("#f8fafc")),
        ("BACKGROUND", (3, 0), (3, -1), colors.HexColor("#f8fafc")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e9eff2")),
        ("VALIGN", (0, 0), (0, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5.25), ("RIGHTPADDING", (0, 0), (-1, -1), 5.25),
        ("TOPPADDING", (0, 0), (-1, -1), 3.75), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.75),
    ]
    for row in (2, 4, 6):
        commands.append(("SPAN", (2, row), (4, row)))
    grid.setStyle(TableStyle(commands))
    story: list[Any] = [
        para(CONTRACT_TITLES.get(contract_type, snapshot.get("print_title") or "계약서"), title_style),
        para(f"계약 체결일 {korean_date(snapshot.get('contract_date')) or '미입력'} · {'수행 지점' if freelancer else '근무지점'} {snapshot.get('branch') or '-'}", small),
        Spacer(1, 7.5), grid, Spacer(1, 6),
    ]
    if contract_type == "regular" and any(snapshot.get(k) for k in ("base_salary", "non_tax_meal_allowance", "taxable_allowance")):
        salary_fields = (
            ("base_salary", "과세 기본급"), ("non_tax_meal_allowance", "비과세 식대"),
            ("taxable_allowance", "기타 과세수당"),
        )
        salary_values = dict(rows_for(salary_fields))
        salary_rows = []
        for key, name in salary_fields:
            description = money(salary_values.get(name))
            if key == "non_tax_meal_allowance":
                description += "\n" + ("식사 미제공 조건" if snapshot.get("meal_provision") == "cash_no_meal" else "미적용")
            salary_rows.append((name, description))
        salary = table(salary_rows)
        story.append(salary)
    story.append(para("용역 상세 조항" if freelancer else "서약 상세 조항" if contract_type == "confidentiality" else "계약 상세 조항", heading))
    for number, (title, body) in enumerate(contract_clauses(snapshot), 1):
        story.append(Paragraph(f"{number}. <b>{escape(title)}</b><br/>" + escape(body).replace("\n", "<br/>"), clause_style))
    agreements = list(dict.fromkeys(_text(snapshot.get(k)) for k in ("terms", "special_terms") if _text(snapshot.get(k))))
    if agreements:
        story.extend([para("추가 특약", heading), para("\n".join(agreements))])
    # Coverage is field-specific: a value in an unrelated field is never proof
    # that this sealed field was printed (e.g. 40 hours vs. 400,000 won).
    clause_bodies = dict(contract_clauses(snapshot))
    covered_fields = {
        "probation_terms": bool(snapshot.get("probation_terms")),
        "special_terms": bool(snapshot.get("special_terms")),
        "terms": bool(snapshot.get("terms")),
        "probation_period": bool(snapshot.get("probation_period")) and any(
            _text(snapshot.get("probation_period")) in body.split("\n")
            for title, body in clause_bodies.items() if "수습" in title
        ),
    }
    covered_fields.update({key: True for key in _body_covered_fields(snapshot)})
    # These legacy identity fields are absent from the compact identity grid.
    residual = rows_for((("visa_status", "체류자격"),
                         ("foreign_registration_no_masked", "외국인등록번호")))
    if snapshot.get("probation_terms") and _text(snapshot.get("probation_period")) in (
        "수습기간 없음.", "수습기간 없음. 수습을 정한 경우에도 입력 임금을 동일 적용한다.",
    ):
        covered_fields["probation_period"] = True
    remaining_terms = {k: v for k, v in snapshot.items() if not covered_fields.get(k, False)}
    for name, item in term_rows(remaining_terms) + extra_rows(remaining_terms):
        if _text(item):
            residual.append((name, item))
    if residual:
        story.append(para("기존 봉인 약정 및 기재사항", heading))
        for name, item in residual:
            story.append(para(name, label))
            story.append(para(item))

    signature = Image(io.BytesIO(signature_png))
    scale = min(150 / (signature.imageWidth or 1), 60 / (signature.imageHeight or 1))
    signature.drawWidth = signature.imageWidth * scale
    signature.drawHeight = signature.imageHeight * scale
    seal_plan = (
        company_seal.layout(str(seal.get("kind") or ""), seal.get("company_name"), seal.get("representative"))
        if isinstance(seal, dict)
        else None
    )

    class SealFlowable(Flowable):
        def __init__(self, plan: dict[str, Any], side: float) -> None:
            super().__init__()
            self.plan = plan
            self.width = self.height = side

        def draw(self) -> None:
            company_seal.draw_seal(self.canv, self.plan, 0, 0, self.width, font, seal_font_for)

    def signed_time(value: Any) -> str:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone(timedelta(hours=9))).strftime("%Y년 %m월 %d일 %H:%M:%S KST")
        except ValueError:
            return _text(value)

    employer_box = [para("사용자 서명", heading), cell("employer_name", "열정국밥"), para(f"대표 {snapshot.get('employer_representative') or '-'} (서명 또는 인)")]
    if seal_plan:
        employer_box.append(SealFlowable(seal_plan, 20 * mm))
    employee_box = [para(party + " 서명", heading), para(snapshot.get("signer_name") or snapshot.get("employee_name")), signature, para("서명일시: " + signed_time(snapshot.get("signed_at")), small)]
    sign_grid = Table([[employer_box, "", employee_box]], colWidths=[(width - 9) / 2, 9, (width - 9) / 2])
    sign_grid.setStyle(TableStyle([
        ("BOX", (0, 0), (0, 0), .75, colors.HexColor("#cbd5e1")),
        ("BOX", (2, 0), (2, 0), .75, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7.5), ("RIGHTPADDING", (0, 0), (-1, -1), 7.5),
        ("TOPPADDING", (0, 0), (-1, -1), 7.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 7.5),
    ]))
    story.extend([Spacer(1, 10.5), sign_grid])
    evidence = "전자서명 확인 · 인증 계정 " + _mask_email(audit.get("authenticated_email") or snapshot.get("signer_email") or "")
    evidence += f"\n전자서명 동의: {'동의함' if consent.get('accepted') else '-'} ({consent.get('version') or ''}) · 봉인 해시 {recorded_sha[:12]}"
    evidence += "\n이 문서는 전자서명 시점에 봉인된 계약 내용으로 생성되었습니다."
    evidence_lines = evidence.split("\n")
    contract_number = str(snapshot.get("id") or "")[:8]

    class NumberedCanvas(Canvas):
        """페이지 상태를 보존해 전체 쪽수와 마지막 쪽 하단 증적을 그린다."""
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.pages: list[dict[str, Any]] = []

        def showPage(self) -> None:
            self.pages.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self.pages)
            for state in self.pages:
                self.__dict__.update(state)
                self.setFont(font, 7.5)
                self.setFillColor(colors.HexColor("#64748b"))
                self.drawCentredString(A4[0] / 2, 10 * mm, f"오비서 전자계약 · {contract_number} · {self._pageNumber} / {total} 쪽")
                if self._pageNumber == total:
                    self.setStrokeColor(colors.HexColor("#cbd5e1"))
                    self.setFillColor(colors.HexColor("#f8fafc"))
                    self.roundRect(20 * mm, 17 * mm, width, 18 * mm, 4, fill=1, stroke=1)
                    self.setFillColor(colors.HexColor("#64748b"))
                    for i, line in enumerate(evidence_lines):
                        self.drawString(22 * mm, (30 - i * 4) * mm, line)
                super().showPage()
            super().save()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=40 * mm,
        title=str(snapshot.get("print_title") or "계약서"),
        author="오비서",
        invariant=1,
    )
    try:
        doc.build(story, canvasmaker=NumberedCanvas)
    except Exception as exc:
        raise ContractPdfError(f"PDF 조판 실패: {type(exc).__name__}: {exc}") from None
    data = buffer.getvalue()
    if not data.startswith(b"%PDF-"):
        raise ContractPdfError("PDF 헤더가 올바르지 않습니다")
    return data
