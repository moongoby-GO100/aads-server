"""ACCT-FREELANCER-CONTRACT-LABELS-20261008 — 3.3% 용역계약서 PDF·미리보기 칸 이름/코드값 한글화."""
import base64
import hashlib
from io import BytesIO
from pathlib import Path

import pytest

from app.services import yeoljeong_contract_pdf as pdf

INDEX = (Path(__file__).resolve().parents[2] / "app/static/apps/obys/index.html").read_text(encoding="utf-8")

LABOR_LABELS = [name for _, name in pdf.TERM_FIELDS]


def _base_snapshot(**overrides):
    snapshot = {
        "id": "c-1",
        "tenant_id": "t-1",
        "contract_type": "freelancer",
        "document_kind": "freelancer_service_contract",
        "template_version": "v1",
        "print_title": "3.3% 프리랜서 용역계약서",
        "employer_name": "열정국밥",
        "employee_name": "양재혁",
        "start_date": "2026-10-10",
        "workplace": "열정국밥 미아점",
        "job_description": "촬영 및 편집",
        "work_time": "정하지 않음",
        "rest_time": "해당 없음",
        "weekly_hours": "해당 없음",
        "work_days": "해당 없음",
        "holidays": "해당 없음",
        "wage_type": "case_fee",
        "wage": 300000,
        "base_salary": 0,
        "non_tax_meal_allowance": 0,
        "taxable_allowance": 0,
        "wage_composition": "건당 30만원, 3.3% 공제",
        "pay_date": "검수 후 7일 이내",
        "pay_method": "계좌이체",
        "overtime_terms": "추가 수행은 별도 합의",
        "leave_terms": "별도 휴가 없음",
        "insurance_terms": "사업소득 3.3% 원천징수",
        "freelancer_scope": "영상 제작",
        "freelancer_settlement_terms": "검수 후 정산",
        "employment_tax_type": "freelancer_33",
        "workplace_size_category": "under_5",
        "meal_provision": "employer_meal",
    }
    snapshot.update(overrides)
    return snapshot


def _labor_snapshot():
    return _base_snapshot(
        contract_type="part_time",
        document_kind="standard_employment_contract",
        print_title="단시간 근로계약서",
        wage_type="hourly",
        wage=10320,
        base_salary=0,
        daily_work_schedule="월 09:00-13:00",
        probation_terms="수습 3개월",
        employment_tax_type="four_insurance",
    )


def test_freelancer_labels_replace_labor_terms():
    labels = dict(pdf.term_rows(_base_snapshot()))
    assert labels["수행 장소"] == "열정국밥 미아점"
    assert labels["수행 시간"] == "정하지 않음"
    assert labels["휴식"] == "해당 없음"
    assert labels["수행 회차 기준"] == "해당 없음"
    assert labels["수행 일정"] == "해당 없음"
    assert labels["휴일 규정"] == "해당 없음"
    assert labels["용역비 산정 방식"] == "건별 용역비"
    assert labels["용역비"] == 300000
    assert labels["용역비 구성/원천징수"].startswith("건당")
    assert labels["용역비 지급일"] == "검수 후 7일 이내"
    assert labels["추가 수행"] and labels["휴가 규정"] and labels["세무 처리"]
    for forbidden in (
        "근무장소", "근무시간", "휴게시간", "주 소정근로시간", "근무일/요일", "근로일별 근로시간",
        "휴일/주휴", "임금 산정 방식", "임금(용역비)", "급여지급일", "임금 구성/공제",
        "연장·야간·휴일근로", "연차/휴가/결근", "4대보험/세무 처리", "수습",
    ):
        assert forbidden not in labels


def test_freelancer_hides_zero_wage_components_and_probation():
    labels = dict(pdf.term_rows(_base_snapshot(daily_work_schedule="", probation_terms="수습 3개월")))
    assert "기본급" not in labels
    assert "비과세 식대" not in labels
    assert "기타 과세수당" not in labels
    assert "수습" not in labels
    assert "수습 3개월" not in [value for _, value in pdf.term_rows(_base_snapshot(probation_terms="수습 3개월"))]


def test_freelancer_keeps_nonzero_allowance():
    labels = dict(pdf.term_rows(_base_snapshot(base_salary=100000)))
    assert "기본급" in labels


def test_case_fee_and_other_wage_codes_are_korean_everywhere():
    assert pdf.display_value("wage_type", "case_fee") == "건별 용역비"
    assert pdf.display_value("wage_type", "hourly") == "시급"
    assert pdf.display_value("wage_type", "monthly") == "월급"
    assert pdf.display_value("wage_type", "daily") == "일급"
    assert pdf.display_value("wage_type", "unknown_code") == "unknown_code"
    assert pdf.display_value("employment_tax_type", "freelancer_33") == "3.3% 프리랜서 원천징수"
    assert pdf.display_value("wage", "case_fee") == "case_fee"
    extras = dict(pdf.extra_rows(_base_snapshot()))
    assert extras["세무 처리 구분"] == "3.3% 프리랜서 원천징수"
    assert "case_fee" not in str(pdf.term_rows(_base_snapshot()))
    assert "workplace_size_category" not in extras and "meal_provision" not in extras


def test_labor_contract_labels_unchanged():
    rows = pdf.term_rows(_labor_snapshot())
    names = [name for name, _ in rows]
    expected = [name for key, name in pdf.TERM_FIELDS if str(_labor_snapshot().get(key, "")).strip()]
    assert names == expected
    assert {"근무장소", "근무시간", "휴게시간", "임금 산정 방식", "임금(용역비)", "급여지급일"} <= set(names)
    assert dict(rows)["임금 산정 방식"] == "시급"
    assert not set(names) & (set(pdf.FREELANCER_TERM_LABELS.values()) - set(LABOR_LABELS))
    extras = dict(pdf.extra_rows(_labor_snapshot()))
    assert extras["사업장 상시근로자 규모"] == "상시 5인 미만"
    assert extras["식사 제공 방식"] == "사용자 식사 제공"
    assert "workplace_size_category" not in extras and "meal_provision" not in extras


def test_labor_term_fields_definition_is_untouched():
    assert ("workplace", "근무장소") in pdf.TERM_FIELDS
    assert ("wage", "임금(용역비)") in pdf.TERM_FIELDS
    assert ("probation_terms", "수습") in pdf.TERM_FIELDS


def _contract_for(snapshot):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    raw = buffer.getvalue()
    snapshot = {
        **snapshot,
        "signature_sha256": hashlib.sha256(raw).hexdigest(),
        "signer_name": "양재혁",
        "signed_at": "2026-10-08T10:00:00+09:00",
    }
    return {
        "signed_snapshot": snapshot,
        "signed_snapshot_sha256": pdf.snapshot_sha256(snapshot),
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
    }


def _capture_paragraphs(monkeypatch):
    import reportlab.platypus as platypus

    captured = []
    original = platypus.Paragraph

    class Recording(original):
        def __init__(self, text, *args, **kwargs):
            captured.append(text)
            super().__init__(text, *args, **kwargs)

    monkeypatch.setattr(platypus, "Paragraph", Recording)
    return captured


def test_freelancer_pdf_uses_service_terms_and_keeps_seal(monkeypatch):
    pytest.importorskip("reportlab")
    captured = _capture_paragraphs(monkeypatch)
    contract = _contract_for(_base_snapshot())
    before_sha = contract["signed_snapshot_sha256"]
    before_snapshot = dict(contract["signed_snapshot"])

    data = pdf.render_signed_contract_pdf(contract)

    assert data.startswith(b"%PDF-")
    joined = "\n".join(captured)
    for expected in ("수행 장소", "용역비 산정 방식", "건별 용역비", "수급인(계약 상대방)", "위탁자(사업주)"):
        assert expected in joined
    for forbidden in ("근무장소", "임금 산정 방식", "case_fee", "근로기준법 제17조", "기본급", "비과세 식대"):
        assert forbidden not in joined
    assert contract["signed_snapshot"] == before_snapshot
    assert pdf.snapshot_sha256(contract["signed_snapshot"]) == before_sha
    assert f"봉인 스냅샷 SHA-256: {before_sha}" in joined
    assert pdf.render_signed_contract_pdf(contract) == data


def test_labor_pdf_keeps_labor_terms(monkeypatch):
    pytest.importorskip("reportlab")
    captured = _capture_paragraphs(monkeypatch)
    pdf.render_signed_contract_pdf(_contract_for(_labor_snapshot()))
    joined = "\n".join(captured)
    for expected in ("근무장소", "임금 산정 방식", "임금(용역비)", "근로자(계약 상대방)", "사용자(사업주)", "근로기준법 제17조"):
        assert expected in joined
    assert "수행 장소" not in joined


def test_preview_html_uses_service_terms_for_case_fee():
    assert 'case_fee: "건별 용역비"' in INDEX
    assert 'isFreelancerContract ? "수행 지점" : "근무지점"' in INDEX
    assert 'case_fee: "건별/용역비"' not in INDEX
    for code, label in (("hourly", "시급"), ("monthly", "월급"), ("daily", "일급")):
        assert f'{code}: "{label}"' in INDEX


INTERNAL_EXTRAS = dict(
    foreign_worker="not_applicable",
    meal_uniform_terms="식사 제공, 복장 지급",
    memo="내부 메모",
    minor_guardian_consent="not_applicable",
    onboarding_document_summary={"submitted": 3},
    updated_at="2026-10-08T10:00:00+09:00",
    deleted_at="2026-10-09T10:00:00+09:00",
    health_certificate_valid_until="2027-01-31",
)


@pytest.mark.parametrize("make", [_base_snapshot, _labor_snapshot])
def test_extra_rows_hide_internal_keys_and_use_korean_names(make):
    snapshot = make()
    snapshot.update(INTERNAL_EXTRAS)
    rows = pdf.extra_rows(snapshot)
    extras = dict(rows)
    assert extras["외국인 근로 여부"] == "해당 없음"
    assert extras["미성년자 보호자 동의"] == "해당 없음"
    assert extras["식사·복장"] == "식사 제공, 복장 지급"
    assert extras["보건증 유효기간"] == "2027-01-31"
    text = str(rows)
    for forbidden in ("memo", "updated_at", "deleted_at", "onboarding_document_summary", "내부 메모",
                      "foreign_worker", "minor_guardian_consent", "meal_uniform_terms",
                      "health_certificate_valid_until", "not_applicable"):
        assert forbidden not in text
    assert not any(pdf._INTERNAL_KEY_RE.match(name) for name, _ in rows)


def test_display_value_codes_and_booleans():
    assert pdf.display_value("foreign_worker", "not_applicable") == "해당 없음"
    assert pdf.display_value("foreign_worker", True) == "예"
    assert pdf.display_value("foreign_worker", False) == "아니오"
    assert pdf.display_value("foreign_worker", "mystery_code") == "mystery_code"
    assert pdf.display_value("wage", 0) == 0


def test_unmapped_snake_key_not_printed_but_logged(caplog):
    snapshot = _base_snapshot(brand_new_internal_key="secret-value", 특기사항="자유 입력")
    with caplog.at_level("WARNING", logger=pdf.logger.name):
        rows = pdf.extra_rows(snapshot)
    text = str(rows)
    assert "brand_new_internal_key" not in text and "secret-value" not in text
    assert ("특기사항", "자유 입력") in rows
    warned = [r.getMessage() for r in caplog.records]
    assert any("brand_new_internal_key" in m for m in warned)
    assert not any("secret-value" in m for m in warned)


def test_pdf_extra_rows_do_not_print_internal_keys(monkeypatch):
    pytest.importorskip("reportlab")
    captured = _capture_paragraphs(monkeypatch)
    contract = _contract_for(_base_snapshot(**INTERNAL_EXTRAS))
    before_sha = contract["signed_snapshot_sha256"]
    data = pdf.render_signed_contract_pdf(contract)
    joined = "\n".join(captured)
    for forbidden in ("memo", "updated_at", "not_applicable", "foreign_worker", "onboarding_document_summary"):
        assert forbidden not in joined
    assert "외국인 근로 여부" in joined and "해당 없음" in joined
    assert pdf.snapshot_sha256(contract["signed_snapshot"]) == before_sha
    assert pdf.render_signed_contract_pdf(contract) == data


def test_labor_term_labels_unchanged_with_internal_extras():
    base = [name for name, _ in pdf.term_rows(_labor_snapshot())]
    with_extras = [name for name, _ in pdf.term_rows({**_labor_snapshot(), **INTERNAL_EXTRAS})]
    assert base == with_extras
