"""ACCT-OBYS-CONTRACT-COMPANY-SEAL-DESIGN-20261015 — 서명본 PDF 도장과 서버 도장 입력(운영 이미지 의존: reportlab·fastapi).

호스트 파이썬에는 두 패키지가 없어 bash scripts/run_unit_tests.sh 로 실행한다. 화면 쪽 검증은 test_obys_company_seal.py.
"""
import base64
import hashlib
from io import BytesIO

from app.services import yeoljeong_contract_pdf as pdf


def _signed_contract(**snapshot_overrides):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    raw = buffer.getvalue()
    snapshot = {
        "id": "c-seal",
        "tenant_id": "t-1",
        "contract_type": "part_time",
        "print_title": "단시간 근로계약서",
        "employer_name": "주식회사 열정국밥",
        "employer_representative": "오윤희",
        "employee_name": "시험직원",
        "signature_sha256": hashlib.sha256(raw).hexdigest(),
        "signer_name": "시험직원",
        "signed_at": "2026-10-15T10:00:00+09:00",
        **snapshot_overrides,
    }
    return {
        "signed_snapshot": snapshot,
        "signed_snapshot_sha256": pdf.snapshot_sha256(snapshot),
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
    }


def test_signed_pdf_contains_the_seal_and_stays_deterministic():
    contract = _signed_contract()
    sha_before = contract["signed_snapshot_sha256"]
    plain = pdf.render_signed_contract_pdf(contract)
    corp = pdf.render_signed_contract_pdf(contract, seal={"kind": "corporation", "company_name": "주식회사 열정국밥", "representative": "오윤희"})
    individual = pdf.render_signed_contract_pdf(contract, seal={"kind": "individual", "company_name": "열정국밥 미아점", "representative": "최미미"})
    assert corp.startswith(b"%PDF-") and individual.startswith(b"%PDF-")
    assert len({plain, corp, individual}) == 3
    assert pdf.render_signed_contract_pdf(contract, seal={"kind": "corporation", "company_name": "주식회사 열정국밥", "representative": "오윤희"}) == corp
    assert pdf.render_signed_contract_pdf(contract, seal={"kind": "individual", "company_name": "", "representative": ""}) == plain
    assert pdf.snapshot_sha256(contract["signed_snapshot"]) == sha_before


def test_contract_seal_inputs_follow_snapshot_then_business_record(monkeypatch):
    from app.services import yeoljeong_finance_service as svc

    monkeypatch.setattr(svc, "_business_record_for_id", lambda business_id: {"name": "사업장상호", "representative": "사업장대표", "entityType": "individual"} if business_id == "biz-x" else {})
    full = svc._contract_seal_inputs({"signed_snapshot": {"business_id": "biz-x", "employer_name": "열정국밥", "employer_representative": "오윤희"}})
    assert full == {"kind": "individual", "company_name": "열정국밥", "representative": "오윤희"}
    from_business = svc._contract_seal_inputs({"business_id": "biz-x", "signed_snapshot": {}})
    assert from_business == {"kind": "individual", "company_name": "사업장상호", "representative": "사업장대표"}
    by_name = svc._contract_seal_inputs({"signed_snapshot": {"employer_name": "주식회사 가나", "employer_representative": "홍길동"}})
    assert by_name["kind"] == "corporation"
    monkeypatch.setattr(svc, "_business_record_for_id", lambda business_id: (_ for _ in ()).throw(RuntimeError("boom")))
    assert svc._contract_seal_inputs({"signed_snapshot": {}}) is None


def test_store_signed_pdf_passes_seal_inputs_to_renderer(monkeypatch, tmp_path):
    from app.services import yeoljeong_finance_service as svc

    seen = {}

    def fake_render(contract, seal=None):
        seen["seal"] = seal
        return b"%PDF-fake"

    monkeypatch.setattr(pdf, "render_signed_contract_pdf", fake_render)
    monkeypatch.setattr(svc, "_persist_contract_delivery", lambda contract, user: None)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path))
    monkeypatch.setattr(svc, "_business_record_for_id", lambda business_id: {})
    contract = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "signed_snapshot": {"employer_name": "주식회사 열정국밥", "employer_representative": "오윤희"},
    }
    result = svc._store_signed_contract_pdf(contract, None)
    assert result["status"] == "stored"
    assert seen["seal"] == {"kind": "corporation", "company_name": "주식회사 열정국밥", "representative": "오윤희"}
