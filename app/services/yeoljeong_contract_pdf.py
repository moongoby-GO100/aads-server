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
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

FONT_ENV = "OBYS_CONTRACT_PDF_FONT_PATH"
FONT_NAME = "ObysContractKR"
BUNDLED_FONT = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "NanumGothic-Regular.ttf"
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
    "health_certificate_valid_until": "보건증 유효기간",
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
    return FONT_NAME


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


def render_signed_contract_pdf(contract: dict[str, Any]) -> bytes:
    """봉인 스냅샷으로 서명본 PDF 바이트를 만든다. 같은 입력이면 같은 바이트가 나온다."""
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
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        raise ContractPdfError("reportlab 이 설치되어 있지 않습니다") from None

    font = _register_font()
    base = ParagraphStyle("body", fontName=font, fontSize=9.5, leading=13.5, wordWrap="CJK")
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor("#444444"))
    label = ParagraphStyle("label", parent=base, textColor=colors.HexColor("#222222"))
    heading = ParagraphStyle("heading", parent=base, fontSize=11.5, leading=16, spaceBefore=8, spaceAfter=4)
    title_style = ParagraphStyle("title", parent=base, fontSize=17, leading=24, alignment=1, spaceAfter=6)

    def para(value: Any, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(escape(_text(value)).replace("\n", "<br/>"), style)

    def table(rows: list[tuple[str, Any]]) -> Table:
        data = [[para(name, label), para(value)] for name, value in rows]
        grid = Table(data, colWidths=[42 * mm, 128 * mm])
        grid.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return grid

    def rows_for(fields: tuple[tuple[str, str], ...]) -> list[tuple[str, Any]]:
        return [(name, snapshot.get(key)) for key, name in fields if _text(snapshot.get(key))]

    contract_type = str(snapshot.get("contract_type") or "")
    freelancer = is_freelancer_snapshot(snapshot)
    consent = snapshot.get("signature_consent") if isinstance(snapshot.get("signature_consent"), dict) else {}
    audit = snapshot.get("signature_audit") if isinstance(snapshot.get("signature_audit"), dict) else {}
    story: list[Any] = [
        para(snapshot.get("print_title") or "계약서", title_style),
        para(
            f"계약서 ID {snapshot.get('id') or ''} · 양식 {snapshot.get('template_version') or ''}",
            small,
        ),
        Spacer(1, 4 * mm),
        para("계약 개요", heading),
        table(
            [
                ("계약 유형", CONTRACT_TYPE_LABELS.get(contract_type, contract_type)),
                ("문서 종류", snapshot.get("document_kind")),
                ("사업장", snapshot.get("branch")),
                ("계약일", snapshot.get("contract_date")),
            ]
        ),
        para("위탁자(사업주)" if freelancer else "사용자(사업주)", heading),
        table(rows_for(PARTY_FIELDS) or [("상호", "")]),
        para("수급인(계약 상대방)" if freelancer else "근로자(계약 상대방)", heading),
        table(rows_for(EMPLOYEE_FIELDS) + [("계정", snapshot.get("employee_email_masked") or _mask_email(snapshot.get("employee_email") or ""))]),
    ]
    terms = term_rows(snapshot)
    if terms:
        story += [para("계약 조건", heading), table(terms)]
    extra = extra_rows(snapshot)
    if extra:
        story += [para("기타 기재사항", heading), table(extra)]

    signature = Image(io.BytesIO(signature_png))
    ratio = (signature.imageHeight or 1) / (signature.imageWidth or 1)
    signature.drawWidth = 60 * mm
    signature.drawHeight = min(30 * mm, 60 * mm * ratio)
    sign_grid = Table(
        [
            [para("서명자", label), para(snapshot.get("signer_name"))],
            [para("서명 일시", label), para(snapshot.get("signed_at"))],
            [para("인증 계정", label), para(_mask_email(audit.get("authenticated_email") or snapshot.get("signer_email") or ""))],
            [para("전자서명 동의", label), para(f"동의함 ({consent.get('version') or ''})" if consent.get("accepted") else "")],
            [para("자필서명", label), signature],
        ],
        colWidths=[42 * mm, 128 * mm],
    )
    sign_grid.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story += [
        para("전자서명", heading),
        sign_grid,
        Spacer(1, 5 * mm),
        para(f"봉인 스냅샷 SHA-256: {recorded_sha}", small),
        para(f"자필서명 SHA-256: {snapshot.get('signature_sha256') or ''}", small),
        para(
            "이 문서는 전자서명 시점에 봉인된 계약 내용(signed_snapshot)으로 생성되었습니다. "
            + (
                "용역계약 체결·교부 및 검수·정산 증빙으로 보관됩니다."
                if freelancer
                else "근로기준법 제17조에 따른 근로조건 서면 명시·교부 증빙으로 보관됩니다."
            ),
            small,
        ),
    ]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=str(snapshot.get("print_title") or "계약서"),
        author="오비서",
        invariant=1,
    )
    try:
        doc.build(story)
    except Exception as exc:
        raise ContractPdfError(f"PDF 조판 실패: {type(exc).__name__}: {exc}") from None
    data = buffer.getvalue()
    if not data.startswith(b"%PDF-"):
        raise ContractPdfError("PDF 헤더가 올바르지 않습니다")
    return data
