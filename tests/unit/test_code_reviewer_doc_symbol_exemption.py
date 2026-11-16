"""AADS-RUNNER-RESULT-MD-EXCLUDE-20261117 회귀 검증 테스트.

1. .md·.markdown·.txt·.rst 안의 -def foo / -async def bar 만 있는 diff 는 FLAG 0.3 을 내지 않는다.
2. 같은 diff 에 .py 의 -def public_fn 이 있으면 계속 FLAG 0.3 이다.
3. 러너 스크립트에 RESULT.md 제외 코드와 result_md_excluded 이벤트가 있다(정적 검사).
4. .gitignore 에 /RESULT.md 가 있다. 추적 해제는 후속 작업에서 수행한다.
5. 러너가 commit_job_worktree_for_approval 실행 시 스테이징된 루트 RESULT.md 를 제외하고 워크트리 파일은 보존하며 result_md_excluded 이벤트를 남긴다.
"""

from pathlib import Path
import re
import shutil
import subprocess

import pytest

import app.services.code_reviewer as cr

ROOT = Path(__file__).resolve().parents[2]


def _make_diff(path: str, deletions: list[str], additions: list[str]) -> str:
    del_lines = [f"-{d}" for d in deletions]
    add_lines = [f"+{a}" for a in additions]
    del_count = max(len(del_lines), 1)
    add_count = max(len(add_lines), 1)
    header = [
        f"diff --git a/{path} b/{path}",
        "index 0000001..0000002 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,{del_count} +1,{add_count} @@",
    ]
    return "\n".join(header + del_lines + add_lines) + "\n"


# ── 1. .md 안의 -def foo / -async def bar 는 FLAG 를 내지 않음 ──


def test_markdown_deleted_symbols_do_not_produce_flag():
    # .md 파일 안의 -def foo / -async def bar 만 있는 diff
    md_diff = _make_diff(
        "RESULT.md",
        ["def foo():", "async def bar():"],
        ["### Report summary", "All tests passed successfully."],
    )
    # 심볼 목록에 잡히지 않음
    removed = cr._removed_preservation_symbols(md_diff)
    assert removed == []

    # 보존 게이트 통과 (FLAG 발생 안 함)
    verdict = cr._precheck_preservation_gate(md_diff, "instruction", ["RESULT.md"])
    assert verdict is None


@pytest.mark.parametrize("ext", [".md", ".markdown", ".txt", ".rst"])
def test_all_doc_extensions_exempt_from_deleted_symbol_gate(ext):
    doc_path = f"docs/release_notes{ext}"
    diff = _make_diff(
        doc_path,
        ["def foo():", "async def bar():", "@router.get('/items')", "class Sample:"],
        ["updated documentation line 1", "updated documentation line 2", "more details", "final text"],
    )
    removed = cr._removed_preservation_symbols(diff)
    assert removed == []

    verdict = cr._precheck_preservation_gate(diff, "instruction", [doc_path])
    assert verdict is None


# ── 2. 같은 diff 에 .py 의 -def public_fn 이 있으면 계속 FLAG 0.3 ──


def test_mixed_diff_with_py_deleted_symbol_still_flags_0_3():
    md_diff = _make_diff(
        "RESULT.md",
        ["def foo():", "async def bar():"],
        ["### Report summary", "All tests passed successfully."],
    )
    py_diff = _make_diff(
        "app/services/code_reviewer.py",
        ["def public_fn():"],
        ["# replaced public_fn", "# extra line 1", "# extra line 2"],
    )
    mixed_diff = md_diff + py_diff

    # 심볼 목록에 .py 의 public_fn 만 잡힘
    removed = cr._removed_preservation_symbols(mixed_diff)
    assert removed == ["def public_fn"]

    # 보존 게이트에서 FLAG 0.3 발생
    verdict = cr._precheck_preservation_gate(
        mixed_diff,
        "instruction",
        ["RESULT.md", "app/services/code_reviewer.py"],
    )
    assert verdict is not None
    assert verdict.verdict == "FLAG"
    assert verdict.score == 0.3
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert "def public_fn" in verdict.feedback["deleted_symbols"]
    assert "def foo" not in verdict.feedback["deleted_symbols"]
    assert "def bar" not in verdict.feedback["deleted_symbols"]


@pytest.mark.parametrize("code_ext,code_path", [
    (".py", "app/api/endpoints.py"),
    (".js", "scripts/bundle.js"),
    (".ts", "src/services/api.ts"),
    (".tsx", "components/Widget.tsx"),
    (".jsx", "components/Button.jsx"),
    (".sh", "scripts/deploy.sh"),
])
def test_code_files_still_gate_deleted_symbols(code_ext, code_path):
    diff = _make_diff(
        code_path,
        ["def public_func():"],
        ["# replaced 1", "# replaced 2", "# replaced 3"],
    )
    removed = cr._removed_preservation_symbols(diff)
    assert "def public_func" in removed


# ── 3. 러너 스크립트 정적 검사 (RESULT.md 제외 코드 및 result_md_excluded 이벤트) ──


def test_runner_scripts_contain_result_md_exclusion_and_event():
    for name in ("scripts/pipeline-runner.sh", "scripts/pipeline-runner.sh.local"):
        script = (ROOT / name).read_text(encoding="utf-8")
        assert "RESULT_MD_EXCLUDED" in script, f"{name} missing RESULT_MD_EXCLUDED log"
        assert "result_md_excluded" in script, f"{name} missing result_md_excluded event"
        assert 'case "$_res_path" in' in script or "case '$_res_path' in" in script, f"{name} missing case"
        assert "RESULT.md)" in script, f"{name} missing RESULT.md) pattern"


def test_pipeline_runner_and_local_are_strictly_identical():
    runner = (ROOT / "scripts/pipeline-runner.sh").read_bytes()
    local = (ROOT / "scripts/pipeline-runner.sh.local").read_bytes()
    assert runner == local, "scripts/pipeline-runner.sh and .local must be identical"


# ── 4. .gitignore 및 git ls-files 검사 ──


def test_gitignore_lists_root_result_md_for_followup_untrack():
    # 추적 해제는 후속 작업 AADS-RUNNER-RESULT-MD-UNTRACK에서 수행한다.
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/RESULT.md" in [line.strip() for line in gitignore.splitlines()]


def test_git_ls_files_in_repo_with_gitignore(tmp_path, need_tools):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("/RESULT.md\n", encoding="utf-8")
    (repo / "RESULT.md").write_text("# Result\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "init")
    out = _git(repo, "ls-files", "RESULT.md")
    assert out == ""


# ── 5. 러너 승인 커밋 시 스테이징된 RESULT.md 제외 및 워크트리 보존 ──


@pytest.fixture()
def need_tools():
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _fn(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_pipeline_runner_excludes_staged_result_md_and_keeps_worktree(tmp_path, need_tools):
    script = _read("scripts/pipeline-runner.sh")
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    _git(wt, "config", "user.email", "test@example.com")
    _git(wt, "config", "user.name", "Test")
    (wt / "app.py").write_text("a = 1\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "seed")
    base = _git(wt, "rev-parse", "HEAD")
    _git(wt, "checkout", "-q", "--detach")
    _git(wt, "branch", "-f", "main", base)

    # 작업 중 app.py 변경 및 RESULT.md 생성/스테이징
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    (wt / "RESULT.md").write_text("# Runner Result\n-async def fake_employee_lock\n", encoding="utf-8")
    _git(wt, "add", "app.py", "RESULT.md")

    events = tmp_path / "events.txt"
    fail = tmp_path / "fail.txt"
    harness = f"""
set -o pipefail
log() {{ echo "$*"; }}
sql_escape() {{ printf "%s" "'$1'"; }}
db_update() {{ :; }}
record_runner_event() {{ printf '%s|%s|%s\\n' "$2" "$3" "$9" >> "$EVENTS_FILE"; }}
verify_isolated_job_worktree() {{ return 0; }}
requeue_scope_violations() {{ return 0; }}
is_deploy_only_instruction() {{ return 1; }}
record_git_diagnostics() {{ echo diag; }}
_fail_job() {{ printf '%s\\n' "$3|$4" >> "$FAIL_FILE"; return 1; }}
{_fn(script, "mask_git_diagnostics")}
{_fn(script, "verify_worker_commit_provenance")}
{_fn(script, "commit_job_worktree_for_approval")}
"""
    fn_file = tmp_path / "fn.sh"
    fn_file.write_text(harness, encoding="utf-8")
    env = {
        "PATH": subprocess.os.environ["PATH"],
        "FAIL_FILE": str(fail),
        "EVENTS_FILE": str(events),
        "HOME": str(tmp_path),
    }

    proc = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; commit_job_worktree_for_approval runner-abc12345 s "{wt}" /m "inst" "{base}"'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr + (fail.read_text() if fail.exists() else "")

    # 커밋에는 app.py 만 들어가고 RESULT.md 는 들어가지 않음
    committed_files = _git(wt, "diff", "--name-only", f"{base}..HEAD").splitlines()
    assert committed_files == ["app.py"]

    # 워크트리의 RESULT.md 파일은 온전히 보존됨
    assert (wt / "RESULT.md").exists()
    assert "-async def fake_employee_lock" in (wt / "RESULT.md").read_text(encoding="utf-8")

    # result_md_excluded 이벤트 기록 확인
    assert events.exists()
    event_lines = events.read_text(encoding="utf-8").splitlines()
    res_event = next((l for l in event_lines if l.startswith("result_md_excluded|info|")), None)
    assert res_event is not None
    assert '"RESULT.md":' in res_event
