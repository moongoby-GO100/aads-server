"""deploy.sh 원격 빌드(AADS_REMOTE_BUILD=42) 계약 테스트 — AADS-IMAGE-BUILD-42-C.

deploy.sh 의 실제 함수(build_dependency_image / build_release_image / remote_*)를
잘라내 bash 로 돌리고, docker·curl·ssh 는 PATH 의 스텁 실행 파일로 바꾼다.
docker 스텁은 호출마다 어느 호스트(원격 ``-H ssh://mesh-42`` = remote / 로컬 = local)로
간 호출인지 기록하고, push 가 로컬로 가거나 호스트와 무관하게 성공하지 않도록 한다.

릴리스 이미지 원격 빌드는 AADS_RUNTIME_BASE 로 쓰는 deps 이미지가 42 docker 에
있어야 한다. 스텁은 42 의 deps 보유 여부(STUB_REMOTE_HAS_DEPS)를 따로 제어한다.
"""
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
DEPLOY = (ROOT / "deploy.sh").read_text(encoding="utf-8")
DIGEST = "sha256:" + "a" * 64
BAD_DIGEST = "sha256:" + "b" * 64
SHA = "1234567890abcdef1234567890abcdef12345678"
KEY = "deadbeef" * 8
DEPS_LOCAL = f"aads-server-deps:{KEY[:24]}"

DOCKER_STUB = r'''#!/bin/bash
host=local
if [[ "$1" == "-H" ]]; then host=remote; shift 2; fi
echo "$host $*" >> "$STUB_LOG"
fail=",${STUB_FAIL:-},"
has() { [[ "$fail" == *",$1,"* ]]; }
case "$1" in
  info) has remote_info && exit 1; exit 0 ;;
  build)
    if [[ $host == remote ]]; then
      has remote_build && exit 1
      [[ "$*" == *runtime-deps* ]] && touch "$STUB_STATE/remote_deps"
      exit 0
    fi
    [[ -n "${STUB_LOCAL_BUILD_RC:-}" ]] && exit "$STUB_LOCAL_BUILD_RC"
    touch "$STUB_STATE/local_built"; exit 0 ;;
  push)
    [[ $host == local ]] && exit 9
    has push && exit 1
    has nodigest && { echo "pushed"; exit 0; }
    echo "latest: digest: ${STUB_PUSH_DIGEST} size: 1234"; exit 0 ;;
  pull)
    if [[ $host == remote ]]; then exit 1; fi
    has pull && exit 1
    touch "$STUB_STATE/pulled"; exit 0 ;;
  tag) has tag && exit 1; echo "$*" >> "$STUB_STATE/tagged"; exit 0 ;;
  rmi) exit 0 ;;
  image)
    ref="$3"; fmt="$5"
    if [[ $host == remote ]]; then
      if [[ "$ref" == *aads-server-deps* && ( "${STUB_REMOTE_HAS_DEPS:-0}" == 1 || -e "$STUB_STATE/remote_deps" ) ]]; then
        echo "$STUB_KEY"; exit 0
      fi
      exit 1
    fi
    if [[ "$ref" == *@sha256:* ]]; then
      [[ -e "$STUB_STATE/pulled" ]] || exit 1
      case "$fmt" in
        *RepoDigests*) has digest_mismatch && d="$STUB_BAD_DIGEST" || d="$STUB_PUSH_DIGEST"
                       echo "${ref%@*}@${d}" ;;
        *dependency-key*) has label && echo wrong || echo "$STUB_KEY" ;;
        *revision*) has label && echo wrong || echo "$STUB_SHA" ;;
      esac
      exit 0
    fi
    if [[ "$ref" == aads-server-deps:* ]]; then
      if [[ "${STUB_LOCAL_DEPS:-0}" == 1 || -e "$STUB_STATE/local_built" || -e "$STUB_STATE/tagged" ]]; then
        echo "$STUB_KEY"; exit 0
      fi
      exit 1
    fi
    exit 1 ;;
esac
exit 0
'''

CURL_STUB = '#!/bin/bash\necho "curl $*" >> "$STUB_LOG"\n[[ ",${STUB_FAIL:-}," == *,registry,* ]] && exit 22\nexit 0\n'
SSH_STUB = '#!/bin/bash\necho "ssh $*" >> "$STUB_LOG"\n[[ ",${STUB_FAIL:-}," == *,ssh,* ]] && exit 255\nexit 0\n'


def _slice(start_marker, end_marker):
    start = DEPLOY.index(start_marker)
    return DEPLOY[start:DEPLOY.index(end_marker, start)]


HELPERS = _slice("REMOTE_BUILD_REGISTRY=", "release_dependency_image() {")
BUILD_RELEASE = _slice("build_release_image() {", "\nif [[ -x \"${COMPOSE_DIR}/scripts/verify-bluegreen")
SQL_ESCAPE = _slice("sql_escape() {", "deploy_db_exec() {")

PREAMBLE = textwrap.dedent(
    """
    set -Eeuo pipefail
    export PATH="$STUB_BIN:$PATH"
    AADS_RELEASE_SHA="%(sha)s"; AADS_DOCKER_TARGET=runtime
    AADS_IMAGE_PROFILE=full; AADS_INSTALL_PLAYWRIGHT=true
    DEPLOY_RUN_ID=77; RELEASE_CONTEXT_DIR=/tmp/aads-test-ctx; COMPOSE_DIR=/tmp
    audit_control() { echo "$*" >> "$STUB_STATE/audit"; }
    deploy_db_exec() { echo "$1" >> "$STUB_STATE/sql"; }
    require_build_disk_free() { echo "local DISKCHECK" >> "$STUB_LOG"; }
    require_dependency_lock_freshness() { :; }
    report_docker_retention_status() { :; }
    cleanup_release_context() { :; }
    require_release_image_within_limit() { :; }
    prepare_release_context() { :; }
    release_dependency_key() { echo "%(key)s"; }
    release_dependency_image() { echo "aads-server-deps:%(key24)s"; }
    """
) % {"sha": SHA, "key": KEY, "key24": KEY[:24]}


def run(kind, tmp_path, fail="", switch=None, flag_file=None, **extra):
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    bin_dir.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    for name, body in (("docker", DOCKER_STUB), ("curl", CURL_STUB), ("ssh", SSH_STUB)):
        f = bin_dir / name
        f.write_text(body)
        f.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "STUB_BIN": str(bin_dir), "STUB_STATE": str(state), "STUB_LOG": str(log),
        "STUB_FAIL": fail, "STUB_PUSH_DIGEST": DIGEST, "STUB_BAD_DIGEST": BAD_DIGEST,
        "STUB_KEY": KEY, "STUB_SHA": SHA,
        "AADS_REMOTE_BUILD_FLAG_FILE": str(flag_file or tmp_path / "no-such-flag"),
    }
    if switch is not None:
        env["AADS_REMOTE_BUILD"] = switch
    if kind == "deps":  # deps 워밍업은 로컬에 이미지가 없을 때만 빌드한다
        extra.pop("STUB_LOCAL_DEPS", None)
    env.update({k: str(v) for k, v in extra.items()})
    fn = "build_release_image" if kind == "release" else "build_dependency_image"
    script = "\n".join([PREAMBLE, SQL_ESCAPE, HELPERS, BUILD_RELEASE, fn])
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    lines = log.read_text().splitlines()
    sql = (state / "sql").read_text() if (state / "sql").exists() else ""
    return proc, lines, sql


def local_builds(lines):
    return [l for l in lines if re.match(r"local build ", l)]


def remote_lines(lines, verb):
    return [l for l in lines if l.startswith(f"remote {verb}")]


@pytest.mark.parametrize("kind", ["release", "deps"])
@pytest.mark.parametrize("mode", ["unset", "off", "banana", "empty-file", "flag-off"])
def test_switch_off_makes_no_remote_calls(tmp_path, kind, mode):
    flag = tmp_path / "flag"
    kwargs = {}
    if mode == "off":
        kwargs["switch"] = "off"
    elif mode == "banana":
        flag.write_text("banana\n")
        kwargs["flag_file"] = flag
    elif mode == "empty-file":
        flag.write_text("")
        kwargs["flag_file"] = flag
    elif mode == "flag-off":
        flag.write_text("off\n")
        kwargs["flag_file"] = flag
    proc, lines, sql = run(kind, tmp_path, STUB_LOCAL_DEPS=1, **kwargs)
    assert proc.returncode == 0, proc.stderr
    assert not [l for l in lines if l.startswith("remote ")]
    assert not [l for l in lines if l.startswith(("ssh", "curl"))]
    assert not [l for l in lines if " push " in l or " pull " in l]
    assert len(local_builds(lines)) == 1
    assert sql == ""  # 스위치 off 는 deploy_runs 에 아무것도 쓰지 않는다


def test_switch_off_local_build_arguments_are_unchanged(tmp_path):
    proc, lines, _ = run("release", tmp_path, STUB_LOCAL_DEPS=1)
    assert proc.returncode == 0, proc.stderr
    build = local_builds(lines)[0]
    assert "--target runtime" in build
    assert f"--build-arg AADS_RUNTIME_BASE={DEPS_LOCAL}" in build
    assert "--build-arg AADS_IMAGE_PROFILE=full" in build
    assert "--build-arg INSTALL_PLAYWRIGHT=true" in build
    assert "--build-arg BUILDKIT_INLINE_CACHE=1" in build
    assert f"--label org.opencontainers.image.revision={SHA}" in build
    assert f"--tag aads-server:{SHA}" in build
    assert "localhost:5000" not in build


def test_flag_file_with_42_enables_remote(tmp_path):
    flag = tmp_path / "flag"
    flag.write_text("42\n")
    proc, lines, _ = run("release", tmp_path, flag_file=flag, STUB_LOCAL_DEPS=1)
    assert proc.returncode == 0, proc.stderr
    assert remote_lines(lines, "push")


def test_env_switch_beats_flag_file(tmp_path):
    flag = tmp_path / "flag"
    flag.write_text("42\n")
    proc, lines, _ = run("release", tmp_path, switch="off", flag_file=flag, STUB_LOCAL_DEPS=1)
    assert proc.returncode == 0, proc.stderr
    assert not [l for l in lines if l.startswith("remote ")]
    assert len(local_builds(lines)) == 1


@pytest.mark.parametrize("has_deps", [0, 1])
def test_release_remote_success(tmp_path, has_deps):
    proc, lines, sql = run("release", tmp_path, switch="42", STUB_LOCAL_DEPS=1,
                           STUB_REMOTE_HAS_DEPS=has_deps)
    assert proc.returncode == 0, proc.stderr
    assert local_builds(lines) == []
    # push 는 원격 호스트, pull·tag 는 로컬 호스트
    pushes = [l for l in lines if " push " in l]
    assert pushes == [f"remote push localhost:5000/aads-server:{SHA}"]
    assert f"local pull localhost:5000/aads-server@{DIGEST}" in lines
    assert f"local tag localhost:5000/aads-server@{DIGEST} aads-server:{SHA}" in lines
    assert not [l for l in lines if l.startswith("local push")]
    assert not [l for l in lines if l.startswith("remote pull localhost:5000/aads-server@")]
    # 원격 release 빌드는 42 에 있는 deps 이미지를 base 로 쓴다
    rbuilds = remote_lines(lines, "build")
    release_build = [l for l in rbuilds if "--target runtime " in l][0]
    assert f"AADS_RUNTIME_BASE=localhost:5000/aads-server-deps:{KEY[:24]}" in release_build
    assert f"--label org.opencontainers.image.revision={SHA}" in release_build
    deps_builds = [l for l in rbuilds if "runtime-deps" in l]
    assert len(deps_builds) == (0 if has_deps else 1)  # 42 에 없으면 deps 를 먼저 빌드
    assert "'build_path', 'remote42'" in sql
    assert f"'remote_digest', '{DIGEST}'" in sql
    assert "UPDATE deploy_runs" in sql and "request_payload" in sql


def test_deps_remote_success(tmp_path):
    proc, lines, sql = run("deps", tmp_path, switch="42")
    assert proc.returncode == 0, proc.stderr
    assert local_builds(lines) == []
    assert [l for l in lines if " push " in l] == [f"remote push localhost:5000/{DEPS_LOCAL}"]
    assert f"local tag localhost:5000/aads-server-deps@{DIGEST} {DEPS_LOCAL}" in lines
    assert f"--label io.aads.dependency-key={KEY}" in remote_lines(lines, "build")[0]
    assert "'build_path', 'remote42'" in sql


STAGES = [
    ("registry", "registry"),
    ("remote_info", "remote_info"),
    ("ssh", "remote_info"),
    ("remote_build", "remote_build"),
    ("push", "push"),
    ("nodigest", "push_digest"),
    ("pull", "pull"),
    ("digest_mismatch", "digest_mismatch"),
    ("tag", "tag"),
    ("label", "label"),  # deps: io.aads.dependency-key 불일치 / release: revision 라벨 불일치
]


@pytest.mark.parametrize("kind", ["release", "deps"])
@pytest.mark.parametrize("fail,stage", STAGES)
def test_each_remote_step_failure_falls_back_to_one_local_build(tmp_path, kind, fail, stage):
    proc, lines, sql = run(kind, tmp_path, fail=fail, switch="42", STUB_LOCAL_DEPS=1,
                           STUB_REMOTE_HAS_DEPS=1)
    assert proc.returncode == 0, proc.stderr
    builds = local_builds(lines)
    assert len(builds) == 1
    # 폴백 직전에 디스크 가드가 실행된다
    assert lines.index("local DISKCHECK") < lines.index(builds[0])
    assert "'build_path', 'local'" in sql
    m = re.search(r"'fallback_reason', '([^']*)'", sql)
    assert m and m.group(1).startswith(f"{stage}: "), sql
    assert "'build_path', 'remote42'" not in sql
    # 로컬 폴백이 만든 이미지는 기존 이름 규칙의 로컬 빌드다
    if kind == "release":
        assert f"--tag aads-server:{SHA}" in builds[0]
    # 실패한 원격 결과는 기존 이름으로 tag 되지 않는다
    if fail != "tag":
        assert not [l for l in lines if l.startswith("local tag ")] or fail in ("tag",)


@pytest.mark.parametrize("kind", ["release", "deps"])
def test_local_fallback_failure_propagates_exit_code(tmp_path, kind):
    proc, lines, _ = run(kind, tmp_path, fail="push", switch="42", STUB_LOCAL_DEPS=1,
                         STUB_LOCAL_BUILD_RC=3)
    assert proc.returncode == 3
    assert len(local_builds(lines)) == 1


def test_remote_deps_base_failure_falls_back(tmp_path):
    # 42 에 deps 가 없고 42 에서의 deps 빌드도 실패하면 release 원격 빌드는 시도하지 않는다.
    proc, lines, sql = run("release", tmp_path, fail="remote_build", switch="42", STUB_LOCAL_DEPS=1)
    assert proc.returncode == 0, proc.stderr
    assert len(local_builds(lines)) == 1
    assert "'build_path', 'local'" in sql
    assert "'fallback_reason', 'deps_base: " in sql
    assert not remote_lines(lines, "push")


def test_stub_would_catch_push_on_wrong_host(tmp_path):
    # 스텁 자체 점검: 로컬 push 는 실패(9)한다 — 호스트 무관 성공 스텁이 아니다.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "docker").chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(tmp_path / "l"),
           "STUB_STATE": str(tmp_path), "STUB_PUSH_DIGEST": DIGEST}
    assert subprocess.run(["docker", "push", "x"], env=env).returncode == 9
    assert subprocess.run(["docker", "-H", "ssh://mesh-42", "push", "x"], env=env).returncode == 0


def test_build_guards_still_see_remote_docker_build_processes():
    # 중복·병행 빌드 차단은 ps 의 실행 파일이 docker 이고 인자에 " build " 가 있는지로 본다.
    # 원격 빌드 형태(docker -H ssh://mesh-42 build ... localhost:5000/ 태그)도 같은 식에 걸려야 한다.
    awk = r'$1 == "docker" && /[ ]build[ ]/ {c++} END{print c+0}'
    ps = "docker docker -H ssh://mesh-42 build --target runtime --tag localhost:5000/aads-server:abc /tmp/ctx\n"
    out = subprocess.run(["awk", awk], input=ps, capture_output=True, text=True).stdout.strip()
    assert out == "1"
    guard = (ROOT / "scripts" / "disk_guard.sh").read_text()
    assert r"/[ ]build[ ]/" in guard


def test_local_and_remote_build_commands_share_the_contract_strings():
    # 슬롯 기동은 --no-build, 이미지는 SHA 당 한 번 — 이 변경이 그 계약 문자열을 건드리지 않는다.
    assert DEPLOY.count("--no-build --no-deps") >= 2
    body = BUILD_RELEASE
    assert body.count('--tag "aads-server:${AADS_RELEASE_SHA}"') == 1
    assert body.count("docker build") == 1
