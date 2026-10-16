"""ACCT-OBYS-CONTRACT-COMPANY-SEAL-DESIGN-20261015 — 서명본 PDF 도장과 서버 도장 입력(운영 이미지 의존: reportlab·fastapi).

호스트 파이썬에는 두 패키지가 없어 bash scripts/run_unit_tests.sh 로 실행한다. 화면 쪽 검증은 test_obys_company_seal.py.
"""
import base64
import hashlib
from io import BytesIO
from pathlib import Path

import pytest

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


PDF_FONT_DIR = Path(__file__).parents[2] / "app/assets/fonts"


def _ksx1001_hangul():
    out = set()
    for hi in range(0xB0, 0xC9):
        for lo in range(0xA1, 0xFF):
            try:
                out.add(bytes([hi, lo]).decode("euc-kr"))
            except UnicodeDecodeError:
                pass
    return {ch for ch in out if 0xAC00 <= ord(ch) <= 0xD7A3}


def test_seal_pdf_fonts_cover_ksx1001_hangul_and_the_four_hanja():
    from reportlab.pdfbase.ttfonts import TTFont

    hangul = TTFont("t-hangul", str(PDF_FONT_DIR / "ObysSealHangul-Gugi-subset.ttf")).face.charToGlyph
    hanja = TTFont("t-hanja", str(PDF_FONT_DIR / "ObysSealHanja-NotoSerifKR-Black-subset.ttf")).face.charToGlyph
    assert all(ord(ch) in hangul for ch in _ksx1001_hangul())
    assert all(ord(ch) in hanja for ch in "代表理事印")
    assert all(ord(ch) in hangul for ch in "ABCabc0123()&.-")


def test_seal_font_picker_uses_gugi_for_hangul_hanja_font_for_hanja_and_body_font_otherwise():
    pick = pdf._seal_font_picker("Body")
    assert pick("열") == "ObysSealHangul" and pick("代") == "ObysSealHanja" and pick("事") == "ObysSealHanja"
    assert pick("㈜") == "Body" and pick("鸞") == "Body" and pick("·") == "Body"


def test_seal_font_picker_falls_back_to_the_body_font_when_the_files_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pdf, "SEAL_FONT_FILES", (("ObysSealMissing", tmp_path / "none.ttf"),))
    pick = pdf._seal_font_picker("Body")
    assert pick("열") == "Body" and pick("代") == "Body"


def test_signed_pdf_embeds_the_seal_fonts_for_corporation_and_still_works_for_individual():
    contract = _signed_contract()
    corp = pdf.render_signed_contract_pdf(contract, seal={"kind": "corporation", "company_name": "주식회사 열정국밥", "representative": "오윤희"})
    plain = pdf.render_signed_contract_pdf(contract)
    # reportlab 은 글꼴을 "/F2+0" 식 이름으로 서브셋 임베드하므로 이름이 아니라 임베드된 글꼴 파일 수로 센다(본문 + 도장 한글 + 도장 한자).
    assert corp.startswith(b"%PDF") and corp.count(b"/FontFile2") == plain.count(b"/FontFile2") + 2
    individual = pdf.render_signed_contract_pdf(contract, seal={"kind": "individual", "company_name": "열정국밥 미아점", "representative": "최미미"})
    assert individual.startswith(b"%PDF") and individual.count(b"/FontFile2") == plain.count(b"/FontFile2") + 1


# --- ACCT-OBYS-SEAL-REGISTERED-TRADE-NAME-20261016: 서버 도장 상호 = 사업장 trade_name → 계약서 값 → 사업장 name ---
def _browser_cases():
    import importlib.util

    path = Path(__file__).with_name("test_obys_company_seal.py")
    spec = importlib.util.spec_from_file_location("obys_company_seal_browser_cases", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TRADE_NAME_CASES


@pytest.mark.parametrize("case", _browser_cases())
def test_server_seal_inputs_match_the_browser_for_every_trade_name_case(monkeypatch, case):
    pytest.importorskip("fastapi", reason="운영 이미지 의존 — scripts/run_unit_tests.sh 로 실행")
    from app.services import yeoljeong_finance_service as svc

    business = case["business"]
    monkeypatch.setattr(svc, "_business_record_for_id", lambda business_id: business if business_id == "biz-x" else {})
    contract_values = case["contract"]
    snapshot = {
        "business_id": "biz-x",
        "employer_name": contract_values.get("employerName") or contract_values.get("employer_name") or "",
        "employer_representative": contract_values.get("employerRepresentative") or contract_values.get("employer_representative") or "",
    }
    seal = svc._contract_seal_inputs({"signed_snapshot": snapshot})
    assert seal["company_name"] == case["company"] and seal["kind"] == case["kind"]
    plan = svc.yeoljeong_company_seal.layout(seal["kind"], seal["company_name"], seal["representative"])
    assert sorted("".join(c["ch"] for c in plan["inner"])) == sorted(case["inner"])


def test_db_trade_name_is_authoritative_and_the_settings_file_record_is_only_a_fallback(monkeypatch):
    pytest.importorskip("fastapi", reason="운영 이미지 의존 — scripts/run_unit_tests.sh 로 실행")
    from app.services import yeoljeong_finance_service as svc

    tenant = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(svc, "_business_record_for_id", lambda business_id: {"name": "사업장상호", "tradeName": "파일의옛값", "entityType": "individual"})
    contract = {"tenant_id": tenant, "signed_snapshot": {"business_id": "biz-x", "employer_name": "계약서상호", "employer_representative": "홍길동"}}
    answers = iter([{"trade_name": "주식회사 DB상호"}, {"trade_name": None}, {}, None])
    seen = []

    def fake_run_db(coro):
        seen.append(coro.cr_frame.f_locals.get("business_id"))
        coro.close()
        return next(answers)

    monkeypatch.setattr(svc, "_run_db", fake_run_db)
    assert svc._contract_seal_inputs(contract)["company_name"] == "주식회사 DB상호"
    assert svc._contract_seal_inputs(contract)["company_name"] == "계약서상호"  # DB 에서 비워 둠 → 파일의 옛 값으로 되돌아가지 않는다
    assert svc._contract_seal_inputs(contract)["company_name"] == "파일의옛값"  # 사업장 행을 못 찾음 → 설정 파일 레코드
    assert svc._contract_seal_inputs(contract)["company_name"] == "파일의옛값"  # DB 를 쓸 수 없음
    assert seen == ["biz-x"] * 4
    no_tenant = {"signed_snapshot": {"business_id": "biz-x", "employer_name": "계약서상호"}}
    assert svc._contract_seal_inputs(no_tenant)["company_name"] == "파일의옛값" and len(seen) == 4  # 테넌트 없이는 DB 를 읽지 않는다


def test_signed_contract_pdf_keeps_its_frozen_seal_when_the_business_trade_name_changes_later(monkeypatch, tmp_path):
    pytest.importorskip("fastapi", reason="운영 이미지 의존 — scripts/run_unit_tests.sh 로 실행")
    from app.services import yeoljeong_finance_service as svc

    seen = []

    def fake_render(contract, seal=None):
        seen.append(seal)
        return b"%PDF-fake"

    monkeypatch.setattr(pdf, "render_signed_contract_pdf", fake_render)
    monkeypatch.setattr(svc, "_persist_contract_delivery", lambda contract, user: None)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path))
    record = {"name": "열정국밥 중화점", "entityType": "corporation", "representative": "오윤희"}
    monkeypatch.setattr(svc, "_business_record_for_id", lambda business_id: dict(record))
    frozen = {"kind": "corporation", "company_name": "열정국밥 중화점", "representative": "오윤희"}
    contract = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "signed_snapshot": {"business_id": "biz-x", "employer_name": "열정국밥 중화점", "employer_representative": "오윤희"},
        "signed_pdf_seal": frozen,
    }
    record["tradeName"] = "주식회사 윤희에프엔비"  # 서명 뒤에 등록증 상호를 입력
    assert svc._store_signed_contract_pdf(contract, None)["status"] == "stored"
    assert svc._store_signed_contract_pdf(contract, None)["status"] == "stored"
    assert seen == [frozen, frozen]
    # 고정값이 없는 서명본(trade_name 도입 전 서명)도 현재 trade_name 으로 바뀌지 않는다
    legacy = {k: v for k, v in contract.items() if k != "signed_pdf_seal"}
    assert svc._store_signed_contract_pdf(legacy, None)["status"] == "stored"
    assert seen[2]["company_name"] == "열정국밥 중화점" and "signed_pdf_seal" not in legacy
