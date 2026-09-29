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
