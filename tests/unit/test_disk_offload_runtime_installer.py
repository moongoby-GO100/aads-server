"""install_disk_offload_runtime.sh: SHA 고정 실행본 설치·크론 전환·롤백 (임시 저장소 + 가짜 crontab)."""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install_disk_offload_runtime.sh"
REAL_SCRIPT = REPO_ROOT / "scripts" / "disk_offload_cafe24.sh"

LEGACY = "/root/aads/aads-server/scripts/disk_offload_cafe24.sh"
LEGACY_LINE = f"0 3 * * * {LEGACY} # aads-disk-offload"
CRON_BASE = (
    "PATH=/usr/local/bin:/usr/bin\n"
    "CRON_TZ=Europe/Berlin\n"
    "# 주석 줄은 그대로\n"
    "0 4 * * * /root/aads/scripts/disk_cleanup.sh >> /var/log/x.log 2>&1\n"
    f"{LEGACY_LINE}\n"
    "*/10 * * * * /root/aads/aads-server/scripts/disk_guard.sh >> /var/log/g.log 2>&1\n"
)

GOOD_SCRIPT = "#!/usr/bin/env bash\necho pinned-v1\n"

FAKE_CRONTAB = r"""#!/usr/bin/env bash
store="$FAKE_CRONTAB_FILE"
if [[ "${1:-}" == "-l" ]]; then
    [[ -f "$store" ]] || { echo "no crontab for root" >&2; exit 1; }
    cat "$store"; exit 0
fi
if [[ "${FAKE_CRONTAB_FAIL_WRITE:-0}" == 1 && -z "${FAKE_CRONTAB_FAILED_ONCE:-}" ]]; then
    exit 1
fi
if [[ "${FAKE_CRONTAB_CORRUPT_WRITE:-0}" == 1 && "$(grep -c 'runtime' "$1")" != 0 ]]; then
    { cat "$1"; echo "# corrupted"; } >"$store"; exit 0
fi
cp -- "$1" "$store"
"""


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def _commit_script(repo: Path, content: str, msg: str = "c") -> str:
    path = repo / "scripts" / "disk_offload_cafe24.sh"
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    sha1 = _commit_script(repo, GOOD_SCRIPT, "v1")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha1)
    crontab_bin = tmp_path / "bin" / "crontab"
    crontab_bin.parent.mkdir()
    crontab_bin.write_text(FAKE_CRONTAB)
    crontab_bin.chmod(0o755)
    store = tmp_path / "crontab.store"
    store.write_text(CRON_BASE)
    runtime = tmp_path / "runtime" / "disk-offload"
    return {
        "tmp": tmp_path, "repo": repo, "sha1": sha1, "store": store, "runtime": runtime,
        "vars": {
            **os.environ,
            "AADS_CRONTAB_BIN": str(crontab_bin),
            "FAKE_CRONTAB_FILE": str(store),
            "AADS_OFFLOAD_RUNTIME_REPO": str(repo),
            "AADS_OFFLOAD_RUNTIME_ROOT": str(runtime),
            "AADS_OFFLOAD_LOG": str(tmp_path / "logs" / "offload.log"),
        },
    }


def run(env, *args, extra_env=None):
    e = {**env["vars"], **(extra_env or {})}
    return subprocess.run(["bash", str(INSTALLER), *args], capture_output=True, text=True, env=e, timeout=120)


def records(env):
    return sorted((env["runtime"] / "state" / "records").glob("*.rec")) if (env["runtime"] / "state" / "records").exists() else []


def rec_field(rec: Path, key: str) -> str:
    for line in rec.read_text().splitlines():
        if line.startswith(key + "="):
            return line[len(key) + 1:]
    raise AssertionError(f"{key} missing in {rec}")


def new_release(env, text, msg):
    sha = _commit_script(env["repo"], GOOD_SCRIPT + text, msg)
    _git(env["repo"], "update-ref", "refs/remotes/origin/main", sha)
    return sha


def cron_target(env):
    return f"0 3 * * * {env['runtime']}/current/run.sh # aads-disk-offload"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_plan_changes_nothing_and_prints_pins(env):
    r = run(env, "--sha", env["sha1"])
    assert r.returncode == 0, r.stderr
    assert not env["runtime"].exists()
    assert env["store"].read_text() == CRON_BASE
    out = r.stdout
    assert f"pinned_sha:       {env['sha1']}" in out
    assert sha256_of_text(GOOD_SCRIPT) in out
    assert f"cron.old:         {LEGACY_LINE}" in out
    assert f"cron.new:         0 3 * * * {env['runtime']}/current/run.sh # aads-disk-offload" in out
    assert "rollback:" in out and "--rollback" in out
    assert "CRON_TZ=Europe/Berlin" in out


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_install_only_stages_release_without_touching_current_or_cron(env):
    r = run(env, "--sha", env["sha1"], "--install")
    assert r.returncode == 0, r.stderr
    rel = env["runtime"] / env["sha1"]
    assert (rel / "disk_offload_cafe24.sh").read_text() == GOOD_SCRIPT
    assert (rel / "run.sh").exists() and (rel / "MANIFEST").exists()
    assert not (env["runtime"] / "current").exists()
    assert env["store"].read_text() == CRON_BASE


def test_activate_switches_current_and_rewrites_only_the_marker_line(env):
    r = run(env, "--sha", env["sha1"], "--install", "--activate")
    assert r.returncode == 0, r.stderr
    assert (env["runtime"] / "current").is_symlink()
    assert os.readlink(env["runtime"] / "current") == env["sha1"]
    new_line = f"0 3 * * * {env['runtime']}/current/run.sh # aads-disk-offload"
    assert env["store"].read_text() == CRON_BASE.replace(LEGACY_LINE, new_line)
    backups = list((env["runtime"] / "backups").glob("crontab.*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == CRON_BASE
    recs = records(env)
    assert len(recs) == 1
    assert rec_field(recs[0], "prev_current") == "NONE"
    assert rec_field(recs[0], "cron_changed") == "1"
    assert rec_field(recs[0], "prev_cron_line") == LEGACY_LINE


def test_activate_is_idempotent_and_never_duplicates_entries(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    after_first = env["store"].read_text()
    r = run(env, "--sha", env["sha1"], "--install", "--activate")
    assert r.returncode == 0, r.stderr
    assert "already active" in r.stdout
    assert env["store"].read_text() == after_first
    assert after_first.count("aads-disk-offload") == 1
    assert len(list((env["runtime"] / "backups").glob("crontab.*.bak"))) == 1
    recs = records(env)
    assert len(recs) == 1 and rec_field(recs[0], "prev_cron_line") == LEGACY_LINE


def test_commit_not_in_origin_main_is_refused(env):
    local_only = _commit_script(env["repo"], GOOD_SCRIPT + "echo local\n", "unpushed")
    r = run(env, "--sha", local_only, "--install", "--activate")
    assert r.returncode != 0
    assert "not contained in origin/main" in r.stderr
    assert not env["runtime"].exists()
    assert env["store"].read_text() == CRON_BASE


def test_short_sha_or_ref_is_refused(env):
    for bad in (env["sha1"][:12], "origin/main", "HEAD"):
        r = run(env, "--sha", bad)
        assert r.returncode != 0
        assert "40-hex" in r.stderr


def test_dirty_worktree_content_is_never_installed(env):
    (env["repo"] / "scripts" / "disk_offload_cafe24.sh").write_text("#!/usr/bin/env bash\necho DIRTY\n")
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    assert (env["runtime"] / env["sha1"] / "disk_offload_cafe24.sh").read_text() == GOOD_SCRIPT


def test_syntax_error_in_pinned_script_refused_and_current_preserved(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    before_cron = env["store"].read_text()
    bad = _commit_script(env["repo"], "#!/usr/bin/env bash\nif then fi (\n", "bad")
    _git(env["repo"], "update-ref", "refs/remotes/origin/main", bad)
    r = run(env, "--sha", bad, "--install", "--activate")
    assert r.returncode != 0
    assert "bash -n failed" in r.stderr
    assert os.readlink(env["runtime"] / "current") == env["sha1"]
    assert not (env["runtime"] / bad).exists()
    assert env["store"].read_text() == before_cron


def test_expect_sha256_mismatch_refused_and_current_preserved(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    v2 = _commit_script(env["repo"], GOOD_SCRIPT + "echo v2\n", "v2")
    _git(env["repo"], "update-ref", "refs/remotes/origin/main", v2)
    r = run(env, "--sha", v2, "--expect-sha256", "0" * 64, "--install", "--activate")
    assert r.returncode != 0 and "--expect-sha256" in r.stderr
    assert os.readlink(env["runtime"] / "current") == env["sha1"]
    assert not (env["runtime"] / v2).exists()
    assert not [p for p in env["runtime"].iterdir() if p.name.startswith(".stage")]


def test_sourced_dependency_is_refused(env):
    dep = _commit_script(env["repo"], "#!/usr/bin/env bash\nsource ./lib.sh\n", "dep")
    _git(env["repo"], "update-ref", "refs/remotes/origin/main", dep)
    r = run(env, "--sha", dep, "--install")
    assert r.returncode != 0 and "sources another file" in r.stderr


def test_activate_without_staged_release_fails_closed(env):
    r = run(env, "--sha", env["sha1"], "--activate")
    assert r.returncode != 0
    assert "not staged" in r.stderr
    assert env["store"].read_text() == CRON_BASE


def test_tampered_staged_release_is_refused_at_activate(env):
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    script = env["runtime"] / env["sha1"] / "disk_offload_cafe24.sh"
    script.chmod(0o755)
    script.write_text("#!/usr/bin/env bash\necho tampered\n")
    r = run(env, "--sha", env["sha1"], "--activate")
    assert r.returncode != 0
    assert not (env["runtime"] / "current").exists()
    assert env["store"].read_text() == CRON_BASE


def test_cron_write_failure_restores_current_and_crontab(env):
    # 이전 current(v1) 가 있는 상태에서 v2 활성화 중 crontab 쓰기가 실패하는 경우.
    assert run(env, "--sha", env["sha1"], "--install", "--activate", "--skip-cron").returncode == 0
    v2 = _commit_script(env["repo"], GOOD_SCRIPT + "echo v2\n", "v2")
    _git(env["repo"], "update-ref", "refs/remotes/origin/main", v2)
    r = run(env, "--sha", v2, "--install", "--activate", extra_env={"FAKE_CRONTAB_FAIL_WRITE": "1"})
    assert r.returncode != 0
    assert os.readlink(env["runtime"] / "current") == env["sha1"]
    assert env["store"].read_text() == CRON_BASE
    assert len(records(env)) == 1  # v1 기록만 남고 v2 시도 기록은 정리됨


def test_cron_readback_mismatch_restores_everything(env):
    r = run(env, "--sha", env["sha1"], "--install", "--activate", extra_env={"FAKE_CRONTAB_CORRUPT_WRITE": "1"})
    assert r.returncode != 0
    assert not (env["runtime"] / "current").exists()
    assert env["store"].read_text() == CRON_BASE
    assert records(env) == []


def test_duplicate_marker_lines_refused_before_any_change(env):
    env["store"].write_text(CRON_BASE + LEGACY_LINE + "\n")
    before = env["store"].read_text()
    r = run(env, "--sha", env["sha1"], "--install", "--activate")
    assert r.returncode != 0 and "exactly one" in r.stderr
    assert not (env["runtime"] / "current").exists()
    assert env["store"].read_text() == before


def test_unmarked_second_reference_is_refused(env):
    env["store"].write_text(CRON_BASE + f"30 5 * * * {LEGACY}\n")
    r = run(env, "--sha", env["sha1"], "--install", "--activate")
    assert r.returncode != 0 and "without the marker" in r.stderr
    assert not (env["runtime"] / "current").exists()


def test_missing_entry_is_not_added(env):
    env["store"].write_text("0 4 * * * /root/aads/scripts/disk_cleanup.sh\n")
    r = run(env, "--sha", env["sha1"], "--install", "--activate")
    assert r.returncode != 0
    assert env["store"].read_text() == "0 4 * * * /root/aads/scripts/disk_cleanup.sh\n"


def test_rollback_v1_v2_keeps_cron_on_pinned_runtime_not_legacy(env):
    # 지적 1: v1 활성화 -> v2 활성화 -> 롤백. current=v1 이고 크론도 고정 실행본(current/run.sh)이어야 한다.
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    cron_after_v1 = env["store"].read_text()
    v2 = new_release(env, "echo v2\n", "v2")
    assert run(env, "--sha", v2, "--install", "--activate").returncode == 0
    assert os.readlink(env["runtime"] / "current") == v2
    assert len(records(env)) == 2
    r = run(env, "--rollback")
    assert r.returncode == 0, r.stderr
    assert os.readlink(env["runtime"] / "current") == env["sha1"]
    assert env["store"].read_text() == cron_after_v1
    assert LEGACY not in env["store"].read_text()
    assert len(records(env)) == 1
    # 두 번째 롤백은 v1 활성화 자체를 되돌린다: 원본 크론 줄 + current 없음.
    r = run(env, "--rollback")
    assert r.returncode == 0, r.stderr
    assert env["store"].read_text() == CRON_BASE
    assert not (env["runtime"] / "current").exists()
    assert "nothing to roll back" in run(env, "--rollback").stdout


def test_rollback_after_first_activation_returns_to_legacy_line_and_no_current(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    # 롤백 사이에 다른 크론 줄이 추가돼도 보존된다.
    env["store"].write_text(env["store"].read_text() + "5 5 * * * /bin/true\n")
    r = run(env, "--rollback")
    assert r.returncode == 0, r.stderr
    assert env["store"].read_text() == CRON_BASE + "5 5 * * * /bin/true\n"
    assert not (env["runtime"] / "current").exists()


def test_rollback_chain_of_three_restores_each_prestate(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    v2 = new_release(env, "echo v2\n", "v2")
    v3 = new_release(env, "echo v3\n", "v3")
    assert run(env, "--sha", v2, "--install", "--activate").returncode == 0
    assert run(env, "--sha", v3, "--install", "--activate").returncode == 0
    for expect in (v2, env["sha1"]):
        assert run(env, "--rollback").returncode == 0
        assert os.readlink(env["runtime"] / "current") == expect
        assert env["store"].read_text().count(cron_target(env)) == 1
    assert run(env, "--rollback").returncode == 0
    assert env["store"].read_text() == CRON_BASE


def test_failed_activation_does_not_destroy_previous_rollback_record(env):
    # 지적 2: v1 --skip-cron 활성화 뒤 v2 의 크론 read-back 실패 -> v1 의 롤백 기록이 남아 있어야 한다.
    assert run(env, "--sha", env["sha1"], "--install", "--activate", "--skip-cron").returncode == 0
    v1_recs = records(env)
    assert len(v1_recs) == 1 and rec_field(v1_recs[0], "prev_current") == "NONE"
    v1_text = v1_recs[0].read_text()
    v2 = new_release(env, "echo v2\n", "v2")
    r = run(env, "--sha", v2, "--install", "--activate", extra_env={"FAKE_CRONTAB_CORRUPT_WRITE": "1"})
    assert r.returncode != 0
    assert os.readlink(env["runtime"] / "current") == env["sha1"]
    assert env["store"].read_text() == CRON_BASE
    assert records(env) == v1_recs and v1_recs[0].read_text() == v1_text
    r = run(env, "--rollback")
    assert r.returncode == 0 and "nothing to roll back" not in r.stdout
    assert not (env["runtime"] / "current").exists()
    assert env["store"].read_text() == CRON_BASE


def test_skip_cron_activation_rollback_leaves_cron_alone(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    v2 = new_release(env, "echo v2\n", "v2")
    assert run(env, "--sha", v2, "--install", "--activate", "--skip-cron").returncode == 0
    recs = records(env)
    assert rec_field(recs[-1], "cron_changed") == "0"
    before = env["store"].read_text()
    assert run(env, "--rollback").returncode == 0
    assert env["store"].read_text() == before
    assert os.readlink(env["runtime"] / "current") == env["sha1"]


def test_unwritable_rollback_record_aborts_before_any_change(env):
    # 지적 3: 복원 정보 저장이 실패하면 current·crontab 을 바꾸지 않고 비정상 종료해야 한다.
    state = env["runtime"] / "state" / "records"
    state.mkdir(parents=True)
    (state / "00000001.rec.new").mkdir()  # 같은 이름의 디렉터리 -> 파일 쓰기 실패 주입
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    r = run(env, "--sha", env["sha1"], "--activate")
    assert r.returncode != 0
    assert "cannot save rollback record" in r.stderr
    assert "activated pinned" not in r.stdout
    assert not (env["runtime"] / "current").exists()
    assert env["store"].read_text() == CRON_BASE
    assert not list(state.glob("*.rec"))


def test_unwritable_state_dir_aborts_before_any_change(env):
    (env["runtime"] / "state").mkdir(parents=True)
    (env["runtime"] / "state" / "records").write_text("not a directory")
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    r = run(env, "--sha", env["sha1"], "--activate")
    assert r.returncode != 0 and "cannot create state/backup dirs" in r.stderr
    assert not (env["runtime"] / "current").exists()
    assert env["store"].read_text() == CRON_BASE


def test_rollback_aborts_before_any_change_when_archive_dir_unavailable(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    (env["runtime"] / "state" / "rolled_back").write_text("not a directory")
    after = env["store"].read_text()
    r = run(env, "--rollback")
    assert r.returncode != 0 and "nothing was changed" in r.stderr
    assert os.readlink(env["runtime"] / "current") == env["sha1"]
    assert env["store"].read_text() == after
    assert len(records(env)) == 1


def test_rollback_reports_failure_when_record_cannot_be_archived(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    shim = env["tmp"] / "shim"
    shim.mkdir()
    real_mv = subprocess.run(["bash", "-c", "command -v mv"], capture_output=True, text=True).stdout.strip()
    (shim / "mv").write_text(f'#!/usr/bin/env bash\nfor a in "$@"; do [[ "$a" == *rolled_back* ]] && exit 1; done\nexec {real_mv} "$@"\n')
    (shim / "mv").chmod(0o755)
    r = run(env, "--rollback", extra_env={"PATH": f"{shim}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "ROLLED BACK, but could not archive" in r.stderr
    assert "rollback complete" not in r.stdout
    assert len(records(env)) == 1


def test_skip_cron_switches_pointer_only(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate", "--skip-cron").returncode == 0
    assert os.readlink(env["runtime"] / "current") == env["sha1"]
    assert env["store"].read_text() == CRON_BASE


def test_runtime_root_inside_repo_is_refused(env):
    r = run(env, "--sha", env["sha1"], "--install", extra_env={"AADS_OFFLOAD_RUNTIME_ROOT": str(env["repo"] / "runtime")})
    assert r.returncode != 0 and "inside the git worktree" in r.stderr


def test_no_crontab_at_all_is_reported_not_created(env):
    env["store"].unlink()
    r = run(env, "--sha", env["sha1"], "--install", "--activate")
    assert r.returncode != 0 and "exactly one" in r.stderr
    assert not env["store"].exists()


def test_launcher_logs_version_and_runs_pinned_script(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    r = subprocess.run(["bash", str(env["runtime"] / "current" / "run.sh")], capture_output=True, text=True, env=env["vars"])
    assert r.returncode == 0 and "pinned-v1" in r.stdout
    log = (env["tmp"] / "logs" / "offload.log").read_text()
    assert f"RUNTIME pinned sha={env['sha1']} script_sha256={sha256_of_text(GOOD_SCRIPT)}" in log


def test_launcher_refuses_tampered_script(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    script = env["runtime"] / env["sha1"] / "disk_offload_cafe24.sh"
    script.chmod(0o755)
    script.write_text("#!/usr/bin/env bash\necho tampered\n")
    r = subprocess.run(["bash", str(env["runtime"] / "current" / "run.sh")], capture_output=True, text=True, env=env["vars"])
    assert r.returncode == 1 and "tampered" not in r.stdout
    assert "pinned runtime verification failed" in (env["tmp"] / "logs" / "offload.log").read_text()


def test_installed_release_is_immutable_on_reinstall(env):
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    script = env["runtime"] / env["sha1"] / "disk_offload_cafe24.sh"
    script.chmod(0o755)
    script.write_text("#!/usr/bin/env bash\necho tampered\n")
    r = run(env, "--sha", env["sha1"], "--install")
    assert r.returncode != 0 and "immutable" in r.stderr
    assert "tampered" in script.read_text()


def test_real_script_installs_from_this_repo_history_without_dependencies(env):
    head = subprocess.run(["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%H", "--", "scripts/disk_offload_cafe24.sh"],
                          capture_output=True, text=True).stdout.strip()
    if not head:
        pytest.skip("script history not available")
    e = {**env["vars"], "AADS_OFFLOAD_RUNTIME_REPO": str(REPO_ROOT), "AADS_OFFLOAD_RUNTIME_BASE_REF": "HEAD"}
    r = subprocess.run(["bash", str(INSTALLER), "--sha", head, "--install"], capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stderr
    staged = env["runtime"] / head / "disk_offload_cafe24.sh"
    committed = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{head}:scripts/disk_offload_cafe24.sh"], capture_output=True).stdout
    assert staged.read_bytes() == committed


def test_installer_and_launcher_pass_bash_syntax_check(env):
    assert subprocess.run(["bash", "-n", str(INSTALLER)]).returncode == 0
    assert subprocess.run(["bash", "-n", str(REAL_SCRIPT)]).returncode == 0
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    assert subprocess.run(["bash", "-n", str(env["runtime"] / env["sha1"] / "run.sh")]).returncode == 0


# ---- 리뷰 지적 1: current 가 외부 경로의 동일 SHA 디렉터리를 가리키는 경우 -------------------------
def test_current_pointing_outside_runtime_with_same_sha_basename_is_refused(env):
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    outside = env["tmp"] / "elsewhere" / env["sha1"]
    outside.mkdir(parents=True)
    (env["runtime"] / "current").symlink_to(outside)
    r = run(env, "--sha", env["sha1"], "--activate")
    assert r.returncode != 0
    assert "not a canonical pointer" in r.stderr
    assert os.readlink(env["runtime"] / "current") == str(outside)
    assert env["store"].read_text() == CRON_BASE
    assert records(env) == []
    assert not (env["runtime"] / "current" / "run.sh").exists()


def test_plan_flags_foreign_current_pointer(env):
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    outside = env["tmp"] / "elsewhere" / env["sha1"]
    outside.mkdir(parents=True)
    (env["runtime"] / "current").symlink_to(outside)
    r = run(env, "--sha", env["sha1"])
    assert r.returncode == 0 and "NOT a canonical pointer" in r.stdout


def test_absolute_canonical_current_is_accepted_and_normalised(env):
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    (env["runtime"] / "current").symlink_to(env["runtime"] / env["sha1"])
    r = run(env, "--sha", env["sha1"], "--activate")
    assert r.returncode == 0, r.stderr
    assert (env["runtime"] / "current" / "run.sh").exists()
    assert env["store"].read_text() == CRON_BASE.replace(LEGACY_LINE, cron_target(env))


# ---- 리뷰 지적 2: current 가 기존 일반 파일/디렉터리인 경우 복구가 지우면 안 된다 -----------------
def test_regular_file_current_is_refused_and_survives(env):
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    cur = env["runtime"] / "current"
    cur.write_text("precious\n")
    r = run(env, "--sha", env["sha1"], "--activate")
    assert r.returncode != 0
    assert cur.read_text() == "precious\n" and not cur.is_symlink()
    assert env["store"].read_text() == CRON_BASE
    assert records(env) == []


def test_regular_file_current_survives_rollback(env):
    assert run(env, "--sha", env["sha1"], "--install", "--activate").returncode == 0
    cur = env["runtime"] / "current"
    cur.unlink()
    cur.write_text("precious\n")
    r = run(env, "--rollback")
    assert r.returncode != 0
    assert cur.read_text() == "precious\n"
    assert len(records(env)) == 1
    assert env["store"].read_text() == CRON_BASE.replace(LEGACY_LINE, cron_target(env))


def test_directory_current_is_refused_and_survives(env):
    assert run(env, "--sha", env["sha1"], "--install").returncode == 0
    cur = env["runtime"] / "current"
    cur.mkdir()
    (cur / "keep").write_text("x")
    r = run(env, "--sha", env["sha1"], "--activate")
    assert r.returncode != 0
    assert (cur / "keep").read_text() == "x"
    assert env["store"].read_text() == CRON_BASE
