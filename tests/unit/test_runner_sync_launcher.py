"""러너 원격 동기화 원본 = origin/main 깨끗한 export (AADS-RUNNER-SYNC-SOURCE-ORIGIN-MAIN, 2026-10-03).

2026-10-02 08:55 KST 부터 공유 체크아웃(HEAD 가 origin/main 보다 62커밋 뒤, 미커밋 24건)의
파일이 동기화 원본이라 sync 가 257회 넘게 defer 했고, 체크아웃이 정리되면 옛 HEAD 의
pipeline-runner.sh 로 contabo14 를 되돌릴 위험이 있었다.

여기서는 실제 git 저장소 3개(origin / 더럽고 뒤처진 공유 체크아웃 / 임시 export)로 런처와
실제 sync 스크립트를 끝까지 돌리고, ssh/scp/docker 만 가짜로 바꿔 "무엇이 원격에 설치됐나"를 본다.
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "runner_sync_launcher.sh"
SYNC = ROOT / "scripts" / "sync_pipeline_runner_remote.sh"

# 런처가 export 하는 파일(런처의 REQUIRED_FILES + 유닛 파일 글롭)
SUPPORT_FILES = [
    "scripts/sync_pipeline_runner_remote.sh",
    "scripts/runner_busy_lib.sh",
    "scripts/claude_model_contract.py",
    "scripts/runner_cli_usage.py",
    "scripts/reclaim_runner_worktrees.sh",
    "scripts/aads-pipeline-litellm-runner.114.service",
    "scripts/aads-pipeline-litellm-runner.211.service",
    "scripts/aads-pipeline-runner.244.service",
    "scripts/aads-pipeline-runner.service",
    "tools/aag/brief.py",
]

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

# 원격 호스트 흉내 — $FAKE_REMOTE 디렉터리가 호스트 파일시스템이다(basename 으로 키).
_FAKE_SSH = r"""#!/usr/bin/env bash
host="${@: -2:1}"; cmd="${@: -1}"
echo "SSH $cmd" >> "$FAKE_LOG"
case "$cmd" in
    *"hostname -s"*) echo "$host" ;;
    *"systemctl is-active"*) echo active ;;
    sha256sum*)
        path=$(sed -n "s/^sha256sum '\([^']*\)'.*/\1/p" <<<"$cmd")
        f="$FAKE_REMOTE/$(basename "$path")"
        [[ -f "$f" ]] && sha256sum "$f" | awk '{print $1}'
        ;;
    install\ *)
        if [[ "$cmd" =~ install\ -m\ \'?[0-9]+\'?\ \'([^\']+)\'\ \'([^\']+)\' ]]; then
            cp "$FAKE_REMOTE/staged/$(basename "${BASH_REMATCH[1]}")" "$FAKE_REMOTE/$(basename "${BASH_REMATCH[2]}")"
            echo "INSTALL $(basename "${BASH_REMATCH[2]}")" >> "$FAKE_LOG"
        fi
        ;;
esac
exit 0
"""

_FAKE_SCP = r"""#!/usr/bin/env bash
src="${@: -2:1}"; dest="${@: -1}"
echo "SCP $src" >> "$FAKE_LOG"
mkdir -p "$FAKE_REMOTE/staged"
cp "$src" "$FAKE_REMOTE/staged/$(basename "${dest#*:}")"
"""

_FAKE_DOCKER = """#!/usr/bin/env bash
echo 0
"""


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV}, check=True
    )
    return proc.stdout.strip()


def _write(base: Path, rel: str, data: bytes) -> None:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class Fixture:
    def __init__(self, tmp: Path, runner_v2: bytes, omit: tuple[str, ...] = ()):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.shared = tmp / "shared-checkout"
        self.remote = tmp / "remote"
        self.export_base = tmp / "exports"
        self.log = tmp / "fake.log"
        self.runner_v2 = runner_v2
        for d in (self.remote, self.export_base):
            d.mkdir()
        self.log.write_text("")

        _git(tmp, "init", "--bare", "-q", "-b", "main", str(self.origin))
        seed = tmp / "seed"
        _git(tmp, "clone", "-q", str(self.origin), str(seed))
        # C1: 옛 러너
        _write(seed, "scripts/pipeline-runner.sh", b"#!/usr/bin/env bash\necho v1-old\n")
        for rel in SUPPORT_FILES:
            _write(seed, rel, (ROOT / rel).read_bytes())
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "C1")
        _git(seed, "push", "-q", "origin", "main")
        # 공유 체크아웃은 C1 에 머문다
        _git(tmp, "clone", "-q", str(self.origin), str(self.shared))
        # C2: origin/main 의 최신 러너. 공유 체크아웃은 이것을 아직 모른다.
        _write(seed, "scripts/pipeline-runner.sh", runner_v2)
        for rel in omit:
            (seed / rel).unlink()
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "C2")
        _git(seed, "push", "-q", "origin", "main")
        self.origin_sha = _git(seed, "rev-parse", "HEAD")
        # 다른 세션의 미커밋 작업본
        _write(self.shared, "scripts/pipeline-runner.sh", b"#!/usr/bin/env bash\necho DIRTY-WORKING-COPY\n")
        _write(self.shared, "scripts/runner_busy_lib.sh", b"# dirty\n")
        (self.shared / "scripts" / "untracked-wip.txt").write_text("wip")

    def shared_snapshot(self) -> dict:
        index = self.shared / ".git" / "index"
        return {
            "head": _git(self.shared, "rev-parse", "HEAD"),
            "status": _git(self.shared, "status", "--porcelain"),
            "stash": _git(self.shared, "stash", "list"),
            "worktrees": _git(self.shared, "worktree", "list"),
            "index": hashlib.sha256(index.read_bytes()).hexdigest(),
            "files": {
                str(p.relative_to(self.shared)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((self.shared / "scripts").rglob("*"))
                if p.is_file()
            },
        }

    def preload_remote_with_origin_main(self) -> None:
        names = {
            "pipeline-runner.sh": self.runner_v2,
            "claude_model_contract.py": (ROOT / "scripts/claude_model_contract.py").read_bytes(),
            "aag-brief.py": (ROOT / "tools/aag/brief.py").read_bytes(),
            "runner_cli_usage.py": (ROOT / "scripts/runner_cli_usage.py").read_bytes(),
            "reclaim_runner_worktrees.sh": (ROOT / "scripts/reclaim_runner_worktrees.sh").read_bytes(),
        }
        for name, data in names.items():
            (self.remote / name).write_bytes(data)

    def run(self, script: Path = LAUNCHER, extra_env: dict | None = None, args: tuple[str, ...] = ("--no-restart",)):
        fakebin = self.tmp / "bin"
        if not fakebin.exists():
            fakebin.mkdir()
            for name, body in (("ssh", _FAKE_SSH), ("scp", _FAKE_SCP), ("docker", _FAKE_DOCKER)):
                (fakebin / name).write_text(body, encoding="utf-8")
                (fakebin / name).chmod(0o755)
        env = {
            "PATH": f"{fakebin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmp),
            **GIT_ENV,
            "FAKE_LOG": str(self.log),
            "FAKE_REMOTE": str(self.remote),
            "AADS_RUNNER_SYNC_REPO": str(self.shared),
            "AADS_RUNNER_SYNC_EXPORT_BASE": str(self.export_base),
            "AADS_RUNNER_SYNC_LOCK": str(self.tmp / "sync.lock"),
            "AADS_RUNNER_SYNC_FAIL_STATE": str(self.tmp / "fail-count"),
            "AADS_RUNNER_SYNC_TARGETS": "hostA|hostA|/root/scripts/pipeline-runner.sh|aads-pipeline-runner.service|/nonexistent.service",
            **(extra_env or {}),
        }
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env, timeout=120)

    def log_lines(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture(autouse=True)
def _need_tools():
    for tool in ("bash", "git", "flock", "tar", "timeout"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} 미설치 환경")


def test_installs_origin_main_not_the_dirty_stale_shared_checkout(tmp_path):
    fx = Fixture(tmp_path, runner_v2=b"#!/usr/bin/env bash\necho v2-origin-main\n")
    before = fx.shared_snapshot()

    proc = fx.run()

    assert proc.returncode == 0, proc.stdout + proc.stderr
    installed = (fx.remote / "pipeline-runner.sh").read_bytes()
    assert installed == fx.runner_v2
    assert b"DIRTY" not in installed and b"v1-old" not in installed
    assert f"export origin/main={fx.origin_sha}" in proc.stdout
    assert f"source={fx.origin_sha}" in proc.stdout
    # 러너와 같은 디렉터리에 회수 스크립트도 함께 설치된다 (contabo14 에 없어 회수가 멈췄던 결함)
    assert (fx.remote / "reclaim_runner_worktrees.sh").read_bytes() == (ROOT / "scripts/reclaim_runner_worktrees.sh").read_bytes()
    # scp 로 올린 원본은 export 경로(공유 체크아웃 밖)에서 왔다
    scp_sources = [ln.split(" ", 1)[1] for ln in fx.log_lines() if ln.startswith("SCP ")]
    assert scp_sources
    assert all(str(fx.export_base) in src and str(fx.shared) not in src for src in scp_sources)
    # 임시 export 는 끝나면 사라진다
    assert list(fx.export_base.iterdir()) == []
    # 공유 체크아웃의 작업본·HEAD·index·stash·worktree 는 그대로다
    assert fx.shared_snapshot() == before


def test_fetch_failure_installs_nothing_and_exits_one(tmp_path):
    fx = Fixture(tmp_path, runner_v2=b"#!/usr/bin/env bash\necho v2\n")
    _git(fx.shared, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    before = fx.shared_snapshot()

    proc = fx.run()

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "nothing installed" in proc.stdout
    assert fx.log_lines() == []  # ssh/scp 한 번도 안 불렸다
    assert list(fx.remote.iterdir()) == []
    assert list(fx.export_base.iterdir()) == []
    assert (tmp_path / "fail-count").read_text() == "1"
    assert fx.shared_snapshot() == before


def test_missing_required_file_in_origin_main_installs_nothing(tmp_path):
    fx = Fixture(tmp_path, runner_v2=b"#!/usr/bin/env bash\necho v2\n", omit=("scripts/runner_cli_usage.py",))

    proc = fx.run()

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert fx.log_lines() == []
    assert list(fx.remote.iterdir()) == []
    assert list(fx.export_base.iterdir()) == []


def test_consecutive_failures_escalate_and_success_resets_counter(tmp_path):
    fx = Fixture(tmp_path, runner_v2=b"#!/usr/bin/env bash\necho v2\n")
    good_url = _git(fx.shared, "remote", "get-url", "origin")
    _git(fx.shared, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    for _ in range(2):
        proc = fx.run(extra_env={"AADS_RUNNER_SYNC_FAIL_ESCALATE_AFTER": "3"})
        assert "sync stalled" not in proc.stdout
    third = fx.run(extra_env={"AADS_RUNNER_SYNC_FAIL_ESCALATE_AFTER": "3"})
    assert third.returncode == 1
    assert "sync stalled: 3 consecutive" in third.stdout

    _git(fx.shared, "remote", "set-url", "origin", good_url)
    ok = fx.run()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert not (tmp_path / "fail-count").exists()


def test_noop_when_remote_already_matches_origin_main(tmp_path):
    """contabo14 가 이미 origin/main 의 러너(md5 046ec72bdcc2 상당)면 설치도 재시작도 없다."""
    fx = Fixture(tmp_path, runner_v2=(ROOT / "scripts" / "pipeline-runner.sh").read_bytes())
    fx.preload_remote_with_origin_main()
    remote_before = {p.name: p.read_bytes() for p in fx.remote.iterdir()}

    proc = fx.run(args=())  # 재시작 허용 모드에서도 no-op 이어야 한다

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "hostA=synced" in proc.stdout
    assert "hostA: already synced" in proc.stdout
    log = fx.log_lines()
    assert not [ln for ln in log if ln.startswith(("SCP ", "INSTALL "))]
    assert not [ln for ln in log if "systemctl restart" in ln or "cp -p" in ln]
    assert {p.name: p.read_bytes() for p in fx.remote.iterdir()} == remote_before


def test_direct_sync_invocation_is_rerouted_through_the_launcher(tmp_path):
    """체크아웃 안의 sync 스크립트를 직접 실행해도 작업본이 아니라 origin/main export 로 설치된다."""
    fx = Fixture(tmp_path, runner_v2=b"#!/usr/bin/env bash\necho v2-origin-main\n")

    proc = fx.run(script=SYNC)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"export origin/main={fx.origin_sha}" in proc.stdout
    assert (fx.remote / "pipeline-runner.sh").read_bytes() == fx.runner_v2


def test_help_does_not_fetch_or_export(tmp_path):
    fx = Fixture(tmp_path, runner_v2=b"#!/usr/bin/env bash\necho v2\n")
    _git(fx.shared, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    proc = fx.run(script=SYNC, args=("--help",))

    assert proc.returncode == 0
    assert "Usage:" in proc.stdout
    assert not (tmp_path / "fail-count").exists()


def test_launcher_never_mutates_shared_checkout_commands():
    """공유 체크아웃에 대해 fetch/archive/rev-parse 외 git 명령을 쓰지 않는다."""
    code = "\n".join(
        ln for ln in LAUNCHER.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")
    )
    assert not re.search(
        r"\bgit\b[^\n]*\s(stash|reset|checkout|pull|clean|restore|worktree|merge|rebase)\b", code
    )
    assert "git -C \"$SYNC_REPO\" -c gc.auto=0 fetch" in code
    assert "git -C \"$SYNC_REPO\" archive" in code
    assert "trap cleanup EXIT" in code
