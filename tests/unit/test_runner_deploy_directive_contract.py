"""PUSH_ONLY 및 긴 한글 배포 금지 제약이 빌드·배포로 새지 않는지 고정한다.

2026-10-04 사고(AADS-RUNNER-PUSH-ONLY-ENFORCE): runner-9d5d8d45 지시문에
"PUSH_ONLY. 빌드·배포·운영 migration 금지." 가 있었는데 instruction_forbids_deploy 가
둘 다 놓쳐 00:45 CEST(07:45 KST) push → BLUEGREEN → 07:54 KST DEPLOYED 가 실행됐다.
원인: (1) PUSH_ONLY 구조화 선언 미지원 (2) "배포"~"금지" 간격 12자 제한("·운영 migration " 은 14자).
추가로 지시서 조회 실패/빈 값이 "제약 없음" 으로 해석돼 배포가 허용되는 경로도 막는다.
실제 배포 명령은 실행하지 않는다 — 셸 함수와 deploy_job 게이트 구간을 stub 으로만 돌린다.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash 미설치 환경")


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _run_bash(code: str, *args: str, **env) -> subprocess.CompletedProcess:
    full_env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8", **env)
    return subprocess.run(
        ["bash", "-c", code, "_", *args], capture_output=True, text=True, timeout=30, env=full_env
    )


@pytest.fixture(scope="module")
def gate_fn(tmp_path_factory):
    fn_file = tmp_path_factory.mktemp("gate_fn") / "fn.sh"
    fn_file.write_text(
        "set -eo pipefail\n" + _extract_function(_read_script(), "instruction_forbids_deploy"),
        encoding="utf-8",
    )
    return fn_file


def _decide(fn_file: Path, text: str) -> str:
    proc = _run_bash(
        f'source "{fn_file}"; if instruction_forbids_deploy "$1"; then echo FORBID; else echo ALLOW; fi',
        text,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# ── 사고 원문 ───────────────────────────────────────────────────────────

INCIDENT_TAIL = (
    "READ_ONLY_FILES: app/api/pipeline_runner.py\n"
    "PUSH_ONLY. 빌드·배포·운영 migration 금지. no-verify/force push/전체 compose 금지.\n"
)


def test_incident_instruction_is_forbidden(gate_fn):
    assert _decide(gate_fn, INCIDENT_TAIL) == "FORBID"


def test_incident_sentence_alone_is_forbidden_without_push_only_token(gate_fn):
    """PUSH_ONLY 토큰이 없어도 긴 한글 금지 문장만으로 막혀야 한다(간격 14자)."""
    assert _decide(gate_fn, "빌드·배포·운영 migration 금지.") == "FORBID"


# ── 구조화 선언 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "PUSH_ONLY",
        "push_only",
        "PUSH_ONLY: true",
        "PUSH-ONLY",
        "TITLE: 청산 게이트 (PUSH_ONLY)",
        "TASK_ID: X\nPUSH_ONLY\n본문",
        "- PUSH ONLY",
        "DEPLOY_POLICY: push_only",
        "Deployment-Policy = NO_DEPLOY",
        "release policy: forbidden",
        "DEPLOY: false",
    ],
)
def test_structured_push_only_declarations_are_forbidden(gate_fn, text):
    assert _decide(gate_fn, text) == "FORBID"


@pytest.mark.parametrize(
    "text",
    [
        "PUSH_ONLY: false",
        "push_only = no",
        "DEPLOY_ONLY: true",
        "DEPLOY_POLICY: auto",
        "phase='push_only_by_directive' 를 읽어라",
        "no_push_only_flag 변수명",
    ],
)
def test_explicit_off_and_lookalikes_are_not_forbidden(gate_fn, text):
    assert _decide(gate_fn, text) == "ALLOW"


# ── 긴 한글 금지 문구 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "빌드·배포·운영 migration 금지",
        "배포 및 운영 DB migration 실행 절대 금지",
        "재기동·서비스 전환·릴리스 슬롯 교체 금지",
        "빌드, 배포, 운영 마이그레이션은 하지 마라",
    ],
)
def test_long_korean_prohibitions_are_forbidden(gate_fn, text):
    assert _decide(gate_fn, text) == "FORBID"


# ── 기존 자연어 표현 회귀 보호 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "배포 금지",
        "배포금지",
        "빌드·배포 실행 금지",
        "빌드·배포 금지. push 까지만 수행",
        "배포를 실행하지 마",
        "배포·재기동 절대 금지",
        "재기동 금지",
        "커밋까지만",
        "push 까지만",
        "Do not deploy",
        "no deploy",
        "빌드까지만 하고 NO DEPLOY",
        "TITLE: 청산 게이트 (커밋까지만, 배포·재기동 절대 금지)",
    ],
)
def test_existing_natural_language_forbids_still_forbid(gate_fn, text):
    assert _decide(gate_fn, text) == "FORBID"


@pytest.mark.parametrize(
    "text",
    [
        "TASK_ID: AADS-AAG-001 추출기를 만들고 단위테스트를 통과시켜라.",
        "대시보드 Mermaid 렌더러 도입 — 배포 후 화면 캡처로 검증하라.",
        "배포 후 검증해라",
        "배포 후 로그를 확인하고 설정 값은 변경하지 마라",
        "deployment notes",
        "",
    ],
)
def test_unrelated_normal_instructions_still_deploy(gate_fn, text):
    assert _decide(gate_fn, text) == "ALLOW"


def test_gate_terminates_quickly_on_large_adversarial_input(gate_fn):
    """R-BG: 정규식 백트래킹 폭발 방지 — 큰 입력도 즉시 끝나야 한다."""
    text = ("배포 " * 4000) + ("가" * 20000)
    assert _decide(gate_fn, text) == "ALLOW"


# ── 조회 실패·빈 instruction 은 fail-closed ─────────────────────────────


def _strict_reader(tmp_path: Path) -> Path:
    fn_file = tmp_path / "strict.sh"
    fn_file.write_text(
        "set -eo pipefail\n" + _extract_function(_read_script(), "read_job_instruction_strict"),
        encoding="utf-8",
    )
    return fn_file


def _read_strict(tmp_path: Path, stub: str, job_id: str = "runner-abc123") -> subprocess.CompletedProcess:
    return _run_bash(
        f'source "{_strict_reader(tmp_path)}"\nsleep() {{ :; }}\n{stub}\n'
        f'if out=$(read_job_instruction_strict "{job_id}"); then echo "OK:$out"; else echo FAIL; fi',
        DEPLOY_DIRECTIVE_LOOKUP_ATTEMPTS="3",
    )


def test_strict_reader_returns_instruction(tmp_path):
    proc = _read_strict(tmp_path, "db_exec() { printf '%s' 'TASK_ID: X'; }")
    assert proc.stdout.strip() == "OK:TASK_ID: X"


@pytest.mark.parametrize(
    "stub",
    [
        "db_exec() { return 1; }",
        "db_exec() { printf ''; }",
        "db_exec() { printf '  \\n '; }",
    ],
)
def test_strict_reader_fails_closed_on_failure_or_empty(tmp_path, stub):
    assert _read_strict(tmp_path, stub).stdout.strip() == "FAIL"


def test_strict_reader_rejects_unsafe_job_id_without_querying(tmp_path):
    proc = _read_strict(
        tmp_path,
        "db_exec() { echo QUERIED >&2; printf '%s' 'x'; }",
        job_id="x'; DROP TABLE pipeline_jobs;--",
    )
    assert proc.stdout.strip() == "FAIL"
    assert "QUERIED" not in proc.stderr


def test_strict_reader_recovers_after_transient_failure(tmp_path):
    # 명령치환은 서브셸이라 변수 카운터가 늘지 않는다 — 파일로 센다.
    counter = tmp_path / "n"
    stub = (
        f'db_exec() {{ local n; n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); '
        f'echo $n > "{counter}"; [[ $n -ge 3 ]] || return 1; printf "%s" "PUSH_ONLY"; }}'
    )
    assert _read_strict(tmp_path, stub).stdout.strip() == "OK:PUSH_ONLY"


# ── deploy_job 게이트 구간 stub 실행 (실제 배포 명령 없음) ──────────────


def _gate_segment() -> str:
    script = _read_script()
    start = script.index('    local job_instruction="" _deploy_directive_state=""')
    end = script.index("    # ═══ 무중단 배포 v3.0", start)
    return script[start:end]


def _run_gate(tmp_path: Path, db_exec_stub: str) -> dict:
    """게이트 구간 + 빌드 직전 불변식 구간까지 실행하고, 빌드 stub 에 닿았는지 본다."""
    script = _read_script()
    inv_start = script.index('    local _build_fail=""', script.index("    # ═══ 무중단 배포 v3.0"))
    inv_end = script.index('    case "$project" in', inv_start)
    harness = f"""
set -eo pipefail
{_extract_function(script, "instruction_forbids_deploy")}
{_extract_function(script, "read_job_instruction_strict")}
sleep() {{ :; }}
log() {{ echo "LOG:$*"; }}
sql_escape() {{ printf "'%s'" "$1"; }}
db_update() {{ echo "DB_UPDATE:$*" | tr '\\n' ' '; echo; }}
record_runner_event() {{ echo "EVENT:$4"; }}
post_to_chat() {{ echo "CHAT"; }}
_release_deploy_lock() {{ echo "LOCK_RELEASED"; }}
_notify_ai() {{ echo "NOTIFY_INTERNAL"; }}
promote_next_queued() {{ echo "PROMOTED"; }}
_fail_job() {{ echo "FAIL_JOB:$3"; }}
{db_exec_stub}
run_gate() {{
  local job_id="runner-test01" session_id="s1" project="AADS"
{_gate_segment()}
{script[inv_start:inv_end]}
  echo BUILD_REACHED
}}
rc=0
run_gate || rc=$?
echo "RC=$rc"
"""
    proc = _run_bash(harness, DEPLOY_DIRECTIVE_LOOKUP_ATTEMPTS="2")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    return {
        "out": out,
        "build": "BUILD_REACHED" in out,
        "rc": int(out.rsplit("RC=", 1)[1].strip()),
    }


def test_push_only_job_stops_before_build_and_is_done_not_error(tmp_path):
    r = _run_gate(tmp_path, "db_exec() { printf '%s' \"$(printf 'TASK_ID: X\\nPUSH_ONLY\\n빌드·배포 금지')\"; }")
    assert not r["build"]
    assert r["rc"] == 0
    assert "DEPLOY_SKIPPED_BY_DIRECTIVE" in r["out"]
    assert "phase='push_only_by_directive'" in r["out"]
    assert "deployed_at=NULL" in r["out"]
    assert "FAIL_JOB" not in r["out"]


def test_incident_text_stops_before_build(tmp_path):
    stub = f"db_exec() {{ printf '%s' '{INCIDENT_TAIL}'; }}"
    r = _run_gate(tmp_path, stub)
    assert not r["build"]
    assert "DEPLOY_SKIPPED_BY_DIRECTIVE" in r["out"]


@pytest.mark.parametrize(
    "stub",
    [
        "db_exec() { return 1; }",
        "db_exec() { printf ''; }",
    ],
)
def test_unverifiable_instruction_stops_before_build(tmp_path, stub):
    r = _run_gate(tmp_path, stub)
    assert not r["build"]
    assert r["rc"] == 1
    assert "FAIL_JOB:deploy_directive_unverifiable" in r["out"]
    assert "LOCK_RELEASED" in r["out"]
    assert "PROMOTED" in r["out"]


def test_normal_job_still_reaches_build(tmp_path):
    r = _run_gate(tmp_path, "db_exec() { printf '%s' 'TASK_ID: AADS-AAG-001 추출기를 만들어라.'; }")
    assert r["build"]
    assert r["rc"] == 0
    assert "FAIL_JOB" not in r["out"]
    assert "DEPLOY_SKIPPED_BY_DIRECTIVE" not in r["out"]


def test_invariant_blocks_build_when_gate_state_is_missing(tmp_path):
    script = _read_script()
    inv_start = script.index('    local _build_fail=""', script.index("    # ═══ 무중단 배포 v3.0"))
    inv_end = script.index('    case "$project" in', inv_start)
    harness = f"""
set -eo pipefail
_fail_job() {{ echo "FAIL_JOB:$3"; }}
_release_deploy_lock() {{ :; }}
promote_next_queued() {{ :; }}
check() {{
  local job_id="runner-test01" session_id="s1" project="AADS" _deploy_directive_state="$1"
{script[inv_start:inv_end]}
  echo BUILD_REACHED
}}
for state in "" "allowed:runner-other" "allowed:runner-test01"; do
  rc=0; echo "STATE=[$state]"; check "$state" || rc=$?; echo "RC=$rc"
done
"""
    out = _run_bash(harness).stdout
    blocks = out.split("STATE=")[1:]
    assert "FAIL_JOB:deploy_directive_gate_bypassed" in blocks[0] and "BUILD_REACHED" not in blocks[0]
    assert "FAIL_JOB:deploy_directive_gate_bypassed" in blocks[1] and "BUILD_REACHED" not in blocks[1]
    assert "BUILD_REACHED" in blocks[2] and "FAIL_JOB" not in blocks[2]


# ── 정적 계약 ───────────────────────────────────────────────────────────


def test_every_build_or_deploy_command_comes_after_gate_and_invariant():
    script = _read_script()
    deploy_job = script[script.index("\ndeploy_job() {"):]
    gate = deploy_job.index("if instruction_forbids_deploy")
    state_set = deploy_job.index('_deploy_directive_state="allowed:${job_id}"')
    invariant = deploy_job.index('"$_deploy_directive_state" != "allowed:${job_id}"')
    assert gate < state_set < invariant

    for needle in ('deploy.sh" bluegreen', "aads-dashboard/deploy.sh", "docker compose -f"):
        positions = [i for i in range(len(deploy_job)) if deploy_job.startswith(needle, i)]
        assert positions, needle
        assert min(positions) > invariant, f"{needle} 가 빌드 직전 불변식보다 앞에 있다"


def test_gate_no_longer_swallows_lookup_failure():
    script = _read_script()
    assert "|| job_instruction=\"\"" not in script[script.index("\ndeploy_job() {"):]
    assert "read_job_instruction_strict" in script


def test_both_runner_scripts_carry_the_fix_and_stay_identical():
    for name in SCRIPTS:
        script = _read_script(name)
        assert "read_job_instruction_strict() {" in script
        assert "deploy_directive_unverifiable" in script
        assert "deploy_directive_gate_bypassed" in script
    assert (ROOT / "scripts" / SCRIPTS[0]).read_bytes() == (ROOT / "scripts" / SCRIPTS[1]).read_bytes()


def test_runner_scripts_pass_bash_syntax_check():
    for name in SCRIPTS:
        proc = subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
