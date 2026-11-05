"""직원 화면 서류 업로드 칸이 로그인 계정이 아니라 화면 대상 직원으로 채워지는지 고정한다.

2026-11-05: 관리자가 김기훈(열정국밥_미아점) 직원 화면을 미리보면 직원 이메일 칸에 관리자
이메일이, 소속 지점에는 목록 첫 지점(중화점)이 들어갔다. 채우는 함수가 authSession.user 만 봤다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "app/static/apps/obys/index.html"
NODE = shutil.which("node")


def _function(html: str, name: str) -> str:
    head = f"    function {name}("
    assert head in html, name
    return head + html.split(head, 1)[1].split("\n    }\n", 1)[0] + "\n    }\n"


def _run(scenario: dict) -> dict:
    html = INDEX.read_text(encoding="utf-8")
    src = "".join(_function(html, n) for n in ("currentEmployeeBranches", "prefillOnboardingForm"))
    script = """
const sc = %s;
const field = v => ({ value: v || "", readOnly: false });
const select = { value: "열정국밥_중화점", innerHTML: "" };
Object.defineProperty(select, "innerHTML", { set(v) { this._html = v; }, get() { return this._html || ""; } });
const form = { employeeEmail: field(sc.formEmail), employeeName: field(""), branch: select, dataset: {} };
const els = { onboardingUploadForm: form };
const title = { textContent: "" };
const document = { getElementById: () => title };
let employeePreview = sc.preview;
const employeeJoinRequests = sc.requests;
const authSession = { user: sc.user };
const authPermissions = () => sc.permissions || {};
const isEmployeeExperience = () => Boolean(employeePreview) || Boolean(sc.employeeAccount);
const currentEmployeeEmail = () => employeePreview ? employeePreview.email : String(sc.user.email || "").toLowerCase();
const currentEmployeeName = () => employeePreview ? employeePreview.name : (sc.user.name || "직원");
const escapeHtml = v => String(v);
%s
prefillOnboardingForm();
const afterStart = { email: form.employeeEmail.value, name: form.employeeName.value, branch: form.branch.value,
  emailLocked: form.employeeEmail.readOnly, title: title.textContent };
employeePreview = null;
sc.employeeAccount = false;
prefillOnboardingForm();
const afterStop = { email: form.employeeEmail.value, emailLocked: form.employeeEmail.readOnly, title: title.textContent };
console.log(JSON.stringify({ afterStart, afterStop }));
""" % (json.dumps(scenario, ensure_ascii=False), src)
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


REQUESTS = [
    {"id": "old", "status": "archived", "email": "cbn100@naver.comc", "name": "김기훈", "branch": "열정국밥_미아점"},
    {"id": "b941", "status": "approved", "email": "cbn100@naver.com", "name": "김기훈", "branch": "열정국밥_미아점"},
]


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_admin_preview_fills_target_employee_not_admin() -> None:
    result = _run({
        "preview": {"email": "cbn100@naver.com", "name": "김기훈", "requestId": "b941"},
        "requests": REQUESTS,
        "user": {"email": "owner@example.com", "name": "대표"},
        "formEmail": "owner@example.com",
    })
    start = result["afterStart"]
    assert start["email"] == "cbn100@naver.com"
    assert start["name"] == "김기훈"
    assert start["branch"] == "열정국밥_미아점"
    assert start["emailLocked"] is True
    assert start["title"] == "내 입사서류 올리기"
    stop = result["afterStop"]
    assert stop["email"] == "" and stop["emailLocked"] is False
    assert stop["title"] == "신규 직원 서류 업로드"


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_real_employee_gets_own_approved_branch() -> None:
    result = _run({
        "preview": None,
        "employeeAccount": True,
        "requests": REQUESTS,
        "user": {"email": "cbn100@naver.com", "name": "김기훈"},
        "permissions": {"role": "employee", "employee_request_id": "b941"},
    })
    start = result["afterStart"]
    assert start["email"] == "cbn100@naver.com"
    assert start["branch"] == "열정국밥_미아점"


@pytest.mark.skipif(NODE is None, reason="node 없음")
def test_admin_screen_does_not_prefill_admin_email() -> None:
    result = _run({"preview": None, "requests": REQUESTS, "user": {"email": "owner@example.com", "name": "대표"}})
    assert result["afterStart"]["email"] == ""
    assert result["afterStart"]["title"] == "신규 직원 서류 업로드"
