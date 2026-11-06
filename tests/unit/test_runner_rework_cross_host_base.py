"""AADS-RUNNER-REWORK-CROSS-HOST-BASE-20261106

재작업 출발 커밋이 다른 러너 서버에만 있을 때: ref 고정, ssh 로 가져오기, 못 가져오면 fail-closed.
셸 함수는 pipeline-runner.sh 에서 잘라 내 가짜 db/log/ssh 로 실제 git 저장소 위에서 돌린다.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from app.services import pipeline_auto_rework as ar

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "pipeline-runner.sh"
FUNCS = ("runner_artifact_prune", "runner_artifact_pin", "runner_artifact_commit_sql",
         "runner_artifact_ssh_cmd", "runner_artifact_ssh_alias",
         "rework_instruction_start_sha", "runner_artifact_fetch_from_repo", "runner_artifact_fetch_from", "runner_artifact_remote_ref",
         "ensure_rework_base_or_fail", "commit_job_worktree_for_approval")


def _git(cwd, *a, env=None):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True, env=env).stdout.strip()


def _extract(name):
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index(f"\n{name}() {{\n") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _mkrepo(p):
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.name", "T")
    _git(p, "config", "user.email", "t@e.com")
    (p / "f").write_text("1\n")
    _git(p, "add", "f")
    _git(p, "commit", "-qm", "init")
    return p


# ── build_rework_instruction ──
def _build(**kw):
    base = dict(original="TARGET: x\n본문\n", parent_job_id="runner-aaaaaaaa", round_no=1,
                rounds_max=2, issues=["지적"], commit_hash="0123456789abcdef")
    return ar.build_rework_instruction(**(base | kw))


def test_start_point_names_parent_host_and_ref():
    out = _build(runner_host="vmi3725547")
    assert "vmi3725547" in out
    assert "refs/runner-artifacts/runner-aaaaaaaa" in out
    assert "/tmp/aads-wt-runner-aaaaaaaa" in out and "0123456789ab" in out


def test_fallback_to_reimplement_removed():
    out = _build()
    assert "산출물이 없으면 위 원 지시대로 구현하라" not in out
    assert "rework_base_unavailable" in out
    assert "구현하지 말고" in out


# ── 셸 ──
@pytest.fixture
def sh(tmp_path):
    funcs = "\n".join(_extract(n) for n in FUNCS)
    lib = tmp_path / "lib.sh"
    lib.write_text(funcs)
    logf = tmp_path / "log"
    logf.write_text("")

    def run(body, env=None, db=""):
        script = f"""
log() {{ echo "$*" >> {logf}; }}
db_exec() {{ {db or 'echo ""'}; }}
sql_escape() {{ echo "'$1'"; }}
db_update() {{ echo "DBUPDATE $*" >> {logf}; }}
_fail_job() {{ echo "FAIL $3 | $5 | $4" >> {logf}; }}
RUNNER_ARTIFACT_NS=refs/runner-artifacts
RUNNER_ARTIFACT_RETENTION_DAYS=${{RUNNER_ARTIFACT_RETENTION_DAYS:-7}}
RUNNER_ARTIFACT_FETCH_TIMEOUT=20
RUNNER_ARTIFACT_SSH_CONFIG=${{RUNNER_ARTIFACT_SSH_CONFIG:-/nonexistent}}
RUNNER_HOST_NAME=localhost-x; RUNNER_HOSTNAME=localhost-x
source {lib}
{body}
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=os.environ | (env or {}), timeout=60)
        return r, logf.read_text()
    return run


def test_pin_records_ref_and_skips_when_no_commit(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    base = _git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt))
    r, _ = sh(f"runner_artifact_pin runner-aaaa1111 {wt} {base}")
    assert r.stdout == ""  # 커밋 캡처 stdout 오염 금지
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "refs/runner-artifacts/runner-aaaa1111"]).returncode != 0
    (wt / "f").write_text("2\n")
    _git(wt, "commit", "-qam", "work")
    sha = _git(wt, "rev-parse", "HEAD")
    r, _ = sh(f"runner_artifact_pin runner-aaaa1111 {wt} {base}")
    assert r.returncode == 0 and r.stdout == ""
    assert _git(repo, "rev-parse", "refs/runner-artifacts/runner-aaaa1111") == sha  # 공통 저장소에 기록


def test_prune_removes_only_expired(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    old = _git(repo, "rev-parse", "HEAD")
    env = os.environ | {"GIT_COMMITTER_DATE": f"{int(time.time()) - 9 * 86400} +0000"}
    (repo / "f").write_text("old\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "old"], check=True, env=env)
    old_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("new\n")
    _git(repo, "commit", "-qam", "new")
    new_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/runner-artifacts/runner-old00000", old_sha)
    _git(repo, "update-ref", "refs/runner-artifacts/runner-new00000", new_sha)
    sh(f"runner_artifact_prune {repo}")
    refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/runner-artifacts")
    assert "runner-new00000" in refs and "runner-old00000" not in refs
    assert old  # silence


def test_ensure_noop_without_parent_marker(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} $'TARGET: x\\n본문'")
    assert r.returncode == 0 and "FAIL" not in log


def test_ensure_fail_closed_when_base_missing(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    sha = "d" * 40
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host unrelated\n    HostName 1.2.3.4\n")
    db = f"printf '%s\\x1e%s\\x1e%s' {sha} vmi3725547 0"
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} $'AUTO_REWORK_OF: runner-aaaaaaaa\\nTARGET: x'",
                env={"RUNNER_ARTIFACT_SSH_CONFIG": str(cfg)}, db=db)
    assert r.returncode == 1
    assert f"FAIL rework_base_unavailable | rework_base_unavailable: {sha} on vmi3725547" in log


FAKE_SSH = r"""#!/bin/bash
# 실제 ssh 인자를 기록하고, 원격 명령을 peer 저장소 경로로 치환해 로컬에서 실행한다(전송 계층은 git 이 그대로 탄다).
# -F 로 지정한 설정 파일에서만 별칭(HostName·IdentityFile)을 읽는다고 가정한다: -F 가 없거나 다르면 실패한다.
echo "ARGV: $*" >> "$FAKE_SSH_LOG"
opts=(); cfg=""
while [[ "$1" == -* ]]; do
  if [[ "$1" == -o ]]; then opts+=("$2"); shift 2
  elif [[ "$1" == -F ]]; then cfg="$2"; shift 2
  else shift; fi
done
host="$1"; shift
echo "OPTS: ${opts[*]} HOST: $host CFG: $cfg" >> "$FAKE_SSH_LOG"
[[ "$cfg" == "$FAKE_EXPECT_CFG" ]] || { echo "ssh: alias $host not resolvable without -F" >&2; exit 255; }
grep -q "^Host $host\$" "$cfg" || exit 255
cmd="$*"
if [[ "$cmd" == "hostname -s" ]]; then
  case "$host" in mesh-42) echo vmi3725547;; *) echo other-$host;; esac
  exit 0
fi
[[ "$host" == mesh-42 ]] || exit 255
[[ -z "$FAKE_SSH_DOWN" ]] || exit 255
cmd="${cmd//$FAKE_LOCAL_REPO/$FAKE_PEER_REPO}"
exec bash -c "$cmd"
"""


@pytest.fixture
def peer_env(tmp_path):
    peer = _mkrepo(tmp_path / "peer")
    local = _mkrepo(tmp_path / "local")
    # 공통 조상으로 맞춘다: local 이 먼저 만든 init 커밋과 peer 의 init 커밋은 다르지만, 가져오기엔 무관하다.
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host unrelated\n    HostName 9.9.9.9\nHost mesh-42\n    HostName 1.1.1.1\n    IdentityFile ~/.ssh/mesh_key\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").write_text(FAKE_SSH)
    (bindir / "ssh").chmod(0o755)
    sshlog = tmp_path / "ssh.log"
    sshlog.write_text("")
    env = {"RUNNER_ARTIFACT_SSH_CONFIG": str(cfg), "PATH": f"{bindir}:{os.environ['PATH']}",
           "FAKE_SSH_LOG": str(sshlog), "FAKE_EXPECT_CFG": str(cfg), "FAKE_LOCAL_REPO": str(local), "FAKE_PEER_REPO": str(peer)}
    return peer, local, env, sshlog


def _peer_commit(peer, text="peer"):
    (peer / "f").write_text(text + "\n")
    _git(peer, "commit", "-qam", text)
    return _git(peer, "rev-parse", "HEAD")


def _instr(sha, parent="runner-aaaaaaaa"):
    return (f"AUTO_REWORK_OF: {parent}\\nTARGET: x\\n- 출발점: 반려된 산출물 `/tmp/aads-wt-{parent}` "
            f"(커밋 `{sha}`), 러너 서버 `vmi3725547`.")


def _has(repo, sha):
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode == 0


def test_ensure_fetches_from_peer_via_real_ssh_args(peer_env, sh):
    """DB commit_hash 가 있고 로컬에 없는 커밋 — 별칭 탐색은 mesh-* 만 ssh 로 물어보고, git fetch 도 같은 ssh 를 탄다."""
    peer, local, env, sshlog = peer_env
    sha = _peer_commit(peer)
    _git(peer, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", sha)
    assert not _has(local, sha)
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {local} $'RETRY_OF_LINK: runner-aaaaaaaa'",
                env=env, db=f"printf '%s\\x1e%s\\x1e%s' {sha} vmi3725547 0")
    assert r.returncode == 0, log
    assert "REWORK_BASE_HOST_MAP host=vmi3725547 alias=mesh-42 via=remote_hostname" in log
    assert "REWORK_BASE_FETCHED" in log and "FAIL" not in log
    assert _git(local, "rev-parse", "refs/runner-artifacts/runner-aaaaaaaa") == sha
    s = sshlog.read_text()
    assert f"OPTS: BatchMode=yes ConnectTimeout=8 HOST: mesh-42 CFG: {env['RUNNER_ARTIFACT_SSH_CONFIG']}" in s  # 별칭 탐색 프로브
    assert "HOST: unrelated" not in s                                          # mesh-/contabo 이외는 건드리지 않는다
    assert re.search(r"OPTS: BatchMode=yes ConnectTimeout=10( .*)? HOST: mesh-42", s)  # git fetch 가 쓴 ssh
    assert f"git-upload-pack '{local}'" in s                                   # 실제로 원격 저장소 경로를 열었다


def test_ensure_fetches_when_db_commit_hash_empty_using_instruction_sha(peer_env, sh):
    """error 종료 부모는 DB commit_hash 가 비어 있다. 지시서 SHA(12자 약식)와 runner_host 로 원격 ref 를 가져온다."""
    peer, local, env, _ = peer_env
    sha = _peer_commit(peer)
    _git(peer, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", sha)
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {local} $'{_instr(sha[:12])}'",
                env=env, db="printf '%s\\x1e%s\\x1e%s' '' vmi3725547 0")
    assert r.returncode == 0, log
    assert "REWORK_BASE_FETCHED" in log and "FAIL" not in log
    assert _git(local, "rev-parse", "refs/runner-artifacts/runner-aaaaaaaa") == sha


def test_ensure_instruction_sha_beats_db_sha_when_db_is_local_but_wrong(peer_env, sh):
    """DB 가 로컬에 있는 커밋 A 를 가리켜도 지시서가 요구하는 B 가 없으면 B 를 가져온다."""
    peer, local, env, _ = peer_env
    a = _git(local, "rev-parse", "HEAD")
    b = _peer_commit(peer)
    _git(peer, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", b)
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {local} $'{_instr(b[:12])}'",
                env=env, db=f"printf '%s\\x1e%s\\x1e%s' {a} vmi3725547 0")
    assert r.returncode == 0, log
    assert "REWORK_BASE_DB_MISMATCH" in log
    assert _git(local, "rev-parse", "refs/runner-artifacts/runner-aaaaaaaa") == b


def test_ensure_fails_when_instruction_sha_absent_though_db_and_ref_point_elsewhere(peer_env, sh):
    """지시서 B, DB/로컬 ref/원격 ref 모두 A — A 가 있어도 B 가 없으면 모델을 띄우지 않는다."""
    peer, local, env, _ = peer_env
    a = _git(local, "rev-parse", "HEAD")
    _git(local, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", a)
    b = "b" * 40
    _git(peer, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", _git(peer, "rev-parse", "HEAD"))
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {local} $'{_instr(b)}'",
                env=env, db=f"printf '%s\\x1e%s\\x1e%s' {a} vmi3725547 0")
    assert r.returncode == 1
    assert f"FAIL rework_base_unavailable | rework_base_unavailable: {b} on vmi3725547" in log
    assert _git(local, "rev-parse", "refs/runner-artifacts/runner-aaaaaaaa") == a  # 기존 ref 를 덮어쓰지 않았다
    assert "incoming" not in _git(local, "for-each-ref", "--format=%(refname)", "refs/runner-artifacts")


def test_ensure_fail_closed_when_ssh_alias_unresolvable(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host unrelated\n    HostName 1.2.3.4\n")
    sha = "c" * 40
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} $'{_instr(sha)}'",
                env={"RUNNER_ARTIFACT_SSH_CONFIG": str(cfg)}, db="printf '%s\\x1e%s\\x1e%s' '' vmi3725547 0")
    assert r.returncode == 1
    assert f"rework_base_unavailable: {sha} on vmi3725547" in log


_NO_SHA_INSTR = "$'AUTO_REWORK_OF: runner-aaaaaaaa\\nTARGET: x'"


def test_ensure_fails_when_no_sha_and_host_unknown(tmp_path, sh):
    """(구 test_ensure_skips_when_no_sha_anywhere_and_remote_unreachable) fail-open 이 아니라 fail-closed. started_at이 있을 때."""
    repo = _mkrepo(tmp_path / "r")
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} {_NO_SHA_INSTR}",
                db="printf '%s\\x1e%s\\x1e%s' '' '' 0")
    assert r.returncode == 1 and "REWORK_BASE_SKIP" not in log
    assert "FAIL rework_base_unavailable | rework_base_unavailable: runner-aaaaaaaa on unknown" in log


def test_ensure_skips_when_parent_never_started(tmp_path, sh):
    """부모가 시작되지 않음(started_at IS NULL) + host 빈 값 + sha 빈 값 → 산출물 없음 확정, 건너뛰기."""
    repo = _mkrepo(tmp_path / "r")
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} {_NO_SHA_INSTR}",
                db="printf '%s\\x1e%s\\x1e%s' '' '' 1")
    assert r.returncode == 0, log
    assert "REWORK_BASE_SKIP" in log and "부모가 시작되지 않음(산출물 없음 확정)" in log and "FAIL" not in log


def test_ensure_fails_when_parent_started_but_host_missing(tmp_path, sh):
    """부모가 시작됨(started_at IS NOT NULL) + host 빈 값 + sha 빈 값 → 산출물 유무를 모르므로 fail-closed."""
    repo = _mkrepo(tmp_path / "r")
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} {_NO_SHA_INSTR}",
                db="printf '%s\\x1e%s\\x1e%s' '' '' 0")
    assert r.returncode == 1
    assert "FAIL rework_base_unavailable | rework_base_unavailable: runner-aaaaaaaa on unknown" in log
    assert "REWORK_BASE_SKIP" not in log


def test_ensure_fails_when_parent_row_missing(tmp_path, sh):
    """DB 쿼리가 행을 반환하지 못함 → rework_base_unavailable."""
    repo = _mkrepo(tmp_path / "r")
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} {_NO_SHA_INSTR}",
                db="echo ''")
    assert r.returncode == 1
    assert "FAIL rework_base_unavailable | rework_base_unavailable: runner-aaaaaaaa on unknown" in log


def test_ensure_skips_when_remote_reachable_and_no_artifact_ref(peer_env, sh):
    peer, local, env, sshlog = peer_env
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {local} {_NO_SHA_INSTR}",
                env=env, db="printf '%s\\x1e%s\\x1e%s' '' vmi3725547 0")
    assert r.returncode == 0, log
    assert "REWORK_BASE_SKIP" in log and "원격 ls-remote 성공" in log and "FAIL" not in log
    assert f"git-upload-pack '{local}'" in sshlog.read_text()  # 실제로 원격에 물어보고 건너뛰었다


def test_ensure_fetches_when_remote_reachable_and_artifact_ref_present(peer_env, sh):
    peer, local, env, _ = peer_env
    sha = _peer_commit(peer)
    _git(peer, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", sha)
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {local} {_NO_SHA_INSTR}",
                env=env, db="printf '%s\\x1e%s\\x1e%s' '' vmi3725547 0")
    assert r.returncode == 0, log
    assert "REWORK_BASE_FETCHED" in log and "REWORK_BASE_SKIP" not in log and "FAIL" not in log
    assert _git(local, "rev-parse", "refs/runner-artifacts/runner-aaaaaaaa") == sha


def test_ensure_fails_when_remote_unreachable_and_no_sha(peer_env, sh):
    """별칭은 찾았지만(hostname -s 응답) ls-remote 가 실패 — 산출물 유무를 모르므로 건너뛰지 않는다."""
    peer, local, env, _ = peer_env
    sha = _peer_commit(peer)
    _git(peer, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", sha)
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {local} {_NO_SHA_INSTR}",
                env=env | {"FAKE_SSH_DOWN": "1"}, db="printf '%s\\x1e%s\\x1e%s' '' vmi3725547 0")
    assert r.returncode == 1 and "REWORK_BASE_SKIP" not in log
    assert "FAIL rework_base_unavailable | rework_base_unavailable: runner-aaaaaaaa on vmi3725547" in log
    assert "ls-remote 실패" in log


def test_ensure_fails_when_no_sha_and_alias_unresolvable(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host unrelated\n    HostName 1.2.3.4\n")
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} {_NO_SHA_INSTR}",
                env={"RUNNER_ARTIFACT_SSH_CONFIG": str(cfg)}, db="printf '%s\\x1e%s\\x1e%s' '' vmi3725547 0")
    assert r.returncode == 1 and "REWORK_BASE_SKIP" not in log
    assert "FAIL rework_base_unavailable" in log and "별칭을 찾지 못함" in log


def test_ensure_same_host_skips_only_when_local_ref_absent(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    db = "printf '%s\\x1e%s\\x1e%s' '' localhost-x 0"
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} {_NO_SHA_INSTR}", db=db)
    assert r.returncode == 0 and "REWORK_BASE_SKIP" in log and "FAIL" not in log


def test_ensure_same_host_uses_local_ref_when_present(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    _git(repo, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", _git(repo, "rev-parse", "HEAD"))
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {repo} {_NO_SHA_INSTR}",
                db="printf '%s\\x1e%s\\x1e%s' '' localhost-x 0")
    assert r.returncode == 0 and "REWORK_BASE_LOCAL" in log and "REWORK_BASE_SKIP" not in log


def test_ssh_config_file_is_passed_to_every_ssh_call(peer_env, sh):
    """별칭의 HostName·IdentityFile 이 별도 설정 파일에만 있다. 별칭 탐색·ls-remote·fetch 모두 -F 를 달고 나간다."""
    peer, local, env, sshlog = peer_env
    sha = _peer_commit(peer)
    _git(peer, "update-ref", "refs/runner-artifacts/runner-aaaaaaaa", sha)
    cfg = env["RUNNER_ARTIFACT_SSH_CONFIG"]
    r, log = sh(f"ensure_rework_base_or_fail runner-n1 sess {local} {_NO_SHA_INSTR}",
                env=env, db="printf '%s\\x1e%s' '' vmi3725547")
    assert r.returncode == 0, log
    argvs = [ln for ln in sshlog.read_text().splitlines() if ln.startswith("ARGV: ")]
    assert len(argvs) >= 3  # 별칭 탐색 + ls-remote + fetch
    assert all(ln.startswith(f"ARGV: -F {cfg} ") for ln in argvs), argvs
    # -F 없이는 같은 별칭을 못 푼다(가짜 ssh 가 실패) — 즉 위 성공은 -F 덕분이다.
    bare = subprocess.run([str(Path(env["PATH"].split(":")[0]) / "ssh"), "mesh-42", "hostname -s"],
                          env=os.environ | env, capture_output=True, text=True)
    assert bare.returncode == 255


def test_ssh_cmd_quotes_config_path_with_spaces(sh):
    r, _ = sh('eval "set -- $(runner_artifact_ssh_cmd 7)"; echo "$1|$2|$3"',
              env={"RUNNER_ARTIFACT_SSH_CONFIG": "/tmp/a b/ssh config"})
    assert r.stdout.strip() == "ssh|-F|/tmp/a b/ssh config"


def test_fail_job_records_pinned_sha_when_commit_hash_empty(tmp_path, sh):
    """provenance 검증 등으로 오류 종료해도 고정한 산출물 SHA 가 DB commit_hash 에 같이 기록된다(비어 있을 때만)."""
    repo = _mkrepo(tmp_path / "r")
    base = _git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt))
    (wt / "f").write_text("worker\n")
    _git(wt, "commit", "-qam", "worker")
    sha = _git(wt, "rev-parse", "HEAD")
    body = (_extract("_fail_job") + "\nrecord_runner_event() { :; }\npost_to_chat() { :; }\n"
            "_reap_job_test_containers() { :; }\n_notify_ai() { :; }\n"
            f"runner_artifact_pin runner-cccc3333 {wt} {base}\n"
            "_fail_job runner-cccc3333 sess approval_x detail\n"
            "_fail_job runner-other000 sess approval_x detail\n")
    r, log = sh(body)
    assert r.returncode == 0, r.stderr
    updates = ["DBUPDATE" + c for c in log.split("DBUPDATE")[1:]]
    mine = next(ln for ln in updates if "job_id='runner-cccc3333'" in ln)
    other = next(ln for ln in updates if "job_id='runner-other000'" in ln).split("FAIL_FAST")[0]
    assert f"commit_hash=COALESCE(NULLIF(commit_hash,''),'{sha}')" in mine  # 비어 있을 때만 채운다
    assert "commit_hash" not in other  # 다른 job 에는 번지지 않는다


def test_start_sha_extracted_from_build_rework_instruction(sh):
    out = _build(commit_hash="0123456789abcdef0123456789abcdef01234567")
    r, _ = sh('rework_instruction_start_sha "$INSTR"', env={"INSTR": out})
    assert r.stdout.strip() == "0123456789ab"


def test_pin_survives_pre_approval_validation_failure(tmp_path, sh):
    """워커가 커밋한 뒤 provenance 검증에서 실패해도 커밋은 refs/runner-artifacts 에 고정된다."""
    repo = _mkrepo(tmp_path / "r")
    base = _git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt))
    (wt / "f").write_text("worker\n")
    _git(wt, "commit", "-qam", "worker")
    sha = _git(wt, "rev-parse", "HEAD")
    body = (f"verify_isolated_job_worktree() {{ return 0; }}\nverify_worker_commit_provenance() {{ return 1; }}\n"
            f"commit_job_worktree_for_approval runner-bbbb2222 sess {wt} {repo} instr {base}")
    r, log = sh(body)
    assert r.returncode == 1 and "FAIL approval_commit_provenance_mismatch" in log
    assert r.stdout == ""
    assert _git(repo, "rev-parse", "refs/runner-artifacts/runner-bbbb2222") == sha


def test_runner_wiring():
    t = RUNNER.read_text(encoding="utf-8")
    assert t.index("ensure_rework_base_or_fail \"$job_id\"") < t.index("prepare_clean_job_worktree \"$job_id\" \"$project\"")
    assert "runner_artifact_pin \"$job_id\" \"$worktree_dir\" \"$pre_exec_sha\"" in t
    assert "runner_artifact_pin \"$job_id\" \"$worktree_dir\" \"$base_sha\" || true" in t
    # run_job 의 세 정리 경로는 작업 시작 SHA 를 _preserve_worktree_patch 로 넘긴다(reject_job 만 merge-base 로 추정)
    assert t.count('_preserve_worktree_patch "$job_id" "$worktree_dir" "${pre_exec_sha:-}"') == 2
    assert '_preserve_worktree_patch "$job_id" "/tmp/aads-wt-${job_id}" "${pre_exec_sha:-}"' in t
    # 워커 종료 직후(검수·diff 캡처 전)에도 고정한다
    assert t.index('_current_head=$(git rev-parse HEAD 2>/dev/null) || _current_head=""\n    runner_artifact_pin') > 0
    body = _extract("commit_job_worktree_for_approval")
    assert body.index("runner_artifact_pin") < body.index("verify_isolated_job_worktree")


@pytest.mark.parametrize("name", ["pipeline-runner.sh", "pipeline-runner.sh.local"])
def test_cli_failure_pins_artifact_before_db_update_so_commit_hash_is_recorded(name):
    """run_job CLI 실패 경로: ref 고정이 error UPDATE 보다 먼저여야 commit_sql 이 SHA 를 싣는다."""
    t = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    marker = '$(runner_artifact_commit_sql "$job_id")error_detail=${safe_error_detail},'
    assert t.count(marker) == 1
    upd = t.index(marker)
    pin = t.rindex('runner_artifact_pin "$job_id" "${worktree_dir:-/tmp/aads-wt-${job_id}}"', 0, upd)
    assert upd - pin < 800  # 같은 실패 블록 안에서 UPDATE 직전에 고정한다


# ── 지적 1(라운드 2): 산출물 없는 실패에 시작 HEAD 를 ref 로 고정하지 않고, 고정했다면 DB 와 같은 SHA ──
def _preserve_fn(tmp_path):
    return _extract("_preserve_worktree_patch").replace("/root/aads/runner-artifacts", str(tmp_path / "arts"))


def _ref_exists(repo, job):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/runner-artifacts/{job}"],
                          capture_output=True).returncode == 0


def test_preserve_without_commit_does_not_pin_start_head_even_without_base(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    _git(repo, "branch", "-M", "main")
    base = _git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt))
    fn = _preserve_fn(tmp_path)
    # 작업 시작 SHA 를 받은 경우(run_job CLI 실패 경로)
    r, _ = sh(f"{fn}\n_preserve_worktree_patch runner-dddd0001 {wt} {base}")
    assert r.returncode == 0
    assert not _ref_exists(repo, "runner-dddd0001")
    # 시작 SHA 를 모르는 호출(reject_job): merge-base == HEAD 이면 산출물 없음
    r, _ = sh(f"{fn}\n_preserve_worktree_patch runner-dddd0002 {wt}")
    assert r.returncode == 0
    assert not _ref_exists(repo, "runner-dddd0002")


def test_cli_failure_without_commit_leaves_no_ref_and_empty_commit_sql(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    base = _git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt))
    fn = _preserve_fn(tmp_path)
    r, _ = sh(f"{fn}\nrunner_artifact_pin runner-dddd0003 {wt} {base}\n"
              f"_preserve_worktree_patch runner-dddd0003 {wt} {base}\n"
              f"echo \"SQL[$(runner_artifact_commit_sql runner-dddd0003)]\"")
    assert "SQL[]" in r.stdout
    assert not _ref_exists(repo, "runner-dddd0003")


def test_cli_failure_with_commit_pins_once_and_db_sql_carries_same_sha(tmp_path, sh):
    repo = _mkrepo(tmp_path / "r")
    base = _git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt))
    (wt / "f").write_text("worker\n")
    _git(wt, "commit", "-qam", "worker")
    sha = _git(wt, "rev-parse", "HEAD")
    fn = _preserve_fn(tmp_path)
    r, _ = sh(f"{fn}\nrunner_artifact_pin runner-dddd0004 {wt} {base}\n"
              f"echo \"SQL[$(runner_artifact_commit_sql runner-dddd0004)]\"\n"
              f"_preserve_worktree_patch runner-dddd0004 {wt} {base}")
    assert f"commit_hash=COALESCE(NULLIF(commit_hash,''),'{sha}')" in r.stdout
    assert _git(repo, "rev-parse", "refs/runner-artifacts/runner-dddd0004") == sha


def test_repo_fallback_after_missing_worktree(peer_env, sh):
    peer, local, env, sshlog = peer_env
    sha = _peer_commit(peer)
    before = _git(peer, 'show-ref')
    r, log = sh(f"runner_artifact_fetch_from {local} mesh-42 runner-aaaaaaaa {sha[:12]}", env=env)
    assert r.returncode == 0, log + r.stderr
    assert 'source=repo:' in log
    assert _git(local, 'rev-parse', 'refs/runner-artifacts/runner-aaaaaaaa') == sha
    assert _git(peer, 'show-ref') == before
    assert 'cat-file -e' in sshlog.read_text()
    assert 'incoming-' not in _git(local, 'for-each-ref', '--format=%(refname)')


def test_repo_without_matching_ref_fails_closed(peer_env, sh):
    peer, local, env, _ = peer_env
    sha = _peer_commit(peer)
    # 존재하는 commit 이지만 refs/* 에서 광고하지 않는 경우를 모킹한다.
    body = f'''timeout() {{ shift; "$@"; }}
    git() {{
        if [[ "$*" == *ls-remote* ]]; then return 0; fi
        command git "$@"
    }}
    ensure_rework_base_or_fail runner-n1 sess {local} $'{_instr(sha)}'
    '''
    r, log = sh(body, env=env, db="printf '%s\\x1e%s\\x1e%s' '' vmi3725547 0")
    assert r.returncode == 1
    assert 'REWORK_BASE_REPO_NO_REF' in log and 'rework_base_unavailable' in log


def test_repo_moved_ref_is_rejected_and_next_ref_tried(peer_env, sh):
    peer, local, env, _ = peer_env
    sha = _peer_commit(peer)
    wrong = _git(local, 'rev-parse', 'HEAD')
    _git(peer, 'update-ref', 'refs/heads/other', sha)
    body = f'''timeout() {{ shift; "$@"; }}
    git() {{
        if [[ "$*" == *ls-remote* ]]; then
            printf '%s\\t%s\\n' {sha} refs/heads/runner-aaaaaaaa {sha} refs/heads/other
        elif [[ "$*" == *fetch* && "$*" == *+refs/heads/runner-aaaaaaaa:* ]]; then
            local spec="${{@: -1}}"
            command git -C {local} update-ref "${{spec##*:}}" {wrong}
        else command git "$@"; fi
    }}
    runner_artifact_fetch_from_repo {local} mesh-42 runner-aaaaaaaa {sha[:12]}
    '''
    r, log = sh(body, env=env)
    assert r.returncode == 0, log + r.stderr
    assert 'REWORK_BASE_FETCH_MISMATCH' in log and 'ref=refs/heads/other' in log
    assert _git(local, 'rev-parse', 'refs/runner-artifacts/runner-aaaaaaaa') == sha
    assert 'incoming-' not in _git(local, 'for-each-ref', '--format=%(refname)')


def test_repo_invalid_sha_does_not_contact_remote(peer_env, sh):
    _, local, env, sshlog = peer_env
    r, _ = sh(f"runner_artifact_fetch_from_repo {local} mesh-42 runner-aaaaaaaa invalid", env=env)
    assert r.returncode == 1 and not sshlog.read_text()


def test_existing_parent_worktree_keeps_original_path(peer_env, sh):
    peer, local, env, sshlog = peer_env
    sha = _peer_commit(peer)
    body = f'''timeout() {{ shift; "$@"; }}
    git() {{
        if [[ "$*" == *fetch* && "$*" == *HEAD:* ]]; then
            command git -C {local} fetch --no-tags -q {peer} {sha}
            local spec="${{@: -1}}"
            command git -C {local} update-ref "${{spec##*:}}" {sha}
        else command git "$@"; fi
    }}
    runner_artifact_fetch_from {local} mesh-42 runner-aaaaaaaa {sha[:12]}
    '''
    r, log = sh(body, env=env)
    assert r.returncode == 0, log + r.stderr
    assert 'source=/tmp/aads-wt-runner-aaaaaaaa' in log
    assert 'source=repo:' not in log and 'cat-file -e' not in sshlog.read_text()
