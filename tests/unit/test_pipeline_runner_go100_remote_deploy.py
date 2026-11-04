"""GO100 배포·재기동 원격 실행 경로 (AADS-RUNNER-GO100-HOST-CONTABO92-PHASE1-20261104).

작업은 contabo92 에서 하고 재기동·프론트 BG 배포·헬스체크는 실매매 호스트(contabo14)에서
`ssh contabo14 bash -s` 로 돌리기 위한 선택 경로다. GO100_DEPLOY_SSH_HOST 가 비어 있으면(기본)
지금과 똑같이 로컬에서 실행해야 한다 — 이것이 첫째 회귀 조건이다.

스크립트에서 실제 함수 구간을 떼어 bash 로 실행한다. ssh 는 GO100_DEPLOY_SSH_BIN 으로 가짜를 주입한다.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pipeline-runner.sh"


@pytest.fixture(scope="module", autouse=True)
def _need_tools():
    if shutil.which("bash") is None or shutil.which("timeout") is None:
        pytest.skip("bash/timeout 미설치 환경")


def _region() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("go100_active_frontend_color() {")
    end = text.index("# ── 에러 분류", start)
    return text[start:end]


@pytest.fixture()
def env(tmp_path):
    lib = tmp_path / "fn.sh"
    lib.write_text(
        "set -eo pipefail\n"
        'log() { echo "[local] $*"; }\n'
        + _region(),
        encoding="utf-8",
    )
    calls = tmp_path / "ssh_calls"
    fake_ssh = tmp_path / "fake_ssh"
    # 호스트·옵션은 기록만 하고 stdin 의 스크립트를 깨끗한 환경의 bash 로 실행한다(원격 흉내).
    fake_ssh.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$FAKE_SSH_CALLS"\n'
        '[[ -n "${FAKE_SSH_SLEEP:-}" ]] && sleep "$FAKE_SSH_SLEEP"\n'
        '[[ -n "${FAKE_SSH_RC:-}" ]] && exit "$FAKE_SSH_RC"\n'
        'exec env -i PATH="$PATH" HOME="$HOME" bash -s\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    logdir = tmp_path / "logs"
    logdir.mkdir()
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "LOG_DIR": str(logdir),
        "FAKE_SSH_CALLS": str(calls),
        "GO100_DEPLOY_SSH_BIN": str(fake_ssh),
    }
    return {"lib": lib, "calls": calls, "logdir": logdir, "base": base, "tmp": tmp_path}


def _run(env, body: str, extra=None, timeout=30):
    e = dict(env["base"])
    e.update(extra or {})
    script = f'source "{env["lib"]}"\n{body}\n'
    return subprocess.run(
        ["bash", "-c", script], env=e, capture_output=True, text=True, timeout=timeout
    )


def test_default_empty_host_runs_locally_without_ssh(env):
    r = _run(
        env,
        'set +e; go100_deploy_call bash -c \'echo "local:$1"; exit 3\' _ "a b"; echo "rc=$?"',
    )
    assert "local:a b" in r.stdout
    assert "rc=3" in r.stdout
    assert not env["calls"].exists(), "호스트가 비어 있으면 ssh 를 절대 부르지 않는다"
    assert not (env["logdir"] / "runner.log").exists()


def test_default_is_remote_false_and_host_var_empty():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'GO100_DEPLOY_SSH_HOST="${GO100_DEPLOY_SSH_HOST:-}"' in text


def test_remote_runs_shipped_function_with_args_rc_and_env_allowlist(env):
    body = r"""
myfn() { printf 'arg1=%s arg2=%s\n' "$1" "$2"; echo "ticket=${GO100_INTRADAY_DEPLOY_TICKET:-}"; echo "skip=${GO100_GATE_SKIP:-}"; echo "repo=${GO100_REPO_DIR:-}"; echo "stdin=$(cat)"; echo err-line >&2; return 7; }
GO100_DEPLOY_REMOTE_FUNCS+=(myfn)
set +e
go100_deploy_call myfn "x y" 'q"$z'
echo "rc=$?"
"""
    r = _run(
        env,
        body,
        {
            "GO100_DEPLOY_SSH_HOST": "contabo14",
            "GO100_INTRADAY_DEPLOY_TICKET": "tok-1",
            "GO100_GATE_SKIP": "1",
            "GO100_REPO_DIR": "/root/kis-autotrade-v4",
        },
    )
    assert "arg1=x y arg2=q\"$z" in r.stdout
    assert "ticket=tok-1" in r.stdout, "장중 정책 변수는 그 명령에만 넘긴다"
    assert "skip=\n" in r.stdout, "비상 우회(GO100_GATE_SKIP)는 넘기지 않는다"
    assert "repo=/root/kis-autotrade-v4" in r.stdout
    assert "stdin=\n" in r.stdout, "명령이 stdin 을 읽어도 원격 스크립트를 먹지 않는다"
    assert "err-line" in r.stderr
    assert "rc=7" in r.stdout
    calls = env["calls"].read_text()
    assert "contabo14" in calls and "BatchMode=yes" in calls and "bash -s" in calls
    log = (env["logdir"] / "runner.log").read_text()
    assert "GO100_DEPLOY_REMOTE host=contabo14 rc=7 reason=remote_rc cmd=myfn" in log


def test_remote_real_function_roundtrip_active_color(env):
    conf = env["tmp"] / "go100.conf"
    conf.write_text(
        "upstream go100_frontend {\n    server 127.0.0.1:3001;\n    server 127.0.0.1:3000 backup;\n}\n",
        encoding="utf-8",
    )
    r = _run(
        env,
        f'go100_deploy_call go100_active_frontend_color "{conf}"',
        {"GO100_DEPLOY_SSH_HOST": "contabo14"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "green"


def test_remote_prune_log_goes_to_stderr_not_stdout(env):
    root = env["tmp"] / "opt"
    root.mkdir()
    r = _run(
        env,
        'GO100_DEPLOY_REMOTE_FUNCS+=(probe); probe() { log "from-remote"; echo out; }; go100_deploy_call probe',
        {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_RELEASE_WORKTREE_ROOT": str(root)},
    )
    assert r.stdout.strip().splitlines()[-1] == "out"
    assert "from-remote" in r.stderr
    assert "from-remote" not in r.stdout


def test_remote_timeout_and_ssh_failure_are_logged(env):
    r = _run(
        env,
        "set +e; GO100_DEPLOY_CALL_TIMEOUT=1 go100_deploy_call true; echo rc=$?",
        {"GO100_DEPLOY_SSH_HOST": "contabo14", "FAKE_SSH_SLEEP": "5"},
    )
    assert re.search(r"rc=(124|137)", r.stdout)
    log = (env["logdir"] / "runner.log").read_text()
    assert "reason=timeout_1s" in log

    r = _run(
        env,
        "set +e; go100_deploy_call true; echo rc=$?",
        {"GO100_DEPLOY_SSH_HOST": "contabo14", "FAKE_SSH_RC": "255"},
    )
    assert "rc=255" in r.stdout
    assert "reason=ssh_failed" in (env["logdir"] / "runner.log").read_text()


def _go100_case() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("        GO100)\n            # GO100 API: systemd")
    end = text.index("        SF)\n", start)
    return text[start:end]


def test_go100_deploy_steps_all_route_through_deploy_call():
    block = _go100_case()
    assert "go100_deploy_call systemctl restart go100 2>/dev/null || true" in block
    assert 'go100_deploy_call test -d "$_fe_dir"' in block
    assert 'go100_deploy_call test -f "$_fe_bg_script"' in block
    assert "go100_deploy_call go100_create_frontend_release_worktree" in block
    assert 'go100_deploy_call env GO100_RELEASE_WORKDIR="$_fe_release_dir" GO100_RUNTIME_WORKDIR="$GO100_REPO_DIR"' in block
    assert "go100_deploy_call verify_go100_bundle_fresh" in block
    assert block.count("go100_deploy_call go100_prune_release_worktrees") == 2
    # 재기동 금지 구간 판정은 원격 경로에서도 재기동보다 먼저 돈다.
    assert block.index("go100_restart_freeze_window") < block.index("go100_deploy_call systemctl restart go100")


def test_no_bare_go100_restart_left():
    text = SCRIPT.read_text(encoding="utf-8")
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if "systemctl restart go100" in s:
            assert "go100_deploy_call systemctl restart go100" in s, s


def test_backend_health_and_rollback_use_deploy_host_for_go100():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'if [[ "$project" == "GO100" ]] && go100_deploy_is_remote; then\n        _health_via=(go100_deploy_call)' in text
    assert text.count('${_health_via[@]+"${_health_via[@]}"} curl') == 2


def test_runner_local_copy_identical():
    assert SCRIPT.read_bytes() == (ROOT / "scripts" / "pipeline-runner.sh.local").read_bytes()


def test_remote_script_log_is_stderr_only_and_not_a_line_leading_definition(env):
    r = _run(
        env,
        'GO100_DEPLOY_SSH_HOST=contabo14; go100_deploy_remote_script true',
    )
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if "log()" in ln]
    assert len(lines) == 1, lines
    assert ">&2" in lines[0]
    assert "tee" not in lines[0] and "LOG_DIR" not in lines[0]
    assert "tee" not in r.stdout and "LOG_DIR" not in r.stdout
    # dup_guard(sh-dup-func)가 로컬 함수 재정의로 오탐하지 않도록 log() 는 줄 첫머리에 오지 않는다.
    assert not re.search(r"^\s*log\(\)", SCRIPT.read_text(encoding="utf-8").split("go100_deploy_remote_script() {", 1)[1].split("\n}\n", 1)[0], re.M)


def test_remote_script_log_runs_remotely_writing_stderr_only(env):
    r = _run(
        env,
        'set +e; GO100_DEPLOY_SSH_HOST=contabo14 go100_deploy_call eval \'log hello-remote; echo out\'; echo rc=$?',
    )
    assert "rc=0" in r.stdout
    assert "out" in r.stdout
    assert "hello-remote" not in r.stdout
    assert "hello-remote" in r.stderr
    assert not (env["logdir"] / "runner.log").exists() or "hello-remote" not in (env["logdir"] / "runner.log").read_text()


# ── R2: 통신 실패를 배포 실패로 처리하고 운영 호스트 코드 반영을 확인한다 ──────────────────
# (AADS-RUNNER-GO100-HOST-CONTABO92-PHASE1-20261104-R2, 리뷰 4161b815 지적 P1-a)

ZERO = "0" * 40


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


@pytest.fixture()
def runtime_repo(tmp_path):
    """운영 호스트의 /root/kis-autotrade-v4 흉내: HEAD 에 SHA 가 있는 저장소와 없는 SHA."""
    if shutil.which("git") is None:
        pytest.skip("git 미설치 환경")
    repo = tmp_path / "runtime"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    (repo / "backend").mkdir()
    (repo / "backend" / "app.py").write_text("v1\n")
    _git(repo, "add", "a.txt", "backend/app.py")
    _git(repo, "commit", "-q", "-m", "c1")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def fake(env):
    """스크립트를 저장하고, 조건에 따라 255/124 를 내거나 깨끗한 bash 로 실행하는 가짜 ssh 와 가짜 systemctl."""
    tmp = env["tmp"]
    scripts = tmp / "ssh_scripts"
    scripts.mkdir()
    bindir = tmp / "bin"
    bindir.mkdir()
    sysl = tmp / "systemctl.log"
    (bindir / "systemctl").write_text(
        '#!/bin/bash\necho "$*" >> "$FAKE_SYSTEMCTL_LOG"\nexit "${FAKE_SYSTEMCTL_RC:-0}"\n', encoding="utf-8"
    )
    (bindir / "systemctl").chmod(0o755)
    ssh = tmp / "fake_ssh2"
    # FAKE_FAIL_ON: 스크립트 마지막 줄이 이 정규식과 맞으면 FAKE_FAIL_RC 로 끝낸다. FAKE_FAIL_ONLY_FIRST=1 이면 첫 번째 맞는 호출에만.
    ssh.write_text(
        "#!/bin/bash\n"
        'n=$(ls "$FAKE_SSH_SCRIPTS" | wc -l); f="$FAKE_SSH_SCRIPTS/$((n+1))"; cat > "$f"\n'
        'last=$(tail -n 1 "$f")\n'
        'if [[ -n "${FAKE_FAIL_ON:-}" && "$last" =~ $FAKE_FAIL_ON ]]; then\n'
        '  if [[ -z "${FAKE_FAIL_ONLY_FIRST:-}" || ! -e "$f.failed_once" && -z "$(ls "$FAKE_SSH_SCRIPTS" | grep failed_once)" ]]; then\n'
        '    touch "$f.failed_once"; exit "${FAKE_FAIL_RC:-255}"\n'
        '  fi\n'
        'fi\n'
        'exec env -i PATH="$FAKE_BIN:$PATH" HOME="$HOME" FAKE_SYSTEMCTL_LOG="$FAKE_SYSTEMCTL_LOG" '
        'FAKE_SYSTEMCTL_RC="${FAKE_SYSTEMCTL_RC:-0}" bash "$f"\n',
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    base = dict(env["base"])
    base.update(
        {
            "GO100_DEPLOY_SSH_BIN": str(ssh),
            "FAKE_SSH_SCRIPTS": str(scripts),
            "FAKE_BIN": str(bindir),
            "FAKE_SYSTEMCTL_LOG": str(sysl),
            # 재기동 금지 구간 판정이 실행 시각에 좌우되지 않게 한다.
            "GO100_RESTART_FREEZE_WINDOWS": "9998-9999",
        }
    )
    return {"base": base, "scripts": scripts, "sysl": sysl, "tmp": tmp, "logdir": env["logdir"], "lib": env["lib"]}


def _scripts_text(fake) -> str:
    return "\n".join(p.read_text() for p in sorted(fake["scripts"].iterdir()) if p.is_file() and not p.name.endswith("failed_once"))


def _restart_sent(fake) -> bool:
    return bool(re.search(r"^systemctl restart go100", _scripts_text(fake), re.M))


def _run_block(fake, files: str, extra: dict, project_sha: str, timeout=60):
    """deploy_job 의 GO100) 분기를 그대로 떼어 스텁 위에서 실행한다. 마지막에 _build_fail·_deploy_deferred 를 출력."""
    block = _go100_case()
    tmp = fake["tmp"]
    chat = tmp / "chat.log"
    body = f"""source "{fake['lib']}"
set +e
source <(sed -n '/^runner_market_hours_now_epoch() {{/,/^}}/p' "{SCRIPT}")
db_update() {{ :; }}
post_to_chat() {{ echo "CHAT $2" >> "{chat}"; }}
fe_ran() {{ :; }}
runit() {{
    local project=GO100 job_id=runner-t session_id=s worktree_dir="{tmp}/nowt" _pre_sha="{ZERO}" current_sha="{project_sha}"
    local _build_fail="" _deploy_deferred="" _deploy_files="{files}" _deploy_files_ok=true
    case "$project" in
{block}
    esac
    echo "BUILD_FAIL=${{_build_fail}}"
    echo "DEFERRED=${{_deploy_deferred}}"
}}
runit
"""
    e = dict(fake["base"])
    e.update(extra)
    r = subprocess.run(["bash", "-c", body], env=e, capture_output=True, text=True, timeout=timeout)
    return r, (chat.read_text() if chat.exists() else "")


def _build_fail(r) -> str:
    m = re.search(r"^BUILD_FAIL=(.*)$", r.stdout, re.M)
    assert m, (r.stdout, r.stderr)
    return m.group(1)


def test_r2_first_test_d_255_is_deploy_failure_not_directory_absent(fake):
    # 리뷰어 재현: 첫 검사(test -d)만 255 → 예전에는 "디렉터리 없음"으로 프론트 배포를 건너뛰고 build_fail 이 비어 done 이 됐다.
    r, chat = _run_block(
        fake,
        "frontend/page.tsx",
        {
            "GO100_DEPLOY_SSH_HOST": "contabo14",
            "FAKE_FAIL_ON": r"^test -d ",
            "FAKE_FAIL_RC": "255",
            "FAKE_FAIL_ONLY_FIRST": "1",
        },
        "a" * 40,
    )
    assert r.returncode == 0, r.stderr
    bf = _build_fail(r)
    assert bf == "go100-frontend:ssh_failed", bf
    assert "배포 여부 불명" in chat
    # 최종 판정 블록: build_fail 이 비어 있지 않으면 done 이 아니라 error/build_fail 이다(health 통과와 무관).
    text = SCRIPT.read_text(encoding="utf-8")
    final = text[text.index("# 최종 판정: 빌드 실패 플래그가 있으면 성공 처리 금지") :]
    assert final.index('elif [[ -n "$_build_fail" ]]') < final.index("status='done', phase='done'")


def test_r2_test_d_124_is_timeout_and_rc1_is_plain_absent(fake):
    r, _ = _run_block(
        fake,
        "frontend/page.tsx",
        {"GO100_DEPLOY_SSH_HOST": "contabo14", "FAKE_FAIL_ON": r"^test -d ", "FAKE_FAIL_RC": "124"},
        "a" * 40,
    )
    assert _build_fail(r) == "go100-frontend:timeout"
    # 이 호스트에 /root/kis-autotrade-v4/frontend 가 없으면 rc=1(없음): 실패가 아니라 기존처럼 조용히 건너뛴다.
    if not Path("/root/kis-autotrade-v4/frontend").is_dir():
        r, _ = _run_block(fake, "frontend/page.tsx", {"GO100_DEPLOY_SSH_HOST": "contabo14"}, "a" * 40)
        assert _build_fail(r) == ""


@pytest.mark.parametrize("rc,label", [("255", "ssh_failed"), ("124", "timeout"), ("137", "timeout")])
def test_r2_remote_restart_comm_failure_is_recorded_as_failure(fake, runtime_repo, rc, label):
    repo, sha = runtime_repo
    r, chat = _run_block(
        fake,
        "backend/app.py",
        {
            "GO100_DEPLOY_SSH_HOST": "contabo14",
            "GO100_REPO_DIR": str(repo),
            "FAKE_FAIL_ON": r"^systemctl restart go100",
            "FAKE_FAIL_RC": rc,
        },
        sha,
    )
    assert r.returncode == 0, r.stderr
    assert f"go100-api:{label}" in _build_fail(r).split(";")
    assert "성공으로 처리하지 않습니다" in chat
    assert not fake["sysl"].exists(), "통신 실패면 원격 systemctl 은 실행되지 않았다"


def test_r2_remote_restart_nonzero_rc_is_restart_failed(fake, runtime_repo):
    repo, sha = runtime_repo
    r, _ = _run_block(
        fake,
        "backend/app.py",
        {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_REPO_DIR": str(repo), "FAKE_SYSTEMCTL_RC": "5"},
        sha,
    )
    assert "go100-api:restart_failed" in _build_fail(r).split(";")


def test_r2_remote_restart_success_when_runtime_contains_sha(fake, runtime_repo):
    repo, sha = runtime_repo
    r, _ = _run_block(
        fake, "backend/app.py", {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_REPO_DIR": str(repo)}, sha
    )
    assert r.returncode == 0, r.stderr
    assert "go100-api" not in _build_fail(r)
    assert fake["sysl"].read_text().strip() == "restart go100"
    # 재기동 전후 두 번 검증한다.
    assert _scripts_text(fake).count("go100_runtime_has_sha") >= 4  # 함수 정의 2회 + 호출 2회


def test_r2_sha_mismatch_blocks_restart_command_fail_closed(fake, runtime_repo):
    repo, _ = runtime_repo
    r, chat = _run_block(
        fake, "backend/app.py", {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_REPO_DIR": str(repo)}, "b" * 40
    )
    assert r.returncode == 0, r.stderr
    assert "go100-api:remote_sha_not_applied" in _build_fail(r).split(";")
    assert not _restart_sent(fake), "SHA 가 반영되지 않았으면 재기동 명령을 보내지 않는다"
    assert not fake["sysl"].exists()
    assert "미반영" in chat


def test_r2_sha_on_other_branch_not_ancestor_of_head_is_not_applied(fake, runtime_repo):
    repo, base = runtime_repo
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")  # 객체는 있지만 HEAD(main)의 조상이 아니다
    r, _ = _run_block(
        fake, "backend/app.py", {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_REPO_DIR": str(repo)}, side
    )
    assert "go100-api:remote_sha_not_applied" in _build_fail(r).split(";")
    assert not _restart_sent(fake)


def test_r2_sha_verification_comm_failure_blocks_restart(fake, runtime_repo):
    repo, sha = runtime_repo
    r, _ = _run_block(
        fake,
        "backend/app.py",
        {
            "GO100_DEPLOY_SSH_HOST": "contabo14",
            "GO100_REPO_DIR": str(repo),
            "FAKE_FAIL_ON": r"go100_runtime_has_sha ",
            "FAKE_FAIL_RC": "255",
        },
        sha,
    )
    assert "go100-api:ssh_failed" in _build_fail(r).split(";")
    assert not _restart_sent(fake)


def test_r2_runtime_has_sha_never_touches_the_working_tree(runtime_repo):
    # 읽기 전용 검증: 미커밋 변경이 있어도 그대로 남고, 전용 함수 본문에 변경 명령이 없다.
    repo, sha = runtime_repo
    (repo / "dirty.txt").write_text("uncommitted\n")
    (repo / "a.txt").write_text("changed\n")
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("go100_runtime_has_sha() {")
    body = text[start : text.index("\n}\n", start)]
    for forbidden in ("reset", "checkout", "pull", "fetch", "merge ", "clean", "stash", "rebase"):
        assert forbidden not in body, forbidden
    r = subprocess.run(
        ["bash", "-c", f'source <(sed -n "/^go100_runtime_has_sha() {{/,/^}}/p" "{SCRIPT}"); go100_runtime_has_sha "{repo}" {sha} backend/app.py; echo rc=$?'],
        capture_output=True, text=True, executable="/bin/bash",
    )
    assert "rc=0" in r.stdout, r.stderr
    assert (repo / "dirty.txt").read_text() == "uncommitted\n"
    assert (repo / "a.txt").read_text() == "changed\n"
    for arg, want in ((ZERO, 1), ("zz", 3)):
        r = subprocess.run(
            ["bash", "-c", f'source <(sed -n "/^go100_runtime_has_sha() {{/,/^}}/p" "{SCRIPT}"); go100_runtime_has_sha "{repo}" {arg} backend/app.py; echo rc=$?'],
            capture_output=True, text=True, executable="/bin/bash",
        )
        assert f"rc={want}" in r.stdout
    r = subprocess.run(
        ["bash", "-c", f'source <(sed -n "/^go100_runtime_has_sha() {{/,/^}}/p" "{SCRIPT}"); go100_runtime_has_sha /nonexistent {sha} backend/app.py; echo rc=$?'],
        capture_output=True, text=True, executable="/bin/bash",
    )
    assert "rc=2" in r.stdout


def _has_sha(repo, sha, *paths):
    fn = f'source <(sed -n "/^go100_runtime_has_sha() {{/,/^}}/p" "{SCRIPT}")'
    args = " ".join(f"'{x}'" for x in paths)
    r = subprocess.run(
        ["bash", "-c", f'{fn}; go100_runtime_has_sha "{repo}" {sha} {args}; echo rc=$?'],
        capture_output=True, text=True, executable="/bin/bash",
    )
    m = re.search(r"rc=(\d+)", r.stdout)
    assert m, (r.stdout, r.stderr)
    return int(m.group(1))


def test_r3_uncommitted_old_version_of_deployed_file_is_not_applied(runtime_repo):
    # 리뷰 지적 1: HEAD 가 배포 SHA 여도 실행 파일을 구버전으로 미커밋 수정했다면 반영된 것이 아니다.
    repo, sha = runtime_repo
    assert _has_sha(repo, sha, "backend/app.py") == 0
    (repo / "backend" / "app.py").write_text("v0-old\n")
    assert _has_sha(repo, sha, "backend/app.py") == 5
    assert _has_sha(repo, sha, "backend/") == 5


def test_r3_revert_of_deploy_commit_after_sha_is_not_applied(runtime_repo):
    # 리뷰 지적 1: 배포 커밋을 후속 커밋이 revert 하면 SHA 는 HEAD 의 조상이지만 파일 내용은 구버전이다.
    repo, base = runtime_repo
    (repo / "backend" / "app.py").write_text("v2\n")
    _git(repo, "commit", "-q", "-am", "deploy")
    deploy = _git(repo, "rev-parse", "HEAD")
    assert _has_sha(repo, deploy, "backend/app.py") == 0
    _git(repo, "revert", "--no-edit", deploy)
    r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", deploy, "HEAD"])
    assert r.returncode == 0, "전제: 조상 관계만 보면 통과하는 상황"
    assert _has_sha(repo, deploy, "backend/app.py") == 5
    # 롤백 SHA(revert 커밋)는 같은 경로가 그 내용과 일치하므로 통과한다.
    assert _has_sha(repo, _git(repo, "rev-parse", "HEAD"), "backend/app.py") == 0


def test_r3_deleted_or_untracked_path_mismatch(runtime_repo):
    repo, sha = runtime_repo
    (repo / "backend" / "app.py").unlink()
    assert _has_sha(repo, sha, "backend/app.py") == 5, "SHA 에 있는 파일이 작업트리에서 사라짐"
    (repo / "backend" / "app.py").write_text("v1\n")
    assert _has_sha(repo, sha, "backend/app.py") == 0
    # SHA 에서 삭제되는 파일: 작업트리에 추적되지 않은 채로 남아 있으면 미반영.
    (repo / "backend" / "gone.py").write_text("x\n")
    _git(repo, "add", "backend/gone.py")
    _git(repo, "commit", "-q", "-m", "add gone")
    _git(repo, "rm", "-q", "backend/gone.py")
    _git(repo, "commit", "-q", "-m", "delete gone")
    deleted = _git(repo, "rev-parse", "HEAD")
    assert _has_sha(repo, deleted, "backend/gone.py") == 0
    (repo / "backend" / "gone.py").write_text("stale\n")
    assert _has_sha(repo, deleted, "backend/gone.py") == 5


def test_r3_unrelated_uncommitted_changes_outside_paths_do_not_block(runtime_repo):
    # 운영 호스트의 무관한 미커밋 변경(수백 건)은 검증 대상이 아니다.
    repo, sha = runtime_repo
    (repo / "a.txt").write_text("changed\n")
    (repo / "dirty.txt").write_text("u\n")
    assert _has_sha(repo, sha, "backend/app.py") == 0
    assert _has_sha(repo, sha, "a.txt") == 5


def test_r4_assume_unchanged_old_content_is_not_applied(runtime_repo):
    # 리뷰 지적 1: git diff 는 assume-unchanged 표시를 믿어 구버전 내용을 놓친다. 실제 내용 해시로 비교해야 한다.
    repo, sha = runtime_repo
    _git(repo, "update-index", "--assume-unchanged", "backend/app.py")
    (repo / "backend" / "app.py").write_text("v0-old\n")
    assert _git(repo, "status", "--porcelain", "--", "backend") == "", "전제: git 은 변경을 보지 못한다"
    assert _has_sha(repo, sha, "backend/app.py") == 5
    assert _has_sha(repo, sha, "backend/") == 5
    (repo / "backend" / "app.py").write_text("v1\n")
    assert _has_sha(repo, sha, "backend/app.py") == 0


def test_r4_skip_worktree_old_content_is_not_applied(runtime_repo):
    repo, sha = runtime_repo
    _git(repo, "update-index", "--skip-worktree", "backend/app.py")
    (repo / "backend" / "app.py").write_text("v0-old\n")
    assert _has_sha(repo, sha, "backend/app.py") == 5
    assert _has_sha(repo, sha, "backend/") == 5


def test_r4_untracked_stale_file_inside_directory_is_not_applied(runtime_repo):
    # 리뷰 지적 2: SHA 에 없는 추적되지 않은 파일이 backend/ 안에 남아 있으면 미반영(삭제된 코드가 남은 상태)이다.
    repo, sha = runtime_repo
    assert _has_sha(repo, sha, "backend/") == 0
    (repo / "backend" / "stale.py").write_text("import evil\n")
    assert _has_sha(repo, sha, "backend/") == 5
    assert _has_sha(repo, sha, "backend") == 5
    sub = repo / "backend" / "sub"
    sub.mkdir()
    (repo / "backend" / "stale.py").unlink()
    assert _has_sha(repo, sha, "backend/") == 0, "빈 디렉터리는 코드가 아니다"
    (sub / "deep.py").write_text("x\n")
    assert _has_sha(repo, sha, "backend/") == 5


def test_r4_ignored_stale_file_is_still_not_applied_but_pycache_is_ignored(runtime_repo):
    repo, sha = runtime_repo
    (repo / ".gitignore").write_text("*.local.py\n")
    (repo / "backend" / "x.local.py").write_text("ignored but importable\n")
    assert _has_sha(repo, sha, "backend/") == 5
    (repo / "backend" / "x.local.py").unlink()
    cache = repo / "backend" / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-311.pyc").write_bytes(b"\x00pyc")
    assert _has_sha(repo, sha, "backend/") == 0


def test_r4_mode_symlink_and_type_swaps_are_not_applied(runtime_repo):
    repo, _ = runtime_repo
    (repo / "backend" / "run.sh").write_text("#!/bin/sh\n")
    (repo / "backend" / "run.sh").chmod(0o755)
    (repo / "backend" / "link.py").symlink_to("app.py")
    _git(repo, "add", "backend")
    _git(repo, "commit", "-q", "-m", "c2")
    sha = _git(repo, "rev-parse", "HEAD")
    assert _has_sha(repo, sha, "backend/") == 0
    (repo / "backend" / "run.sh").chmod(0o644)
    assert _has_sha(repo, sha, "backend/") == 5, "실행 비트 소실"
    (repo / "backend" / "run.sh").chmod(0o755)
    (repo / "backend" / "link.py").unlink()
    (repo / "backend" / "link.py").write_text("app.py")
    assert _has_sha(repo, sha, "backend/link.py") == 5, "심볼릭 링크가 일반 파일로 바뀜"
    (repo / "backend" / "link.py").unlink()
    (repo / "backend" / "link.py").symlink_to("other.py")
    assert _has_sha(repo, sha, "backend/link.py") == 5, "링크 대상이 다름"


def test_r4_invalid_path_arguments_are_rejected(runtime_repo):
    repo, sha = runtime_repo
    assert _has_sha(repo, sha, "../etc") == 4
    assert _has_sha(repo, sha, "/etc") == 4


def test_r3_no_paths_is_fail_closed(runtime_repo):
    repo, sha = runtime_repo
    assert _has_sha(repo, sha) == 4


def test_r3_restart_blocked_when_runtime_file_is_old_version(fake, runtime_repo):
    repo, sha = runtime_repo
    (repo / "backend" / "app.py").write_text("old\n")
    r, chat = _run_block(
        fake, "backend/app.py", {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_REPO_DIR": str(repo)}, sha
    )
    assert r.returncode == 0, r.stderr
    assert "go100-api:remote_code_mismatch" in _build_fail(r).split(";")
    assert not _restart_sent(fake), "실행 파일이 구버전이면 재기동 명령을 보내지 않는다"
    assert not fake["sysl"].exists()
    assert "미반영" in chat


def test_r3_restart_passes_only_backend_paths_and_defaults_to_backend_dir(fake, runtime_repo):
    repo, sha = runtime_repo
    # 변경 파일에 frontend/ 와 backend/ 가 섞여 있어도 검증 경로는 backend/ 파일만이다.
    (repo / "frontend").mkdir()
    (repo / "frontend" / "x.ts").write_text("dirty\n")  # 경로 밖 변경은 영향 없음
    r, _ = _run_block(
        fake,
        "frontend/x.ts\nbackend/app.py",
        {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_REPO_DIR": str(repo)},
        sha,
    )
    assert "go100-api" not in _build_fail(r), (r.stdout, r.stderr)
    assert _restart_sent(fake)
    calls = [ln for ln in _scripts_text(fake).splitlines() if ln.startswith("go100_runtime_has_sha /")]
    assert len(calls) == 2 and all("backend/app.py" in ln and "frontend" not in ln for ln in calls), calls
    body = (
        f'source "{fake["lib"]}"\nset +e\n'
        'go100_backend_verify_paths false "x"; go100_backend_verify_paths true "docs/a.md"; '
        'go100_backend_verify_paths true $\'backend/a.py\\nbackend/b.py\''
    )
    out = subprocess.run(["bash", "-c", body], env=fake["base"], capture_output=True, text=True).stdout.split()
    assert out == ["backend/", "backend/", "backend/a.py", "backend/b.py"]


def test_r3_post_restart_code_change_is_recorded_as_failure(fake, runtime_repo):
    # 재기동 직후 실행 파일이 바뀌면(구버전 복귀) 재검증에서 실패로 기록한다.
    repo, sha = runtime_repo
    bindir = Path(fake["base"]["FAKE_BIN"])
    (bindir / "systemctl").write_text(
        f'#!/bin/bash\necho "$*" >> "$FAKE_SYSTEMCTL_LOG"\nprintf "old\\n" > "{repo}/backend/app.py"\nexit 0\n',
        encoding="utf-8",
    )
    r, _ = _run_block(
        fake, "backend/app.py", {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_REPO_DIR": str(repo)}, sha
    )
    assert "go100-api:remote_sha_lost" in _build_fail(r).split(";")


def test_r2_rollback_requires_rollback_sha_on_runtime_host(fake, runtime_repo):
    repo, sha = runtime_repo
    extra = {"GO100_DEPLOY_SSH_HOST": "contabo14", "GO100_REPO_DIR": str(repo)}
    body = f'source "{fake["lib"]}"\nset +e\nlog() {{ echo "L $*"; }}\ngo100_remote_api_restart {ZERO[:39]}1 rollback; echo "rc=$? label=$GO100_REMOTE_FAIL_LABEL"\n'
    e = dict(fake["base"])
    e.update(extra)
    r = subprocess.run(["bash", "-c", body], env=e, capture_output=True, text=True, timeout=60)
    assert "rc=1 label=rollback:remote_sha_not_applied" in r.stdout, (r.stdout, r.stderr)
    assert not _restart_sent(fake)
    body = body.replace(f"{ZERO[:39]}1", sha)
    r = subprocess.run(["bash", "-c", body], env=e, capture_output=True, text=True, timeout=60)
    assert "rc=0 label=" in r.stdout, (r.stdout, r.stderr)
    assert _restart_sent(fake)
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'go100_remote_api_restart "$rollback_sha" rollback' in text
    # 롤백 SHA 확인 없이 원격 재기동을 부르는 줄이 없다(원격 모드 재기동은 래퍼 한 곳을 지난다).
    for ln in text.splitlines():
        if "go100_deploy_call systemctl restart go100" in ln and not ln.strip().startswith("#"):
            assert "2>/dev/null || true" in ln or "|| _rc=$?" in ln, ln


def test_r2_local_mode_unchanged_restart_failure_ignored_and_no_ssh(fake):
    # ④ 로컬 모드: 호스트가 비면 ssh 를 부르지 않고, 재기동 실패(rc!=0)는 예전처럼 무시하며 build_fail 은 비어 있다.
    r, chat = _run_block(
        fake,
        "backend/app.py",
        {"FAKE_SYSTEMCTL_RC": "5", "PATH": f"{fake['base']['FAKE_BIN']}:{fake['base']['PATH']}"},
        "c" * 40,
    )
    assert r.returncode == 0, r.stderr
    assert _build_fail(r) == ""
    assert fake["sysl"].read_text().strip() == "restart go100"
    assert not any(p.is_file() for p in fake["scripts"].iterdir()), "로컬 모드는 ssh 를 부르지 않는다"
    assert chat == ""
    # 로컬 분기의 기존 줄이 그대로 있다.
    assert "go100_deploy_call systemctl restart go100 2>/dev/null || true" in _go100_case()


def test_r2_comm_failed_helper_is_false_in_local_mode(env):
    for rc in ("0", "1", "2", "124", "137", "255"):
        r = _run(env, f"set +e; go100_deploy_comm_failed {rc}; echo rc=$?")
        assert "rc=1" in r.stdout, rc
    for rc, want in (("0", 1), ("1", 1), ("2", 1), ("124", 0), ("137", 0), ("255", 0)):
        r = _run(env, f"set +e; go100_deploy_comm_failed {rc}; echo rc=$?", {"GO100_DEPLOY_SSH_HOST": "contabo14"})
        assert f"rc={want}" in r.stdout, rc
    r = _run(env, "go100_deploy_comm_label 124; go100_deploy_comm_label 137; go100_deploy_comm_label 255")
    assert r.stdout.split() == ["timeout", "timeout", "ssh_failed"]


def test_r2_runtime_has_sha_is_shipped_to_remote():
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("GO100_DEPLOY_REMOTE_FUNCS=(")
    assert "go100_runtime_has_sha" in text[start : text.index(")", start)]


def test_r2_runner_local_copy_identical_after_r2():
    assert SCRIPT.read_bytes() == (ROOT / "scripts" / "pipeline-runner.sh.local").read_bytes()
