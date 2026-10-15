"""ACCT-OBYS-CONTRACT-REMOVE-INTERNAL-GUIDANCE-20261015 — 계약서 본문에는 당사자 간 약정만 남는다.

작성 담당자용 안내·점검표·경고·양식정보는 contractDraftGuidanceHtml(관리자 작성 화면의 종이 바깥 패널)로만
나오고, 서명 화면·직원 포털·인쇄·PDF 의 계약서 본문에는 나오지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import pytest

from app.services import yeoljeong_contract_pdf as pdf

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "app" / "static" / "apps" / "obys"
CORE_PATH = ROOT / "modules" / "contract-core.js"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
CORE = CORE_PATH.read_text(encoding="utf-8")
EDITOR = (ROOT / "modules" / "contract-editor-v41.js").read_text(encoding="utf-8")
PORTAL = (ROOT / "modules" / "employee-portal.js").read_text(encoding="utf-8")

FORBIDDEN = (
    "내부 양식", "내부 작성본", "필수 확인", "오분류", "하십시오", "최신양식", "A4 210mm", "기준 참고",
    "직원관리 화면", "입사서류 검수", "명시한다", "확정한다", "legal-basis", "legal-checklist",
    "classification-warning", "입사서류 확인", "보건증 유효기한", "문서보관함", "확정 입력",
    "별도 명시", "명확히 기재", "구분 기재", "확정 기재", "확정 근무표", "확정하여", "확인한다",
)

CONTRACT_TYPES = ("regular", "part_time", "manager", "freelancer", "confidentiality", "custom_unknown")

NODE_SCRIPT = r"""
globalThis.window = globalThis;
const C = require(process.argv.slice(-1)[0]);
const out = {};
for (const type of ["regular", "part_time", "manager", "freelancer", "confidentiality", "custom_unknown"]) {
  const cls = C.classificationFor(type);
  const taxType = cls.employmentTaxType || "four_insurance";
  const filled = { contractType: type, ...cls, ...C.defaultsFor(type, taxType),
    employeeName: "시험직원", employerName: "열정국밥", branch: "미아점", contractDate: "2026-10-15",
    startDate: "2026-10-16", wage: 10320, baseSalary: 3000000, nonTaxMealAllowance: 0, taxableAllowance: 0,
    onboardingDocumentSummary: "신분증, 통장사본", healthCertificateValidUntil: "2027-01-31" };
  const foreign = { ...filled, foreignWorker: true, visaStatus: "F-4", foreignRegistrationNoMasked: "900101-1******" };
  const minor = { ...filled, minorGuardianName: "보호자", minorGuardianPhone: "010-0000-0000" };
  const bare = { contractType: type };
  const snake = { contract_type: type, employee_name: "시험직원", foreign_worker: true, minor_guardian_name: "보호자" };
  out[type] = {
    filled: C.contractPreviewHtml(filled), foreign: C.contractPreviewHtml(foreign), minor: C.contractPreviewHtml(minor),
    bare: C.contractPreviewHtml(bare), snake: C.contractPreviewHtml(snake),
    guidance: C.contractDraftGuidanceHtml(filled),
    defaults: JSON.stringify(C.defaultsFor(type, taxType))
  };
}
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def rendered() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없는 실행 환경 — 정적 검사 테스트가 대신 돈다")
    out = subprocess.run([node, "-e", NODE_SCRIPT, "--", str(CORE_PATH)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@pytest.mark.parametrize("contract_type", CONTRACT_TYPES)
def test_contract_paper_has_no_internal_guidance(rendered, contract_type):
    for variant in ("filled", "foreign", "minor", "bare", "snake"):
        html = rendered[contract_type][variant]
        assert "<h3>" in html and "signature-grid" in html
        hits = [phrase for phrase in FORBIDDEN if phrase in html]
        assert not hits, f"{contract_type}/{variant}: 계약서 본문에 내부 문구 {hits}"


@pytest.mark.parametrize("contract_type", CONTRACT_TYPES)
def test_contract_defaults_are_agreement_sentences(rendered, contract_type):
    defaults = rendered[contract_type]["defaults"]
    hits = [phrase for phrase in FORBIDDEN if phrase in defaults]
    assert not hits, f"{contract_type}: 기본 문구에 작성 지시 {hits}"


@pytest.mark.parametrize("contract_type", ("regular", "part_time", "manager", "freelancer", "confidentiality"))
def test_preview_meta_keeps_only_date_and_branch(rendered, contract_type):
    html = rendered[contract_type]["filled"]
    meta = re.search(r'<p class="preview-meta">(.*?)</p>', html, re.S).group(1)
    assert meta.startswith("계약 체결일 2026-10-15 · ")
    assert meta.endswith("미아점")
    assert meta.count("·") == 1
    assert "서류" not in html


def test_replacement_clauses_are_present(rendered):
    regular = rendered["regular"]["bare"]
    assert "기간의 정함이 없는 근로계약으로 한다." in regular
    assert "수습기간 없음." in regular
    freelancer = rendered["freelancer"]["bare"]
    assert "독립적 업무수행" in freelancer and "독립성 및 근로자성 점검" not in freelancer
    assert "사업소득세 3.3%를 원천징수한다." in freelancer
    foreign = rendered["regular"]["foreign"]
    assert "근로자는 체류자격·체류기간이 바뀌면 사용자에게 즉시 알린다." in foreign
    for contract_type in ("regular", "freelancer", "confidentiality"):
        assert "전자서명 완료본" in rendered[contract_type]["bare"]


def test_guidance_panel_keeps_admin_notes(rendered):
    freelancer = rendered["freelancer"]["guidance"]
    for phrase in ("내부 양식", "용역계약 필수 확인", "프리랜서 오분류 방지", "재검토하십시오", "legal-basis", "legal-checklist", "classification-warning"):
        assert phrase in freelancer, phrase
    assert "근로기준법 제17조 필수 확인" in rendered["regular"]["guidance"]
    assert "내부 작성본" in rendered["part_time"]["guidance"]
    assert "서약서 필수 확인" in rendered["confidentiality"]["guidance"]
    assert "classification-warning" not in rendered["regular"]["guidance"]
    for contract_type in CONTRACT_TYPES:
        assert "contract-draft-guidance" in rendered[contract_type]["guidance"]


def _function_source(source: str, start: str, end: str) -> str:
    head = source.index(start)
    return source[head: source.index(end, head)]


def test_legacy_screen_renderer_has_no_internal_guidance():
    renderer = _function_source(INDEX, "function contractPreviewHtml(contract) {", "function contractPreviewWithCurrentReferences(")
    assert "legalBasisText" not in renderer and "legalChecklist" not in renderer and "classificationWarning" not in renderer
    for phrase in FORBIDDEN:
        assert phrase not in renderer, phrase
    defaults = _function_source(INDEX, "const standardContractDefaults = {", "const payrollStatusLabels = {")
    for phrase in FORBIDDEN:
        assert phrase not in defaults, phrase


def test_guidance_is_shown_only_outside_the_paper():
    assert "contractDraftGuidanceHtml" in CORE
    guided_lines = [line for line in INDEX.splitlines() if "contractDraftGuidanceHtml(" in line and "function " not in line]
    assert len(guided_lines) == 1 and "contractPreviewGuidance" in guided_lines[0]
    assert 'id="contractPreviewGuidance"' in INDEX
    assert INDEX.index('id="contractPreviewGuidance"') < INDEX.index('id="contractPreviewBody"')
    # 서명 화면·인쇄 모달·번들 미리보기·직원 포털은 계약서 본문만 쓴다.
    for sink in ("els.contractSignPaper.innerHTML", "els.contractPreviewModalPaper.innerHTML", "preview.innerHTML"):
        assert not any(sink in line and "Guidance" in line for line in INDEX.splitlines()), sink
    assert "contractDraftGuidanceHtml" not in PORTAL
    for line in EDITOR.splitlines():
        if "contractDraftGuidanceHtml" in line:
            assert 'class="cv41-guidance"' in line and "cv41-paper" not in line.split("cv41-guidance", 1)[0]
    assert re.search(r'cv41-guidance">\$\{C\.contractDraftGuidanceHtml\(currentDraft\(\)\)\}</div><article class="cv41-paper">', EDITOR)


def _contract_for(snapshot):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    raw = buffer.getvalue()
    snapshot = {**snapshot, "signature_sha256": hashlib.sha256(raw).hexdigest(), "signer_name": "양재혁", "signed_at": "2026-10-15T10:00:00+09:00"}
    return {
        "signed_snapshot": snapshot,
        "signed_snapshot_sha256": pdf.snapshot_sha256(snapshot),
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
    }


@pytest.mark.parametrize("contract_type,document_kind", [("freelancer", "freelancer_service_contract"), ("regular", "standard_employment_contract")])
def test_pdf_has_no_form_version_or_internal_codes(monkeypatch, contract_type, document_kind):
    pytest.importorskip("reportlab")
    import reportlab.platypus as platypus

    captured: list[str] = []
    original = platypus.Paragraph

    class Recording(original):
        def __init__(self, text, *args, **kwargs):
            captured.append(text)
            super().__init__(text, *args, **kwargs)

    monkeypatch.setattr(platypus, "Paragraph", Recording)
    snapshot = {
        "id": "c-1", "contract_type": contract_type, "document_kind": document_kind,
        "template_version": "majangbiseo-employment-2026-07-identity-table-v3",
        "print_title": "계약서", "employer_name": "열정국밥", "employee_name": "양재혁", "start_date": "2026-10-16",
        "wage_type": "case_fee", "wage": 300000, "minor_guardian_consent": "confirmed",
        "health_certificate_valid_until": "2027-01-31", "onboarding_document_summary": {"submitted": 3},
    }
    assert pdf.render_signed_contract_pdf(_contract_for(snapshot)).startswith(b"%PDF-")
    text = "\n".join(captured)
    assert "계약서 번호 c-1" in text
    for phrase in ("양식 v", "양식 majang", "template_version", "majangbiseo", "signed_snapshot", document_kind, "보건증", "입사서류", "confirmed"):
        assert phrase not in text, phrase
    assert "SHA-256" in text
