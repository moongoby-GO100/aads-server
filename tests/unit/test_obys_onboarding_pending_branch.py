"""ACCT-ONBOARDING-PENDING-BRANCH-20261127 — 가입 직후(pending) 입사서류 업로드 지점 선택.

업로드 지점만 pending 본인 요청/가입 직후 저장값으로 대체하고, approved 전용 범위 판정은 유지한다.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app" / "static" / "apps" / "obys" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _fn(name: str) -> str:
    m = re.search(rf"^    (?:async )?function {name}\(", HTML, re.M)
    assert m, name
    i = HTML.index(") {", m.start()) + 2
    depth = 0
    for j in range(i, len(HTML)):
        depth += HTML[j] == "{"
        depth -= HTML[j] == "}"
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


HARNESS = r"""
const vm = require('node:vm');
const src = JSON.parse(process.env.SRC);
const cases = JSON.parse(process.env.CASES);
const out = {};
for (const c of cases) {
  const ctx = {
    String, Set, Array,
    employeeJoinRequests: c.rows,
    employeePreview: null,
    isEmployeeExperience: () => true,
    currentEmployeeEmail: () => 'me@x.com',
    authPermissions: () => ({ employee_request_id: c.requestId || '' }),
    loadEmployeeSelfSignupMap: () => c.signup || {},
  };
  vm.createContext(ctx);
  vm.runInContext(src.join('\n') + '\nthis.r = [currentEmployeeUploadBranches(), currentEmployeeBranches()];', ctx);
  out[c.name] = ctx.r;
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_upload_branches_node():
    row = lambda **k: {"id": "r1", "email": "me@x.com", "branch": "미아점", **k}  # noqa: E731
    cases = [
        {"name": "pending_own", "rows": [row(status="pending")]},
        {"name": "approved_wins", "rows": [row(status="approved", branch="중화점"), row(id="r2", status="pending", branch="미아점")]},
        {"name": "other_email_pending", "rows": [row(status="pending", email="o@x.com")]},
        {"name": "rejected", "rows": [row(status="rejected")]},
        {"name": "signup_fallback", "rows": [], "signup": {"me@x.com": {"branch": "강북점"}}},
        {"name": "request_id_first", "requestId": "r2", "rows": [row(status="pending"), row(id="r2", status="pending", branch="수유점")]},
    ]
    r = subprocess.run([NODE, "-e", HARNESS], text=True, capture_output=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SRC": json.dumps([_fn("currentEmployeeUploadBranches"), _fn("currentEmployeeBranches")]), "CASES": json.dumps(cases)})
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads(r.stdout)
    assert got["pending_own"] == [["미아점"], []]  # ① 기존 approved 전용 함수는 여전히 빈 배열
    assert got["approved_wins"][0] == ["중화점"]  # ②
    assert got["other_email_pending"][0] == []  # ③
    assert got["rejected"][0] == []  # ④
    assert got["signup_fallback"][0] == ["강북점"]
    assert got["request_id_first"][0] == ["수유점"]


def test_scope_judgement_still_approved_only():  # ⑤
    src = _fn("isCurrentEmployeeRecord")
    assert 'filter(item => String(item.status).toLowerCase() === "approved"' in src
    assert "pending" not in src and "currentEmployeeUploadBranches" not in src
    assert "currentEmployeeUploadBranches" not in _fn("loadOnboardingDocuments") if "function loadOnboardingDocuments(" in HTML else True


def test_callers_use_upload_branches_and_placeholder():
    assert "currentEmployeeUploadBranches()" in _fn("prefillOnboardingForm")
    assert "가입요청 확인 중 — 잠시 후 다시 열어 주십시오" in _fn("prefillOnboardingForm")
    assert "loadEmployeeJoinRequests" in _fn("prefillOnboardingForm")
