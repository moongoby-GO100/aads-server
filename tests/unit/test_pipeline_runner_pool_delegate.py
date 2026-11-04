"""러너 풀(2026-11-04): 배포 위임·서버 단위 상한·한가한 서버 우선.

설정이 없으면 기존과 똑같아야 하고, 켜면 contabo42 같은 위임 러너가
커밋·작업 폴더를 배포 호스트(116)에 넘긴 뒤 승인·반려·DEPLOY_ONLY 작업을 가져가지 않는다.
"""
import os
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/pipeline-runner.sh").read_text(encoding="utf-8")
BLOCK = SCRIPT[SCRIPT.index("# ── 러너 풀: 배포 위임"):SCRIPT.index("claim_approved_job() {")]
DEPLOY_ONLY_LINE = next(l for l in SCRIPT.splitlines() if l.startswith("DEPLOY_ONLY_HEADER_SQL="))

STUBS = r'''
LOGF="${LOGF:-/dev/null}"
log() { echo "$*" >> "$LOGF"; }
post_to_chat() { echo "CHAT $1 $2" >> "$LOGF"; }
sql_escape() { printf "'%s'" "${1//\'/\'\'}"; }
RUNNER_HOST_NAME="${RUNNER_HOST_NAME:-contabo42}"
'''


def run(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    script = f"set -u\n{STUBS}\n{DEPLOY_ONLY_LINE}\n{BLOCK}\n{body}\n"
    return subprocess.run(["bash", "-c", script], env={**os.environ, **(env or {})},
                          capture_output=True, text=True)


def test_defaults_change_nothing():
    out = run('echo "[$(runner_pool_host_scope_sql)][$(runner_pool_host_scope_sql r)][$(runner_pool_claim_extra_sql)]"; '
              'runner_is_deploy_delegate && echo delegate || echo normal; runner_claim_psi_ok && echo claim_ok').stdout
    assert "[][][]" in out
    assert "normal" in out
    assert "claim_ok" in out


def test_host_scope_and_deploy_only_exclusion():
    out = run('echo "[$(runner_pool_host_scope_sql)]"; echo "[$(runner_pool_host_scope_sql r)]"; echo "[$(runner_pool_claim_extra_sql)]"',
              {"RUNNER_CAPACITY_SCOPE": "host", "RUNNER_DEPLOY_DELEGATE_HOST": "server-116"}).stdout.splitlines()
    assert out[0] == "[AND runner_host = 'contabo42']"
    assert out[1] == "[AND r.runner_host = 'contabo42']"
    assert out[2].startswith("[AND NOT (") and "DEPLOY_ONLY" in out[2]


def test_psi_gate_defers_when_busy_and_logs_once(tmp_path):
    psi = tmp_path / "cpu"
    logf = tmp_path / "log"
    env = {"RUNNER_CLAIM_MAX_PSI": "40", "RUNNER_PSI_FILE": str(psi), "LOGF": str(logf)}
    psi.write_text("some avg10=70.00 avg60=55.10 avg300=50.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    out = run('runner_claim_psi_ok && echo A || echo a; runner_claim_psi_ok && echo B || echo b', env).stdout
    assert out.split() == ["a", "b"]
    assert logf.read_text().count("CLAIM_DEFERRED_BUSY") == 1
    psi.write_text("some avg10=5.00 avg60=12.00 avg300=10.00 total=1\n")
    assert run("runner_claim_psi_ok && echo ok", env).stdout.strip() == "ok"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _setup_repos(tmp_path):
    local_main = tmp_path / "local" / "aads-server"
    remote_root = tmp_path / "remote"
    local_main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=local_main)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base", cwd=local_main)
    # 배포 호스트 저장소: 같은 경로 구조를 원격 루트 아래에 흉내 낸다.
    remote_main = remote_root / str(local_main).lstrip("/")
    remote_main.parent.mkdir(parents=True)
    _git("clone", "-q", str(local_main), str(remote_main), cwd=tmp_path)
    job = f"runner-test{uuid.uuid4().hex[:8]}"
    wt = Path(f"/tmp/aads-wt-{job}")
    _git("worktree", "add", "-q", "--detach", str(wt), "HEAD", cwd=local_main)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "job", cwd=wt)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()
    # 가짜 ssh: 호스트 인자를 버리고, 원격 경로를 remote_root 아래로 옮겨 로컬에서 실행한다.
    fake = tmp_path / "bin" / "ssh"
    fake.parent.mkdir()
    fake.write_text(f'''#!/bin/bash
while [[ "$1" == -* ]]; do [[ "$1" == "-o" ]] && shift; shift; done
shift
cmd="$*"
WT_FROM='/tmp/aads-wt-'; WT_TO='{remote_root}/tmp/aads-wt-'
MAIN_FROM='{local_main}'; MAIN_TO='{remote_main}'
cmd="${{cmd//"$WT_FROM"/"$WT_TO"}}"
cmd="${{cmd//"$MAIN_FROM"/"$MAIN_TO"}}"
mkdir -p '{remote_root}/tmp'
exec bash -c "$cmd"
''')
    fake.chmod(0o755)
    env = {"PATH": f"{fake.parent}:{os.environ['PATH']}", "GIT_SSH_COMMAND": str(fake),
           "RUNNER_DEPLOY_DELEGATE_HOST": "server-116"}
    return job, wt, sha, remote_root, remote_main, env


def test_handoff_pushes_commit_and_creates_worktree_on_deploy_host(tmp_path):
    job, wt, sha, remote_root, remote_main, env = _setup_repos(tmp_path)
    try:
        r = run(f'runner_deploy_handoff_one {job} {sha} && echo HANDOFF_OK', {**env, "LOGF": str(tmp_path / "log")})
        assert "HANDOFF_OK" in r.stdout, (r.stdout, r.stderr, (tmp_path / "log").read_text() if (tmp_path / "log").exists() else "")
        ref = subprocess.run(["git", "rev-parse", f"refs/runner-handoff/{job}"], cwd=remote_main, capture_output=True, text=True).stdout.strip()
        assert ref == sha
        remote_wt = remote_root / "tmp" / f"aads-wt-{job}"
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=remote_wt, capture_output=True, text=True).stdout.strip()
        assert head == sha
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=wt.parent, capture_output=True)
        subprocess.run(["rm", "-rf", str(wt)])


def test_handoff_refuses_when_head_differs(tmp_path):
    job, wt, sha, *_rest, env = _setup_repos(tmp_path)
    try:
        r = run(f'runner_deploy_handoff_one {job} {"0" * 40} && echo BAD || echo REFUSED', {**env, "LOGF": str(tmp_path / "log")})
        assert "REFUSED" in r.stdout
        assert "DEPLOY_HANDOFF_SKIP" in (tmp_path / "log").read_text()
    finally:
        subprocess.run(["rm", "-rf", str(wt)])


def test_sweep_marks_done_once_and_reports_failure_once(tmp_path):
    state = tmp_path / "state"
    logf = tmp_path / "log"
    calls = tmp_path / "calls"
    env = {"RUNNER_DEPLOY_DELEGATE_HOST": "server-116", "RUNNER_HANDOFF_STATE_DIR": str(state), "LOGF": str(logf)}
    ok_sha, bad_sha = "a" * 40, "b" * 40
    body = f'''
db_exec() {{ printf 'job-ok\\x1e{ok_sha}\\x1esess1\\njob-bad\\x1e{bad_sha}\\x1esess2\\n'; }}
runner_deploy_handoff_one() {{ echo "$1" >> {calls}; [[ "$1" == job-ok ]]; }}
runner_deploy_handoff_sweep; runner_deploy_handoff_sweep
'''
    run(body, env)
    assert calls.read_text().split() == ["job-ok", "job-bad", "job-bad"]
    log = logf.read_text()
    assert log.count("DEPLOY_HANDOFF_OK") == 1
    assert log.count("CHAT sess2") == 1


def test_main_loop_skips_approved_and_rejected_only_in_delegate_mode():
    loop = SCRIPT[SCRIPT.index("        if runner_claim_psi_ok; then"):]
    loop = loop[:loop.index("reject_job \"$job_id\"")]
    assert 'pending=$(claim_queued_job "$project_filter" 2>/dev/null) || true' in loop
    assert loop.count("if runner_is_deploy_delegate; then") == 2
    assert "runner_deploy_handoff_sweep || true" in loop
    assert 'approved=$(claim_approved_job "$project_filter" 2>/dev/null) || true' in loop
    assert 'rejected=$(claim_rejected_job "$project_filter" 2>/dev/null) || true' in loop


def test_claim_queries_use_pool_hooks():
    assert "('running','claimed') $1 $(runner_pool_host_scope_sql);" in SCRIPT
    assert "AND r.job_id != p.job_id $(runner_pool_host_scope_sql r)) <" in SCRIPT
    assert "${RUNNER_MH_CLAIM_EXTRA:-} $(runner_pool_claim_extra_sql)" in SCRIPT
