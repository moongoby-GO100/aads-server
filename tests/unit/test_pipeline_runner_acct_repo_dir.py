"""ACCT 저장소 경로는 호스트 설정값(AADS_ACCT_REPO_DIR)으로 받는다 — 구 서버 고정경로 제거."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts/pipeline-runner.sh"
LOCAL = ROOT / "scripts/pipeline-runner.sh.local"


def _block(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i) + len(end)]


def _map_block(text: str) -> str:
    return _block(text, "declare -A PROJECT_WORKDIR=(", "\n)\n")


def _func(text: str) -> str:
    return _block(text, "acct_repo_ready() {", "\n}\n")


def _run(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
    )


def _acct_dir(env: dict) -> str:
    text = SH.read_text(encoding="utf-8")
    out = _run(f'{_map_block(text)}\nprintf %s "${{PROJECT_WORKDIR[ACCT]}}"', env)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_acct_workdir_empty_when_unset():
    assert _acct_dir({}) == ""


def test_acct_workdir_uses_configured_path(tmp_path):
    assert _acct_dir({"AADS_ACCT_REPO_DIR": str(tmp_path)}) == str(tmp_path)


def test_acct_repo_ready_rejects_empty_and_non_git(tmp_path):
    func = _func(SH.read_text(encoding="utf-8"))
    assert _run(f'{func}\nacct_repo_ready ACCT ""').returncode == 1
    assert _run(f'{func}\nacct_repo_ready ACCT "{tmp_path}"').returncode == 1
    assert _run(f'{func}\nacct_repo_ready ACCT "{tmp_path}/missing"').returncode == 1


def test_acct_repo_ready_accepts_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    func = _func(SH.read_text(encoding="utf-8"))
    assert _run(f'{func}\nacct_repo_ready ACCT "{tmp_path}"').returncode == 0


def test_acct_repo_ready_ignores_other_projects():
    func = _func(SH.read_text(encoding="utf-8"))
    assert _run(f'{func}\nacct_repo_ready AADS ""').returncode == 0


def test_run_job_fails_acct_before_lock_with_expected_error():
    text = SH.read_text(encoding="utf-8")
    body = text[text.index("\nrun_job() {") :]
    guard = body.index("acct_repo_ready")
    assert guard < body.index("/ops/locks/work/acquire")
    snippet = body[guard : guard + 400]
    assert "acct_repo_not_configured" in snippet
    assert "AADS_ACCT_REPO_DIR" in snippet


def test_sh_and_local_are_identical_for_acct_blocks():
    a, b = SH.read_text(encoding="utf-8"), LOCAL.read_text(encoding="utf-8")
    assert _map_block(a) == _map_block(b)
    assert _func(a) == _func(b)


def test_no_legacy_acct_server_strings():
    for path in (SH, LOCAL, ROOT / "app/api/pipeline_runner.py", ROOT / "app/services/tool_registry.py"):
        text = path.read_text(encoding="utf-8")
        assert "jinah244" not in text, path
        assert "회계비서" not in text, path


def test_acct_host_fallback_and_tool_description():
    api = (ROOT / "app/api/pipeline_runner.py").read_text(encoding="utf-8")
    assert re.search(r'_PROJECT_HOST_FALLBACK = \{[^}]*"ACCT": "cafe24_114"', api, re.S)
    reg = (ROOT / "app/services/tool_registry.py").read_text(encoding="utf-8")
    assert "ACCT→cafe24_114 (aads-server 저장소, AADS_ACCT_REPO_DIR)" in reg


# ── ACCT → aads-server 저장소 push-only 릴리스 (deploy_job 실제 실행) ─────────────
import os  # noqa: E402
import pytest  # noqa: E402

_REAL_FUNCS = [
    "acct_repo_is_aads_server", "acct_release_via_aads", "deploy_job", "get_job_status",
    "classify_push_state", "attempt_stale_base_rebase", "instruction_forbids_deploy",
    "already_present_decision", "is_deploy_only_instruction",
]
_ORIGIN_RE = r"/aads-server\.git$"
_FAKE_DB = r'''
log() { :; }
sql_escape() { printf "'%s'" "${1//\'/\'\'}"; }
db_update() { db_exec "$1" >/dev/null || true; }
db_exec() {
    local sql="$1" cur new row
    printf '%s\n----\n' "$sql" >> "$T/sql.log"
    cur=$(cat "$T/status")
    if [[ "$sql" == "SELECT COALESCE(commit_hash"* ]]; then cat "$T/hash"; return 0; fi
    if [[ "$sql" == "SELECT status FROM"* ]]; then cat "$T/status"; return 0; fi
    if [[ "$sql" == "UPDATE pipeline_jobs SET commit_hash='"* ]]; then
        [[ "$sql" =~ SET\ commit_hash=\'([0-9a-f]+)\' ]] && printf '%s' "${BASH_REMATCH[1]}" > "$T/hash"
        return 0
    fi
    if [[ "$sql" =~ SET\ status=\'([a-z_]+)\' ]]; then
        new="${BASH_REMATCH[1]}"
        if [[ "$new" == done && -f "$T/cancel_before_done" ]]; then printf cancelled > "$T/status"; cur=cancelled; fi
        if [[ "$sql" == *"AND status='deploying'"* && "$cur" != deploying ]]; then return 0; fi
        if [[ "$sql" =~ AND\ commit_hash=\'([0-9a-f]+)\' && "$(cat "$T/hash")" != "${BASH_REMATCH[1]}" ]]; then return 0; fi
        if [[ "$sql" == *"status='error'"* && "$sql" != *"AND status="* ]]; then echo UNCONDITIONAL_ERROR >> "$T/flags"; fi
        printf '%s' "$new" > "$T/status"
        [[ "$new" == done && -f "$T/cancel_after_done" ]] && printf cancelled > "$T/status"
        [[ "$sql" == *RETURNING* ]] && echo job-1
    fi
    return 0
}
record_runner_event() { printf '%s|%s|%s\n' "$2" "$4" "${9:-}" >> "$T/events"; }
_fail_job() {
    printf '%s' "$4" >> "$T/fail"; printf '%s\n' "$3" >> "$T/failtype"
    printf error > "$T/status"
}
post_to_chat() { :; }
_notify_ai() { :; }
runner_handover_record() { :; }
promote_next_queued() { :; }
_release_deploy_lock() { echo released >> "$T/flags"; }
acquire_deploy_lock_with_requeue() { return 0; }
record_git_diagnostics() { :; }
is_aads_dashboard_instruction() { return 1; }
resolve_project_workdir() { printf '%s\n' "$MAIN"; }
get_job_instruction() { cat "$T/instruction"; }
read_job_instruction_strict() { cat "$T/instruction"; }
deploy_git_preflight() { return 0; }
deploy_isolated_git_preflight() {
    DEPLOY_ISOLATED_EFFECTIVE_SHA="$6"; git -C "$4" worktree add -q --detach "$5" "$6"
}
approved_sha_is_live() { return 1; }
ensure_approved_job_worktree() { git -C "$3" worktree add -q --detach "$2" "$4"; }
runner_sync_live_workdir() { echo reached_existing_nonaads_path >> "$T/reached"; exit 0; }
deploy_changed_files() { echo files >> "$T/forbidden"; }
docker() { echo "docker $*" >> "$T/forbidden"; }
ssh() { echo "ssh $*" >> "$T/forbidden"; }
curl() { echo "curl $*" >> "$T/forbidden"; }
'''


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


class _Env:
    def __init__(self, tmp: Path, origin_name: str, advance: str):
        self.t = tmp / "t"
        self.t.mkdir()
        self.origin = tmp / origin_name
        self.main = tmp / "main"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.main)], check=True, capture_output=True)
        (self.main / "base.txt").write_text("base\n")
        _git(self.main, "add", "-A")
        _git(self.main, "commit", "-qm", "base")
        _git(self.main, "push", "-q", "origin", "HEAD:refs/heads/main")
        base = _git(self.main, "rev-parse", "HEAD")
        (self.main / "job.txt").write_text("job\n")
        _git(self.main, "add", "-A")
        _git(self.main, "commit", "-qm", "job change")
        self.sha = _git(self.main, "rev-parse", "HEAD")
        _git(self.main, "reset", "-q", "--hard", base)
        if advance == "other_file":
            # 다른 작업이 먼저 push — 승인 SHA 의 base 가 낡아진다(stale_base)
            (self.main / "other.txt").write_text("other\n")
            _git(self.main, "add", "-A")
            _git(self.main, "commit", "-qm", "other")
            _git(self.main, "push", "-q", "origin", "HEAD:refs/heads/main")
        elif advance == "already_present":
            _git(self.main, "merge", "-q", "--ff-only", self.sha)
            (self.main / "later.txt").write_text("later\n")
            _git(self.main, "add", "-A")
            _git(self.main, "commit", "-qm", "later")
            _git(self.main, "push", "-q", "origin", "HEAD:refs/heads/main")
        else:
            _git(self.main, "fetch", "-q", "origin")
        # 승인 SHA 객체는 main 저장소에 이미 있어야 worktree 스텁이 만들 수 있다
        _git(self.main, "cat-file", "-e", self.sha + "^{commit}")
        (self.t / "status").write_text("deploying")
        (self.t / "hash").write_text(self.sha)
        (self.t / "instruction").write_text("ACCT 코드작업\nTARGET: /root/aads/aads-server\n")
        self.worktree = Path(f"/tmp/aads-wt-job-1")

    def read(self, name: str) -> str:
        p = self.t / name
        return p.read_text() if p.exists() else ""

    def run(self, project: str, extra_env: dict | None = None, setup: str = "") -> subprocess.CompletedProcess:
        text = SH.read_text(encoding="utf-8")
        funcs = "\n".join(_block(text, f"\n{n}() {{", "\n}\n") for n in _REAL_FUNCS)
        script = (
            "set -eo pipefail\n"
            f'export T="{self.t}" MAIN="{self.main}" AADS_SERVER_ORIGIN_RE=\'{_ORIGIN_RE}\'\n'
            f"{_FAKE_DB}\n{funcs}\n{setup}\n"
            f'rc=0; deploy_job job-1 {project} sess-1 || rc=$?\nprintf "RC=%s\\n" "$rc"\n'
        )
        subprocess.run(["git", "-C", str(self.main), "worktree", "prune"], check=False)
        subprocess.run(["rm", "-rf", str(self.worktree)], check=False)
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(self.t),
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                **(extra_env or {}),
            },
        )


@pytest.fixture
def mkenv(tmp_path):
    made = []

    def _make(origin_name="aads-server.git", advance="none"):
        e = _Env(tmp_path, origin_name, advance)
        made.append(e)
        return e

    yield _make
    for e in made:
        subprocess.run(["git", "-C", str(e.main), "worktree", "prune"], check=False)
        subprocess.run(["rm", "-rf", str(e.worktree)], check=False)


def test_acct_aads_server_origin_pushes_and_finishes_without_build(mkenv):
    e = mkenv()
    out = e.run("ACCT")
    assert "RC=0" in out.stdout, out.stdout + out.stderr
    assert e.read("status") == "done"
    assert "acct_release_via_aads_bluegreen" in e.read("events")
    assert e.read("fail") == ""
    assert e.read("forbidden") == ""
    assert _git(e.origin, "rev-parse", "refs/heads/main") == e.sha


def test_acct_already_present_with_advanced_main_does_not_fail_unverified(mkenv):
    e = mkenv(advance="already_present")
    out = e.run("ACCT")
    assert "RC=0" in out.stdout, out.stdout + out.stderr
    assert e.read("status") == "done"
    assert "deploy_already_present_unverified" not in e.read("failtype")
    assert e.read("forbidden") == ""


def test_acct_completion_race_cancelled_before_update_is_not_overwritten(mkenv):
    e = mkenv()
    (e.t / "cancel_before_done").write_text("1")
    out = e.run("ACCT")
    assert "RC=1" in out.stdout, out.stdout + out.stderr
    assert e.read("status") == "cancelled"
    assert "UNCONDITIONAL_ERROR" not in e.read("flags")
    assert "job_terminal|done" not in e.read("events")


def test_acct_cancel_right_after_done_update_is_not_overwritten_by_failure_path(mkenv):
    e = mkenv()
    (e.t / "cancel_after_done").write_text("1")
    out = e.run("ACCT")
    assert "RC=1" in out.stdout, out.stdout + out.stderr
    assert e.read("status") == "cancelled"
    assert "UNCONDITIONAL_ERROR" not in e.read("flags")
    assert "job_terminal|error" not in e.read("events")


def test_acct_persist_failure_while_deploying_ends_in_error(mkenv):
    e = mkenv()
    # worktree 준비 뒤 DB commit_hash 가 push 한 SHA 와 달라지면 완료 UPDATE 가 거부되고 deploying 인 채로 error 가 된다
    setup = (
        'ensure_approved_job_worktree() { git -C "$3" worktree add -q --detach "$2" "$4"; printf "%040d" 0 > "$T/hash"; }'
    )
    out = e.run("ACCT", setup=setup)
    assert "RC=1" in out.stdout, out.stdout + out.stderr
    assert e.read("status") == "error"
    assert "acct_release_persist_failed" in e.read("sql.log"), e.read("failtype") + out.stderr
    assert "job_terminal|error" in e.read("events")


def test_acct_release_after_auto_rebase_uses_rebased_sha(mkenv):
    e = mkenv(advance="other_file")
    out = e.run("ACCT")
    assert "RC=0" in out.stdout, out.stdout + out.stderr
    assert e.read("status") == "done"
    pushed = _git(e.origin, "rev-parse", "refs/heads/main")
    assert pushed != e.sha
    assert e.read("hash") == pushed
    assert "acct_release_via_aads_bluegreen" in e.read("events")
    assert e.read("forbidden") == ""


def test_aads_job_still_runs_already_present_verification(mkenv):
    e = mkenv(advance="already_present")
    (e.t / "instruction").write_text("AADS 작업\nTARGET: /root/aads/aads-server\n")
    out = e.run("AADS")
    assert "deploy_already_present_unverified" in e.read("failtype"), out.stdout + out.stderr
    assert e.read("status") == "error"


def test_acct_non_aads_server_origin_keeps_existing_path(mkenv):
    e = mkenv(origin_name="biseo.git")
    out = e.run("ACCT")
    assert "acct_release_via_aads" not in e.read("events")
    assert "acct_release_via_aads" not in e.read("sql.log")
    assert "reached_existing_nonaads_path" in e.read("reached"), out.stderr
    assert e.read("status") == "deploying"


def _rescue_block(text: str) -> str:
    start = text.index('    if [[ -z "${_new_untracked//[[:space:]]/}" && -n "${worktree_dir:-}"')
    return text[start : text.index("\n    fi\n", start) + len("\n    fi\n")]


@pytest.mark.parametrize("origin_name,expect_rescue", [("biseo.git", True), ("aads-server.git", False)])
def test_whitelist_rescue_runs_only_for_non_aads_server_origin(tmp_path, origin_name, expect_rescue):
    origin = tmp_path / origin_name
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    (repo / ".gitignore").write_text("*\n!.gitignore\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "init")
    (repo / ".git").stat()
    marker = repo / "new_output.txt"
    marker.write_text("x\n")
    os.utime(repo / ".git", (1, 1))
    text = SH.read_text(encoding="utf-8")
    funcs = "\n".join(_block(text, f"\n{n}() {{", "\n}\n") for n in ["acct_repo_is_aads_server"])
    script = f'''
set -eo pipefail
export AADS_SERVER_ORIGIN_RE='{_ORIGIN_RE}'
log() {{ echo "$*" >> "{tmp_path}/log"; }}
_intent_to_add_nul() {{ cat >/dev/null; echo called > "{tmp_path}/rescued"; }}
{funcs}
rescue() {{
    local project=ACCT main_workdir="{repo}" worktree_dir="{repo}" _new_untracked="" _whitelist_ignore_repo=1
    cd "{repo}"
{_rescue_block(text)}
}}
rescue
'''
    out = _run(script, {"HOME": str(tmp_path)})
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "rescued").exists() is expect_rescue


def test_acct_repo_not_configured_when_dir_unset():
    text = SH.read_text(encoding="utf-8")
    out = _run(
        f'{_map_block(text)}\n{_func(text)}\n'
        'acct_repo_ready ACCT "${PROJECT_WORKDIR[ACCT]}"; echo "rc=$?"'
    )
    assert out.stdout.strip().endswith("rc=1")
    assert 'acct_repo_ready "$project" "$workdir"' in text
