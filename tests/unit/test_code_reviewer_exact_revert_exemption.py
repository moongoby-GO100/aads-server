"""정확한 되돌리기(REVERT_OF) 예외 — 러너 검증 + 리뷰어 면제 계약.

GO100 runner-aeeaba38: 1 추가 / 1,579 삭제 순수 revert 가 PRESERVATION_HARD_GATE 에 걸렸다.
지시서 표식만으로는 면제하지 않고, 러너가 patch-id 로 검증해 `revert_of_verified` 를 실을 때만 면제한다.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import app.services.code_reviewer as cr

ROOT = Path(__file__).resolve().parents[2]
CODE_PATH = "app/services/foo.py"
OTHER_PATH = "app/services/bar.py"
SHA = "b4dda431f0123456789abcdef0123456789abcde"
INSTRUCTION = "GO100 되돌리기\nREVERT_OF: b4dda431f\n"


def _revert_diff(path: str = CODE_PATH, additions: int = 1, deletions: int = 1579) -> str:
    lines = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,{deletions} +1,{additions} @@",
    ]
    lines += [f"-old_{i} = {i}" for i in range(deletions)]
    lines += [f"+new_{i} = {i}" for i in range(additions)]
    return "\n".join(lines) + "\n"


def _verified(sha: str = SHA, files=(CODE_PATH,)) -> dict:
    return {"sha": sha, "patch_id": "abc123", "files": list(files)}


def _gate(diff: str, instruction: str = INSTRUCTION, verified=None):
    return cr._precheck_preservation_gate(diff, instruction, None, revert_of_verified=verified)


def test_a_verified_and_matching_sha_exempts_net_removal():
    out: dict = {}
    verdict = cr._precheck_preservation_gate(
        _revert_diff(), INSTRUCTION, None, out, revert_of_verified=_verified()
    )
    assert verdict is None
    assert out["preservation_exempt_reason"] == "exact_revert"
    assert out["preservation_exempt_sha"] == SHA
    assert out["preservation_exempt_patch_id"] == "abc123"


def test_b_marker_only_stays_blocked():
    verdict = _gate(_revert_diff())
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"


def test_c_sha_mismatch_stays_blocked():
    verdict = _gate(_revert_diff(), verified=_verified(sha="1234567" + "0" * 33))
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"


def test_c2_no_marker_in_instruction_stays_blocked():
    verdict = _gate(_revert_diff(), instruction="일반 작업", verified=_verified())
    assert verdict is not None


def test_c3_verified_without_files_stays_blocked():
    verdict = _gate(_revert_diff(), verified={"sha": SHA, "patch_id": "abc123"})
    assert verdict is not None


def test_d_public_symbol_deleted_outside_revert_files_stays_flagged():
    other = (
        f"diff --git a/{OTHER_PATH} b/{OTHER_PATH}\n"
        f"--- a/{OTHER_PATH}\n+++ b/{OTHER_PATH}\n"
        "@@ -1,2 +1,2 @@\n"
        "-def public_api(x):\n-    return x\n"
        "+value = 1\n+other = 2\n"
    )
    verdict = _gate(_revert_diff() + other, verified=_verified())
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert verdict.verdict == "FLAG"
    assert any("public_api" in s for s in verdict.feedback.get("deleted_symbols", []))


def test_d2_public_symbol_deleted_inside_revert_files_is_exempt():
    diff = (
        f"diff --git a/{CODE_PATH} b/{CODE_PATH}\n"
        f"--- a/{CODE_PATH}\n+++ b/{CODE_PATH}\n"
        "@@ -1,4 +0,0 @@\n"
        "-def feature_added_by_mistake(x):\n-    return x\n-a = 1\n-b = 2\n"
    )
    assert _gate(diff, verified=_verified()) is None


def test_d3_extra_file_net_removal_still_judged_by_old_gate():
    extra = _revert_diff(path="docs/note.md", additions=1, deletions=50)
    verdict = _gate(_revert_diff() + extra, verified=_verified())
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"


# ── e) 러너 쪽 patch-id 비교 함수 ──────────────────────────────────────────

def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.py").write_text("one\ntwo\nthree\n")
    (r / "b.py").write_text("keep\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base0")
    (r / "a.py").write_text("one\ntwo\nthree\nfeature1\nfeature2\n")
    (r / "c.py").write_text("newfile\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "feature")
    feature = _git(r, "rev-parse", "HEAD")
    base = feature  # 러너 작업의 base = 기능 커밋이 들어간 시점
    return r, feature, base


def _run_fn(repo_path: Path, base: str, instruction: str) -> str:
    script = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
    fn = _extract_function(script, "revert_of_verified_json")
    # 러너 본문의 실제 정의를 그대로 가져온다(정책이 바뀌면 테스트도 따라간다).
    specs = next(ln for ln in script.splitlines() if ln.startswith("JOB_DIFF_EXCLUDE_SPECS="))
    fn = specs + "\n" + fn
    proc = subprocess.run(
        ["bash", "-c", f'set -eo pipefail\n{fn}\nrevert_of_verified_json "$1" "$2" "$3"',
         "_", str(repo_path), base, instruction],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _do_exact_revert(r: Path, feature: str) -> None:
    _git(r, "revert", "--no-edit", feature)


def test_e_exact_revert_matches(repo):
    r, feature, base = repo
    _do_exact_revert(r, feature)
    out = json.loads(_run_fn(r, base, f"REVERT_OF: {feature[:9]}\n"))
    assert out["sha"] == feature
    assert sorted(out["files"]) == ["a.py", "c.py"]
    assert out["patch_id"]


def test_e_exact_revert_with_extra_report_file_matches(repo):
    r, feature, base = repo
    _do_exact_revert(r, feature)
    (r / "docs").mkdir()
    (r / "docs" / "report.md").write_text("report\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "report")
    assert json.loads(_run_fn(r, base, f"REVERT_OF: {feature}\n"))["sha"] == feature


def test_e_one_line_difference_mismatches(repo):
    r, feature, base = repo
    _do_exact_revert(r, feature)
    (r / "a.py").write_text("one\ntwo\nthree\nEXTRA\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "tweak")
    assert _run_fn(r, base, f"REVERT_OF: {feature}\n") == ""


def test_e_partial_revert_mismatches(repo):
    r, feature, base = repo
    _git(r, "rm", "-q", "c.py")
    _git(r, "commit", "-qm", "partial")
    assert _run_fn(r, base, f"REVERT_OF: {feature}\n") == ""


def test_e_missing_marker_or_unknown_sha_yields_nothing(repo):
    r, feature, base = repo
    _do_exact_revert(r, feature)
    assert _run_fn(r, base, "no marker") == ""
    assert _run_fn(r, base, "REVERT_OF: deadbeef\n") == ""


def test_e_path_with_quote_is_json_escaped(repo):
    r, feature, base = repo
    odd = 'we"ird.py'
    (r / odd).write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "odd")
    odd_sha = _git(r, "rev-parse", "HEAD")
    _git(r, "revert", "--no-edit", odd_sha)
    out = json.loads(_run_fn(r, odd_sha, f"REVERT_OF: {odd_sha}\n"))
    assert out["files"] == [odd]


# ── 리뷰 반려 지적 교정 ────────────────────────────────────────────────────

def test_e_runner_artifacts_follow_job_diff_exclude_specs(repo):
    """원본 커밋이 .runner_full_diff.patch/RESULT-*.md 를 건드려도 코드 revert 는 일치한다."""
    r, _, _ = repo
    (r / ".runner_full_diff.patch").write_text("p1\n")
    (r / "RESULT-x.md").write_text("r1\n")
    (r / "a.py").write_text("one\ntwo\nthree\nfeature1\nfeature2\nmore\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "feature2")
    feature = _git(r, "rev-parse", "HEAD")
    _git(r, "revert", "--no-edit", feature)
    # 러너가 산출물을 복원·재작성한다 — 역패치와 다른 내용
    (r / ".runner_full_diff.patch").write_text("different\n")
    (r / "RESULT-x.md").write_text("other\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "runner artifacts")
    out = json.loads(_run_fn(r, feature, f"REVERT_OF: {feature}\n"))
    assert out["files"] == ["a.py"]


def test_header_paths_quoted_and_spaced():
    assert cr._diff_header_paths('diff --git "a/\\355\\225\\234\\352\\270\\200.py" "b/\\355\\225\\234\\352\\270\\200.py"') == ("한글.py", "한글.py")
    assert cr._diff_header_paths("diff --git a/my file.py b/my file.py") == ("my file.py", "my file.py")
    assert cr._diff_header_paths('diff --git "a/we\\"ird.py" "b/we\\"ird.py"') == ('we"ird.py', 'we"ird.py')
    assert cr._diff_header_paths("diff --git a/x.py b/y.py") == ("x.py", "y.py")


@pytest.mark.parametrize("header_path,real", [
    ('"app/\\355\\225\\234\\352\\270\\200.py"', "app/한글.py"),
    ("app/my file.py", "app/my file.py"),
])
def test_exemption_applies_to_quoted_and_spaced_paths(header_path, real):
    diff = _revert_diff(path=header_path.strip('"'))
    if header_path.startswith('"'):
        quoted = header_path
        diff = (
            f"diff --git a/ b/\n".replace("a/ b/", f'"a/{header_path[1:-1]}" "b/{header_path[1:-1]}"')
            + f'--- "a/{header_path[1:-1]}"\n+++ "b/{header_path[1:-1]}"\n@@ -1,1579 +1,1 @@\n'
            + "".join(f"-old_{i} = {i}\n" for i in range(1579)) + "+new_0 = 0\n"
        )
    out: dict = {}
    verdict = cr._precheck_preservation_gate(
        diff, INSTRUCTION, None, out, revert_of_verified=_verified(files=(real,))
    )
    assert verdict is None
    assert out["preservation_exempt_reason"] == "exact_revert"


def test_exemption_note_reaches_llm_prompt():
    import asyncio
    from unittest.mock import AsyncMock, patch

    response = json.dumps(dict(verdict="APPROVE", correctness=1, security=1, scope_compliance=1,
                               preservation=1, quality=1, issues=[], summary="ok"))
    call = AsyncMock(return_value=response)

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch.object(cr.asyncio, "to_thread", inline), \
         patch.object(cr, "_get_review_models", AsyncMock(return_value=["codex:gpt-test"])), \
         patch.object(cr, "_call_review_model", call), \
         patch.object(cr, "_save_review_result", AsyncMock()), \
         patch("app.services.review_advisory.schedule_review_advisory"):
        verdict = asyncio.run(cr.review_code_diff(
            project="GO100", job_id="runner-revert-prompt", diff=_revert_diff(),
            instruction=INSTRUCTION, files_changed=[CODE_PATH], revert_of_verified=_verified()))
    call.assert_awaited_once()
    assert "정확한 되돌리기 면제" in str(call.call_args)
    assert verdict.verdict == "APPROVE"


class _FakeConn:
    def __init__(self, recorded):
        self.recorded = recorded

    async def fetchval(self, *a, **k):
        return self.recorded


class _FakePool:
    def __init__(self, recorded):
        self.conn = _FakeConn(recorded)

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


import hashlib as _hl


def _h(text: str) -> str:
    return _hl.sha256(text.encode("utf-8")).hexdigest()


def _bound(base: dict, project="aads", diff="D", instruction="I") -> dict:
    return {**base, "project": project, "diff_sha256": _h(diff), "instruction_sha256": _h(instruction)}


def _attest(recorded, claimed, project="aads", diff="D", instruction="I"):
    import asyncio
    from unittest.mock import patch

    import app.api.code_review as api

    with patch("app.core.db_pool.get_pool", return_value=_FakePool(recorded)):
        return asyncio.run(
            api._attest_revert_of_verified(
                "runner-x", claimed, project=project, diff=diff, instruction=instruction
            )
        )


def test_forged_request_without_runner_record_is_ignored():
    claimed = {"sha": SHA, "patch_id": "forged", "files": ["app/everything.py"]}
    assert _attest(None, claimed) is None


def test_request_with_different_patch_id_is_ignored():
    recorded = _bound({"sha": SHA, "patch_id": "real", "files": [CODE_PATH]})
    assert _attest(recorded, {"sha": SHA, "patch_id": "forged", "files": ["x"]}) is None


def test_matching_request_uses_recorded_files_not_claimed_files():
    recorded = _bound({"sha": SHA, "patch_id": "real", "files": [CODE_PATH]})
    out = _attest(recorded, {"sha": SHA, "patch_id": "real", "files": ["app/everything.py"]})
    assert out == recorded


def test_reused_sha_and_patch_id_with_swapped_diff_is_ignored():
    """리뷰 지적 1: 같은 job_id·sha·patch_id 를 다른 diff 에 재사용해도 면제되지 않는다."""
    recorded = _bound({"sha": SHA, "patch_id": "real", "files": [CODE_PATH]})
    claimed = {"sha": SHA, "patch_id": "real"}
    assert _attest(recorded, claimed, diff="D-evil") is None
    assert _attest(recorded, claimed, instruction="I-other") is None
    assert _attest(recorded, claimed, project="other") is None


def test_record_without_request_binding_is_not_trusted():
    recorded = {"sha": SHA, "patch_id": "real", "files": [CODE_PATH]}
    assert _attest(recorded, {"sha": SHA, "patch_id": "real"}) is None


def test_nfd_path_is_not_treated_as_nfc_revert_file():
    """리뷰 지적 2: NFC 로 기록된 역패치 파일이 NFD 표기의 별개 파일까지 면제하지 않는다."""
    import unicodedata

    nfc = unicodedata.normalize("NFC", "app/caf\u00e9.py")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    diff = _revert_diff(path=nfd, additions=1, deletions=50)
    kept, ignored, _ = cr._strip_exact_revert_sections(diff, [nfc])
    assert ignored == [] and kept == diff
    kept, ignored, _ = cr._strip_exact_revert_sections(diff, [nfd])
    assert ignored == [nfd]
