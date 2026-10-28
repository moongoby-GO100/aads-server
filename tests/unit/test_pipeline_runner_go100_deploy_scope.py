"""GO100 배포 범위 가드 (AADS-RUNNER-GO100-DEPLOY-SCOPE-GUARD-20261028).

2026-10-28 runner-a2a07712: .gitignore + docs 2파일 커밋인데 승인 후 `systemctl restart go100` 이 돌았고
(재기동 금지 구간 08:50~09:10 에 약 100초 API 중단), 프론트는 73파일 변경으로 오판돼 장중 게이트에 막혀
error 로 끝났다. 스크립트에서 실제 함수·판정 블록을 떼어 bash 로 실행해 검증한다.
"""

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KST = timezone(timedelta(hours=9))


def _read(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _epoch(hhmm: str, day: str = "2026-10-28") -> int:
    dt = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=KST)
    return int(dt.timestamp())


@pytest.fixture(scope="module", autouse=True)
def _need_tools():
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory):
    script = _read()
    body = "set -eo pipefail\n"
    for name in (
        "runner_market_hours_now_epoch",
        "deploy_changed_files",
        "deploy_files_docs_only",
        "go100_restart_freeze_window",
        "go100_fe_failure_label",
        "_go100_fe_out_has",
        "_go100_fe_out_intraday",
        "go100_fe_bg_classify",
        "go100_fe_failure_chat_text",
    ):
        body += _extract_function(script, name)
    path = tmp_path_factory.mktemp("scope") / "fn.sh"
    path.write_text(body, encoding="utf-8")
    return path


def _bash(fn_file, call: str, env: dict | None = None):
    import os

    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; {call}'],
        capture_output=True, text=True, timeout=30, env=e,
    )


# ── 실제 git 저장소 ──────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "wt"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, {"backend/app.py": "a=1\n", "frontend/page.tsx": "x\n", "README.md": "r\n"}, "base")
    return r


def _changed(fn_file, repo, pre, cur):
    r = _bash(fn_file, f'deploy_changed_files "{repo}" "{pre}" "{cur}"')
    return r.returncode, r.stdout


# ── 1) 변경 파일 계산은 push SHA 기준 ─────────────────────────────────────


def test_changed_files_uses_pre_and_current_sha(fn_file, repo):
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {"backend/app.py": "a=2\n"}, "be")
    rc, out = _changed(fn_file, repo, pre, cur)
    assert rc == 0
    assert out.split() == ["backend/app.py"]


def test_empty_pre_sha_falls_back_to_parent_of_current(fn_file, repo):
    cur = _commit(repo, {"docs/a.md": "d\n"}, "doc")
    rc, out = _changed(fn_file, repo, "", cur)
    assert rc == 0
    assert out.split() == ["docs/a.md"]


def test_unknown_sha_reports_failure_not_empty_success(fn_file, repo):
    rc, _ = _changed(fn_file, repo, "deadbeef" * 5, "cafebabe" * 5)
    assert rc != 0


def test_korean_doc_filename_is_not_quoted(fn_file, repo):
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {"docs/reports/20261020_GO100_AAG운영검증.md": "x\n"}, "ko")
    rc, out = _changed(fn_file, repo, pre, cur)
    assert rc == 0
    assert out.strip() == "docs/reports/20261020_GO100_AAG운영검증.md"
    assert '"' not in out and "\\3" not in out


# ── 2) docs-only 판정 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "files,expected",
    [
        (".gitignore\ndocs/reports/a.md\ndocs/x/y/z.txt", 0),
        ("README.md\nbackend/NOTES.md", 0),
        ("docs/reports/20261020_GO100_AAG운영검증.md", 0),
        ("docs/a.md\nbackend/app.py", 1),
        ("frontend/page.tsx", 1),
        ("scripts/deploy.sh", 1),
        ("docs_extra/run.py", 1),
        ("", 1),
    ],
)
def test_docs_only_classification(fn_file, files, expected):
    r = _bash(fn_file, f'printf %s "$FILES" | deploy_files_docs_only', env={"FILES": files})
    assert r.returncode == expected


# ── 3) 재기동 금지 구간 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hhmm,frozen,window",
    [
        ("0849", False, ""),
        ("0850", True, "0850-0910"),
        ("0909", True, "0850-0910"),
        ("0910", False, ""),
        ("1000", False, ""),
        ("1514", False, ""),
        ("1515", True, "1515-1530"),
        ("1529", True, "1515-1530"),
        ("1530", False, ""),
    ],
)
def test_restart_freeze_window_kst(fn_file, hhmm, frozen, window):
    r = _bash(fn_file, "go100_restart_freeze_window",
              env={"RUNNER_MARKET_HOURS_TEST_NOW_EPOCH": str(_epoch(hhmm))})
    assert (r.returncode == 0) is frozen
    assert r.stdout == window


def test_freeze_window_is_kst_not_host_tz(fn_file):
    # 08:55 KST = 23:55 UTC 전날. 호스트 TZ 가 UTC 여도 KST 로 판정해야 한다.
    r = _bash(fn_file, "go100_restart_freeze_window",
              env={"RUNNER_MARKET_HOURS_TEST_NOW_EPOCH": str(_epoch("0855")), "TZ": "UTC"})
    assert r.returncode == 0 and r.stdout == "0850-0910"


def test_freeze_window_env_override(fn_file):
    r = _bash(fn_file, "go100_restart_freeze_window",
              env={"RUNNER_MARKET_HOURS_TEST_NOW_EPOCH": str(_epoch("1100")),
                   "GO100_RESTART_FREEZE_WINDOWS": "1050-1110"})
    assert r.returncode == 0 and r.stdout == "1050-1110"


# ── 4) 장중 프론트 게이트는 error 가 아니라 보류 ──────────────────────────


def _classify(fn_file, tmp_path, text: str, rc: int = 1, env: dict | None = None) -> str:
    out = tmp_path / "bg.out"
    out.write_text(text, encoding="utf-8")
    r = _bash(fn_file, f'go100_fe_bg_classify {rc} "{out}"', env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_intraday_gate_is_deferred_not_gate_blocked(fn_file, tmp_path):
    text = "[BG-DEPLOY] ❌ Deploy Gate 차단 — 장중 프론트 배포 금지 구간\n"
    assert _classify(fn_file, tmp_path, text) == "go100-frontend:deploy_deferred_intraday"


def test_dirty_gate_stays_deploy_gate_blocked(fn_file, tmp_path):
    text = "[BG-DEPLOY] ❌ Deploy Gate 차단 — 커밋 후 재시도\n M frontend/장중_패널.tsx\n"
    assert _classify(fn_file, tmp_path, text) == "go100-frontend:deploy_gate_blocked"


def test_build_failure_is_still_build_failed(fn_file, tmp_path):
    assert _classify(fn_file, tmp_path, "[BG-DEPLOY]   ❌ 빌드 실패 — 장중\n") == "go100-frontend:build_failed"


def test_intraday_pattern_overridable(fn_file, tmp_path):
    text = "[BG-DEPLOY] ❌ Deploy Gate 차단 — freeze window\n"
    assert _classify(fn_file, tmp_path, text) == "go100-frontend:deploy_gate_blocked"
    assert _classify(fn_file, tmp_path, text, env={"GO100_FE_INTRADAY_GATE_PATTERN": "freeze window"}) \
        == "go100-frontend:deploy_deferred_intraday"


def test_deferred_label_chat_text_says_not_an_error(fn_file):
    r = _bash(fn_file, 'go100_fe_failure_chat_text go100-frontend:deploy_deferred_intraday 1')
    assert "보류" in r.stdout and "오류 아님" in r.stdout


# ── 5) 실제 결정 블록 실행: docs-only → 재기동 0회, backend + 금지 구간 → 보류 ──


def _decision_harness(tmp_path, wt: Path, pre: str, cur: str, hhmm: str, project: str = "GO100"):
    script = _read()
    d_start = script.index('    local _deploy_deferred="" _deploy_files=""')
    d_end = script.index('    case "$project" in\n        AADS)', d_start)
    docs_block = script[d_start:d_end]
    a_start = script.index('            local _go100_be_changed="true"')
    a_end = script.index("            # GO100 Frontend: npm build", a_start)
    api_block = script[a_start:a_end]

    helpers = "set -eo pipefail\n"
    for name in ("runner_market_hours_now_epoch", "deploy_changed_files",
                 "deploy_files_docs_only", "go100_restart_freeze_window"):
        helpers += _extract_function(script, name)

    sysl = tmp_path / "systemctl.log"
    dbl = tmp_path / "db.log"
    body = f"""{helpers}
log() {{ echo "LOG $*" >> "{tmp_path}/run.log"; }}
db_update() {{ echo "$*" >> "{dbl}"; }}
record_runner_event() {{ :; }}
runner_handover_record() {{ :; }}
post_to_chat() {{ :; }}
_release_deploy_lock() {{ :; }}
_notify_ai() {{ :; }}
promote_next_queued() {{ :; }}
systemctl() {{ echo "$*" >> "{sysl}"; }}
scope() {{
    local project={project} job_id=runner-t session_id=s worktree_dir="{wt}" _pre_sha="{pre}" current_sha="{cur}"
{docs_block}
    echo REACHED_CASE
    {api_block}
    echo "DEFERRED=$_deploy_deferred"
}}
scope
"""
    f = tmp_path / "harness.sh"
    f.write_text(body, encoding="utf-8")
    import os

    env = dict(os.environ)
    env["RUNNER_MARKET_HOURS_TEST_NOW_EPOCH"] = str(_epoch(hhmm))
    r = subprocess.run(["bash", str(f)], capture_output=True, text=True, timeout=30, env=env)
    restarts = sysl.read_text().count("restart go100") if sysl.exists() else 0
    db = dbl.read_text() if dbl.exists() else ""
    runlog = (tmp_path / "run.log").read_text() if (tmp_path / "run.log").exists() else ""
    return r, restarts, db, runlog


def test_docs_only_commit_never_restarts_and_closes_done(repo, tmp_path):
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {".gitignore": "x\n", "docs/reports/a.md": "d\n"}, "docs")
    r, restarts, db, runlog = _decision_harness(tmp_path, repo, pre, cur, "1000")
    assert r.returncode == 0, r.stderr
    assert restarts == 0
    assert "REACHED_CASE" not in r.stdout
    assert "DEPLOY_SKIP docs_only" in runlog
    assert "status='done'" in db and "deploy_skip_docs_only" in db


def test_docs_only_inside_freeze_window_also_zero_restarts(repo, tmp_path):
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {"docs/a.md": "d\n"}, "docs")
    r, restarts, _, _ = _decision_harness(tmp_path, repo, pre, cur, "0900")
    assert r.returncode == 0 and restarts == 0


def test_backend_change_in_freeze_window_is_deferred_without_restart(repo, tmp_path):
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {"backend/app.py": "a=2\n"}, "be")
    r, restarts, _, runlog = _decision_harness(tmp_path, repo, pre, cur, "0900")
    assert r.returncode == 0, r.stderr
    assert restarts == 0
    assert "REACHED_CASE" in r.stdout
    assert "DEFERRED=go100-api:restart_window_0850-0910" in r.stdout
    assert "DEPLOY_DEFER go100 api restart" in runlog


def test_backend_change_outside_freeze_window_restarts_once(repo, tmp_path):
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {"backend/app.py": "a=2\n"}, "be")
    r, restarts, _, _ = _decision_harness(tmp_path, repo, pre, cur, "1100")
    assert r.returncode == 0, r.stderr
    assert restarts == 1
    assert "DEFERRED=\n" in r.stdout + "\n"


def test_non_backend_non_docs_change_skips_api_restart(repo, tmp_path):
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {"frontend/page.tsx": "y\n"}, "fe")
    r, restarts, _, runlog = _decision_harness(tmp_path, repo, pre, cur, "1100")
    assert r.returncode == 0, r.stderr
    assert restarts == 0
    assert "SKIP go100 api restart" in runlog


def test_unknown_change_list_restarts_outside_window_and_defers_inside(repo, tmp_path):
    bad_pre, bad_cur = "deadbeef" * 5, "cafebabe" * 5
    r, restarts, _, _ = _decision_harness(tmp_path, repo, bad_pre, bad_cur, "1100")
    assert r.returncode == 0 and restarts == 1
    t2 = tmp_path / "inwin"
    t2.mkdir()
    r, restarts, _, _ = _decision_harness(t2, repo, bad_pre, bad_cur, "1520")
    assert r.returncode == 0 and restarts == 0
    assert "DEFERRED=go100-api:restart_window_1515-1530" in r.stdout


def test_non_go100_project_is_untouched_by_docs_only_gate(repo, tmp_path):
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {"docs/a.md": "d\n"}, "docs")
    r, _, db, _ = _decision_harness(tmp_path, repo, pre, cur, "1000", project="KIS")
    assert "REACHED_CASE" in r.stdout
    assert "deploy_skip_docs_only" not in db


# ── 6) 프론트 변경 감지: frontend 미변경 커밋 → 빈값 (GO100·NTV2 공통 방식) ──


def test_frontend_unchanged_commit_yields_empty_fe_changed_even_if_runtime_head_diverged(repo, tmp_path):
    runtime = tmp_path / "runtime"
    subprocess.run(["git", "clone", "-q", str(repo), str(runtime)], check=True)
    # 런타임 checkout 은 frontend 를 건드린 로컬 커밋을 품은 채 갈라져 있다
    _commit(runtime, {"frontend/local.tsx": "z\n"}, "runtime-only")
    pre = _git(repo, "rev-parse", "HEAD")
    cur = _commit(repo, {"backend/app.py": "a=3\n"}, "be-only")
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", pre, cur, "--name-only", "--", "frontend/"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert out.strip() == ""


# ── 7) 스크립트 계약 ─────────────────────────────────────────────────────


def _ntv2_block(script: str) -> str:
    start = script.index("        NTV2)\n            # NTV2 Laravel")
    end = script.index("    esac", start)
    return script[start:end]


def _go100_block(script: str) -> str:
    start = script.index("        GO100)\n            # GO100 API: systemd")
    end = script.index("        SF)\n", start)
    return script[start:end]


def test_ntv2_frontend_detection_never_reads_runtime_head():
    block = _ntv2_block(_read())
    assert "git -C /srv/newtalk-v2 diff" not in block
    assert 'git -C "$worktree_dir" diff "$_pre_sha" "$current_sha" --name-only -- frontend/' in block
    assert 'git -C "$worktree_dir" diff "${current_sha}^" "$current_sha" --name-only -- frontend/' in block


def test_go100_api_restart_is_conditional_and_only_once_in_block():
    block = _go100_block(_read())
    assert block.count("systemctl restart go100") == 1
    before = block[: block.index("systemctl restart go100")]
    assert "go100_restart_freeze_window" in before
    assert "_go100_be_changed" in before
    assert "git -C /root/kis-autotrade-v4 diff" not in block


def test_deferred_state_is_not_routed_through_build_fail():
    block = _go100_block(_read())
    i = block.index("deploy_deferred_intraday")
    assert "_deploy_deferred=" in block[i: i + 600]
    script = _read()
    j = script.index("elif [[ -n \"$_deploy_deferred\" ]]; then")
    tail = script[j: j + 900]
    assert "status='done', phase='deploy_deferred_intraday'" in tail
    assert "status='error'" not in tail


def test_deferred_branch_follows_build_fail_branch():
    script = _read()
    assert script.index('elif [[ -n "$_build_fail" ]]; then\n        db_update') \
        < script.index('elif [[ -n "$_deploy_deferred" ]]; then')


def test_runner_scripts_stay_byte_identical():
    assert _read("pipeline-runner.sh") == _read("pipeline-runner.sh.local")
