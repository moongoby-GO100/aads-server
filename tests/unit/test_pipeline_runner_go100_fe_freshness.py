"""GO100 프론트 번들 신선도 게이트 (AADS-RUNNER-GO100-FE-FRESHNESS-GATE-20260930).

2026-09-30 contabo14 실측: runner-9c1f2658 배포가 frontend_health=OK 로 보고됐지만
포트 curl 200 만 본 것이고 활성 슬롯 번들은 구버전이었다.
  - go100-frontend.service 는 masked → `systemctl restart go100-frontend || true` 는 no-op
  - 실제 번들은 frontend/.next.blue|.next.green, frontend/.next 는 없음
    → 기존 BUILD_ID 비교는 한 번도 발화할 수 없었다.
  - 활성 슬롯은 nginx `upstream go100_frontend` 의 포트(3000=blue, 3001=green)가 정한다.

이 테스트는 두 가지를 고정한다.
  1) 스크립트 계약 — 정본 blue-green 스크립트 호출, npx/systemctl 경로 제거, 게이트 존재.
  2) 실제 동작 — 함수 블록만 추출해 슬롯 판정 3종 + 신선도 판정 4종을 검증.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMIT_EPOCH = 1790000000  # 2026-09-22 KST 부근, 고정값


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _go100_block(script: str) -> str:
    start = script.index("        GO100)\n            # GO100 API: systemd")
    end = script.index("        SF)\n", start)
    return script[start:end]


# ── 1) 스크립트 계약 ────────────────────────────────────────────────────


def test_go100_frontend_uses_canonical_bluegreen_script():
    block = _go100_block(_read_script())

    assert 'scripts/deploy_frontend_blue_green.sh"' in block
    assert 'bash "$_fe_bg_script" --apply 2>&1 | tail -30' in block
    assert "go100-frontend:bluegreen_script_missing" in block
    # masked 서비스 재시작, 활성 슬롯에 닿지 않는 직접 빌드는 다시 들어오면 안 된다
    assert "systemctl restart go100-frontend" not in block
    assert "npx next build" not in block
    assert "/.next/BUILD_ID" not in block


def test_go100_frontend_change_detection_kept():
    block = _go100_block(_read_script())

    assert 'diff "$_pre_sha" HEAD --name-only -- frontend/' in block
    assert 'log "  SKIP go100-frontend (no frontend changes since $_pre_sha)"' in block


def test_freshness_gate_only_runs_when_frontend_changed():
    block = _go100_block(_read_script())

    changed = block.index('if [ -n "$_fe_changed" ]; then')
    gate = block.index("verify_go100_bundle_fresh")
    skip = block.index("SKIP go100-frontend")
    assert changed < gate < skip


def test_freshness_gate_failure_codes_feed_existing_build_fail_path():
    script = _read_script()
    block = _go100_block(script)

    for code in ("stale_bundle", "build_id_missing", "active_slot_unknown"):
        assert f"go100-frontend:{code}" in block
    assert "go100-frontend bundle fresh (color=" in block
    # 새 흐름을 만들지 않고 기존 최종 판정 분기를 탄다
    assert 'if [[ -n "$_build_fail" ]]; then' in script
    assert "배포 부분 실패 — 빌드 실패 감지: ${_build_fail}" in script


def test_heartbeat_before_and_after_bluegreen():
    block = _go100_block(_read_script())
    hb = "UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='${job_id}' AND status='deploying';"

    call = block.index('bash "$_fe_bg_script" --apply')
    before = block.rindex(hb, 0, call)
    after = block.index(hb, call)
    assert before < call < after


def test_runner_scripts_stay_byte_identical():
    assert _read_script("pipeline-runner.sh") == _read_script("pipeline-runner.sh.local")


# ── 2) 실제 동작 ────────────────────────────────────────────────────────


NGINX_CONF_TEMPLATE = """\
upstream go100_api {{
    server 127.0.0.1:8002;
}}

upstream go100_frontend {{
    # server 127.0.0.1:3000;
    server 127.0.0.1:{port};
    keepalive 16;
}}

server {{
    listen 443 ssl;
    location / {{ proxy_pass http://go100_frontend; }}
}}
"""


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory):
    for tool in ("bash", "git", "awk", "stat", "touch"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} 미설치 환경 — 동작 테스트 생략")
    script = _read_script()
    body = "set -eo pipefail\n"
    body += _extract_function(script, "go100_active_frontend_color")
    body += _extract_function(script, "verify_go100_bundle_fresh")
    path = tmp_path_factory.mktemp("go100_fn") / "fn.sh"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "kis-autotrade-v4"
    (repo / "frontend").mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_AUTHOR_DATE": f"@{COMMIT_EPOCH} +0900",
        "GIT_COMMITTER_DATE": f"@{COMMIT_EPOCH} +0900",
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "README").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "merge"], check=True, env=env)
    return repo


def _conf(tmp_path: Path, port: str) -> Path:
    path = tmp_path / f"go100-{port}.conf"
    path.write_text(NGINX_CONF_TEMPLATE.format(port=port), encoding="utf-8")
    return path


def _bash(fn: Path, call: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "{fn}"; {call}'],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _build_id(repo: Path, color: str, epoch: int) -> None:
    d = repo / "frontend" / f".next.{color}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "BUILD_ID"
    f.write_text("AbCdEfGh12345678", encoding="utf-8")
    os.utime(f, (epoch, epoch))


@pytest.mark.parametrize(
    ("port", "expected"),
    [("3001", "green"), ("3000", "blue"), ("30x1", "")],
    ids=["case1_3001_green", "case2_3000_blue", "case3_broken_unknown"],
)
def test_active_color(fn_file, tmp_path, port, expected):
    conf = _conf(tmp_path, port)
    assert _bash(fn_file, f'go100_active_frontend_color "{conf}"') == expected


def test_case4_missing_conf_is_unknown_and_safe(fn_file, tmp_path):
    assert _bash(fn_file, f'go100_active_frontend_color "{tmp_path}/nope.conf"') == ""


def test_case5_green_bundle_older_than_commit_is_stale(fn_file, tmp_path, repo):
    _build_id(repo, "green", COMMIT_EPOCH - 3600)
    out = _bash(fn_file, f'verify_go100_bundle_fresh "{repo}" "{_conf(tmp_path, "3001")}"')
    assert out == f"stale|green|AbCdEfGh|{COMMIT_EPOCH - 3600}|{COMMIT_EPOCH}"


def test_case6_green_bundle_newer_than_commit_is_fresh(fn_file, tmp_path, repo):
    _build_id(repo, "green", COMMIT_EPOCH + 600)
    out = _bash(fn_file, f'verify_go100_bundle_fresh "{repo}" "{_conf(tmp_path, "3001")}"')
    assert out == f"fresh|green|AbCdEfGh|{COMMIT_EPOCH + 600}|{COMMIT_EPOCH}"


def test_case7_active_blue_without_build_id_is_missing(fn_file, tmp_path, repo):
    # green 만 새로 빌드되고 활성은 blue 인 경우 — green 번들을 보고 통과시키면 안 된다
    _build_id(repo, "green", COMMIT_EPOCH + 600)
    out = _bash(fn_file, f'verify_go100_bundle_fresh "{repo}" "{_conf(tmp_path, "3000")}"')
    assert out == "build_id_missing|blue|||"


def test_case8_broken_conf_is_active_slot_unknown(fn_file, tmp_path, repo):
    _build_id(repo, "green", COMMIT_EPOCH + 600)
    out = _bash(fn_file, f'verify_go100_bundle_fresh "{repo}" "{_conf(tmp_path, "30x1")}"')
    assert out == "active_slot_unknown||||"


# ── 3) clean 릴리스 워크트리 (AADS-RUNNER-GO100-FE-CLEAN-WORKTREE-20260930) ──
# 2026-09-30 14:18 BG 배포가 공유 런타임 워크트리(dirty, HEAD≠origin/main)에서 돌아
# Deploy Gate 가 막았다. push 한 SHA 의 clean 워크트리를 만들어 주입해야 한다.


def test_bluegreen_runs_in_release_worktree_of_pushed_sha():
    block = _go100_block(_read_script())

    create = block.index('go100_create_frontend_release_worktree "$GO100_REPO_DIR" "$current_sha"')
    call = block.index('bash "$_fe_bg_script" --apply')
    assert create < call
    assert 'GO100_RELEASE_WORKDIR="$_fe_release_dir"' in block
    assert "go100-frontend:release_worktree_failed" in block
    # 기존 라벨은 그대로, 생성 실패는 별도 코드
    assert "go100-frontend:build_failed" in block
    # 런타임 HEAD 를 배포 대상으로 쓰거나 게이트를 우회하면 안 된다
    assert "GO100_ALLOW_DIRTY_DEPLOY" not in block
    assert "GO100_GATE_SKIP" not in block


def test_release_worktree_failure_does_not_fall_back_to_runtime():
    block = _go100_block(_read_script())

    fail = block.index("go100-frontend:release_worktree_failed")
    call = block.index('bash "$_fe_bg_script" --apply')
    # 실패 분기가 먼저 오고 BG 호출은 그 else 안에만 있다(호출은 한 번)
    assert fail < call
    assert block.count('bash "$_fe_bg_script" --apply') == 1
    between = block[fail:call]
    assert "else" in between


def test_release_worktrees_pruned_after_deploy_regardless_of_result():
    block = _go100_block(_read_script())

    call = block.index('bash "$_fe_bg_script" --apply')
    prune_after = block.index('go100_prune_release_worktrees "$GO100_REPO_DIR" "$GO100_RELEASE_WORKTREE_KEEP"', call)
    gate = block.index("verify_go100_bundle_fresh")
    assert call < prune_after < gate


def _git(*args, cwd=None, env=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture()
def wt_fn_file(tmp_path):
    for tool in ("bash", "git", "awk", "df", "find", "cmp"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} 미설치 환경 — 동작 테스트 생략")
    script = _read_script()
    body = "set -eo pipefail\nlog() { echo \"$*\" >&2; }\n"
    for name in (
        "_go100_path_in_use",
        "_go100_remove_release_worktree",
        "go100_prune_release_worktrees",
        "go100_create_frontend_release_worktree",
    ):
        body += _extract_function(script, name)
    path = tmp_path / "wt_fn.sh"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def dirty_runtime(tmp_path):
    """origin + 러너 push 클론 + dirty 런타임(HEAD 가 origin/main 과 갈라짐)."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin), env=env)
    seed = tmp_path / "seed"
    _git("clone", "-q", str(origin), str(seed), env=env)
    (seed / "frontend").mkdir()
    (seed / "scripts").mkdir()
    (seed / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    (seed / "frontend" / "package-lock.json").write_text('{"lock":1}', encoding="utf-8")
    (seed / "frontend" / ".gitignore").write_text("/node_modules\n", encoding="utf-8")
    (seed / "scripts" / "deploy_frontend_blue_green.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    _git("add", "-A", cwd=seed, env=env)
    _git("commit", "-q", "-m", "base", cwd=seed, env=env)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed, env=env)

    runtime = tmp_path / "kis-autotrade-v4"
    _git("clone", "-q", str(origin), str(runtime), env=env)
    nm_bin = runtime / "frontend" / "node_modules" / ".bin"
    nm_bin.mkdir(parents=True)
    (nm_bin / "next").write_text("#!/bin/sh\n", encoding="utf-8")
    (nm_bin / "next").chmod(0o755)
    # 런타임만의 로컬 커밋(ahead=1) + 미커밋 변경(dirty)
    (runtime / "local.txt").write_text("local", encoding="utf-8")
    _git("add", "local.txt", cwd=runtime, env=env)
    _git("commit", "-q", "-m", "unrelated local", cwd=runtime, env=env)
    (runtime / "frontend" / "package.json").write_text('{"dirty":1}', encoding="utf-8")

    # 러너가 push 한 SHA (런타임은 아직 모른다 → behind=1)
    (seed / "frontend" / "page.tsx").write_text("export default 1\n", encoding="utf-8")
    _git("add", "-A", cwd=seed, env=env)
    _git("commit", "-q", "-m", "runner push", cwd=seed, env=env)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed, env=env)
    pushed = _git("rev-parse", "HEAD", cwd=seed)
    root = tmp_path / "opt-go100"
    root.mkdir()
    return runtime, pushed, root


def _wt_bash(fn, root, call, **extra):
    env = {**os.environ, "GO100_RELEASE_WORKTREE_ROOT": str(root),
           "GO100_RELEASE_WORKTREE_MIN_FREE_MB": "1", "GO100_RELEASE_WORKTREE_MIN_AGE_MIN": "0",
           "GO100_RELEASE_WORKTREE_KEEP": "2", **extra}
    body = (
        'GO100_RELEASE_WORKTREE_ROOT="${GO100_RELEASE_WORKTREE_ROOT}"\n'
        f'source "{fn}"\n{call}\n'
    )
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)


def test_release_worktree_is_clean_at_pushed_sha_even_if_runtime_dirty(wt_fn_file, dirty_runtime):
    runtime, pushed, root = dirty_runtime
    assert _git("status", "--porcelain", cwd=runtime)  # 전제: 런타임 dirty
    assert _git("rev-parse", "HEAD", cwd=runtime) != pushed  # 전제: 런타임 HEAD ≠ push SHA

    proc = _wt_bash(wt_fn_file, root, f'go100_create_frontend_release_worktree "{runtime}" "{pushed}"')
    assert proc.returncode == 0, proc.stderr
    wt = Path(proc.stdout.strip())
    assert wt.parent == root and wt.name.startswith(f"frontend-release-{pushed[:9]}-")
    assert _git("rev-parse", "HEAD", cwd=wt) == pushed
    assert _git("status", "--porcelain", cwd=wt) == ""  # 게이트 dirty 검사 통과 조건
    nm = wt / "frontend" / "node_modules"
    assert nm.is_symlink() and os.readlink(nm) == str(runtime / "frontend" / "node_modules")


def test_release_worktree_failure_reports_and_leaves_nothing(wt_fn_file, dirty_runtime):
    runtime, _pushed, root = dirty_runtime
    proc = _wt_bash(wt_fn_file, root, f'go100_create_frontend_release_worktree "{runtime}" "deadbeefdeadbeef"')
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""  # 경로를 내지 않으므로 호출측은 런타임으로 갈 수 없다
    assert "release_worktree:" in proc.stderr
    assert list(root.iterdir()) == []


def test_prune_keeps_latest_n_and_leaves_no_ghost_worktrees(wt_fn_file, dirty_runtime):
    runtime, pushed, root = dirty_runtime
    made = []
    for i in range(4):
        proc = _wt_bash(wt_fn_file, root, f'go100_create_frontend_release_worktree "{runtime}" "{pushed}"')
        assert proc.returncode == 0, proc.stderr
        made.append(Path(proc.stdout.strip()))
        os.utime(made[-1], (1790000000 + i, 1790000000 + i))
    stray = root / "frontend-release-3e1d4aa06.Rx6Wd7"  # 미등록 잔재
    stray.mkdir()
    os.utime(stray, (1780000000, 1780000000))
    other = root / "backend-releases"  # 패턴 밖은 건드리지 않는다
    other.mkdir()

    proc = _wt_bash(wt_fn_file, root, f'go100_prune_release_worktrees "{runtime}" 2')
    assert proc.returncode == 0, proc.stderr
    left = sorted(p.name for p in root.iterdir() if p.name.startswith("frontend-release-"))
    assert left == sorted(p.name for p in made[-2:])
    assert other.exists()
    # symlink 만 끊고 런타임 node_modules 는 그대로
    assert (runtime / "frontend" / "node_modules" / ".bin" / "next").exists()
    listed = _git("worktree", "list", "--porcelain", cwd=runtime)
    wts = [line.split(" ", 1)[1] for line in listed.splitlines() if line.startswith("worktree ")]
    assert sorted(wts) == sorted([str(runtime), *map(str, made[-2:])])
    assert "prunable" not in listed
