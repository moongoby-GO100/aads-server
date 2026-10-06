"""오비서 V4.1 현재 관리자 화면 계약 작성(PRD AADS-LAYOUT-025) 배선·로직 검증.

- 정적: 현재 관리자 화면이 계약 모듈을 싣고 직원 현황·계약·인사증빙에서 mount 한다.
- 일치: contract-core.js 의 양식·검증·미리보기가 기존 관리화면(index.html) 코드와 같다.
- 로직(node): 화면이 만든 저장 본문이 서버 validator(_validate_contract_payload)를 통과하고,
  서버가 돌려준 누락 항목 이름이 모두 화면의 입력 칸으로 이어진다.
화면 동작은 별도 Playwright 캡처로 검증한다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "app" / "static" / "apps" / "obys"
V41 = (ROOT / "mockup-v4-1.html").read_text(encoding="utf-8")
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
CORE = (ROOT / "modules" / "contract-core.js").read_text(encoding="utf-8")
EDITOR = (ROOT / "modules" / "contract-editor-v41.js").read_text(encoding="utf-8")
SERVICE = (REPO / "app" / "services" / "yeoljeong_finance_service.py").read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    head = source.index(start)
    return source[head: source.index(end, head)]


def test_v41_loads_contract_modules_before_inline_script():
    core = V41.index("modules/contract-core.js?v=")
    editor = V41.index("modules/contract-editor-v41.js?v=")
    inline = V41.index("\n  <script>\n")
    assert core < editor < inline
    assert re.search(r"modules/contract-editor-v41\.css\?v=\d{8}-r\d+", V41)


def test_v41_mounts_editor_on_employee_and_contract_routes():
    assert "window.obysContractV41?.mount(state.route,page," in V41
    assert 'ROUTES = new Set(["employees", "hr-docs"])' in EDITOR
    # 가입 승인 직후 승인 직원 계약 목록이 갱신된다.
    assert 'if(action==="approved")window.obysContractV41?.refresh()' in V41


def test_editor_uses_existing_contract_api_and_no_legacy_redirect():
    for path in (
        '"/employees/approved"',
        'api("/contracts", { method: "POST"',
        "/request-signature`",
        "/resend-signature-notice`",
        "/signed-pdf/regenerate`",
        "/contracts/${encodeURIComponent(contractId)}/signed-pdf`",
    ):
        assert path in EDITOR, path
    assert "legacy=1" not in EDITOR
    assert "view=legacy" not in EDITOR


def test_editor_does_not_persist_personal_data_in_browser_storage():
    assert "localStorage" not in EDITOR
    assert "sessionStorage" not in EDITOR


def test_core_is_same_code_as_legacy_contract_screen():
    pairs = [
        ("const contractTypeLabels = {", "const payrollStatusLabels = {"),
        ("function validateContractDraft(draft) {", "function contractClause(title, body) {"),
        ("function contractPreviewHtml(contract) {", "function contractPreviewWithCurrentReferences(contract) {"),
    ]
    core_text = CORE.split("})(typeof window", 1)[0]
    for start, end in pairs:
        legacy = _block(INDEX, start, end).rstrip()
        assert legacy in core_text, f"contract-core.js 가 index.html 의 {start} 와 달라졌습니다"


def _server_missing_labels() -> set[str]:
    body = _block(SERVICE, "def _validate_contract_payload", "def _signed_contract_snapshot")
    return set(re.findall(r'\(\s*"([^"]+)",\s*(?:"[a-z_]+",\s*"[A-Za-z]+"|[a-z_]+|_contract_payload_value)', body)) | set(
        re.findall(r'missing\.append\("([^"]+)"\)', body)
    )


NODE_SCRIPT = r"""
globalThis.window = globalThis;
globalThis.location = { href: "https://fb.example/static/apps/obys/mockup-v4-1.html", origin: "https://fb.example", search: "" };
const [coreArg, editorArg] = process.argv.slice(-2);
require(coreArg);
require(editorArg);
const T = window.obysContractV41._test;
const C = window.ObysContractCore;
const target = { employee_request_id: "req-1", business_id: "biz-mia", branch: "미아점" };
const values = { contractType: "part_time", ...C.classificationFor("part_time") };
Object.entries(C.defaultsFor("part_time", "four_insurance")).forEach(([k, v]) => { if (!["wage", "baseSalary"].includes(k)) values[k] = String(v); });
const empty = T.checkLocally(T.draftFromValues({ ...values }, target));
Object.assign(values, {
  contractDate: "2026-10-06", startDate: "2026-10-07", employeeName: "시험직원", employeeEmail: "tester@example.com",
  employeePhone: "010-0000-0000", employeeBirthDate: "1990-01-01", employeeAddress: "서울시 시험구",
  workTime: "10:00-15:00", restTime: "12:00-12:30", weeklyHours: "주 18시간", workDays: "월/화/수/목",
  dailyWorkSchedule: "월~목 10:00-15:00 (휴게 30분)", wage: "10320", foreignWorker: "false"
});
const draft = T.draftFromValues(values, target);
const full = T.checkLocally(draft);
const payload = T.payloadFromDraft(draft, "");
const fieldByLabel = Object.fromEntries(T.FIELD_BY_LABEL);
console.log(JSON.stringify({ empty, full, payload, fieldByLabel }));
"""


@pytest.fixture(scope="module")
def node_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 가 없는 실행 환경 — 로직 검증은 node 가 있는 호스트에서 돈다")
    out = subprocess.run(
        [node, "-e", NODE_SCRIPT, "--", str(ROOT / "modules" / "contract-core.js"), str(ROOT / "modules" / "contract-editor-v41.js")],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


def test_blank_wage_and_dates_block_preview(node_result):
    empty = node_result["empty"]
    assert empty["ok"] is False
    for label in ("확정 임금", "직원명", "근로자 연락처", "근로자 생년월일", "근로자 주소", "입사일"):
        assert label in empty["missing"], label


def test_complete_draft_passes_client_and_server_validator(node_result):
    pytest.importorskip("fastapi")
    from app.services import yeoljeong_finance_service as svc

    assert node_result["full"]["ok"] is True, node_result["full"]["message"]
    payload = node_result["payload"]
    assert payload["employee_request_id"] == "req-1"
    assert payload["business_id"] == "biz-mia" and payload["branch"] == "미아점"
    assert payload["wage"] == 10320
    # 사용자 정보는 서버가 사업자 등록정보로 채운다 — 여기서는 채워진 상태를 흉내 낸다.
    payload.update(employer_name="시험상호", employer_registration_no="123-45-67890", employer_representative="대표", employer_address="서울")
    validated = svc._validate_contract_payload(payload)
    assert validated["contract_type"] == "part_time"


def test_every_server_missing_label_points_to_an_input(node_result):
    target_level = {"승인 직원", "사업자", "근무 지점", "직원 이메일"}
    mapped = set(node_result["fieldByLabel"])
    unmapped = {label for label in _server_missing_labels() if label not in mapped and label not in target_level}
    assert not unmapped, f"화면에서 찾을 수 없는 서버 누락 항목: {sorted(unmapped)}"
