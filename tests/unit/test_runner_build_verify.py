"""RUNNER_BUILD_VERIFY: 파서·판정·리뷰 입력 연결·H7 문구를 고정한다(실제 빌드는 실행하지 않는다)."""
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "scripts/pipeline-runner.sh").read_text()
HELPER = ROOT / "scripts/runner_build_verify.py"
spec = importlib.util.spec_from_file_location("runner_build_verify", HELPER)
rbv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rbv)


def kinds(text, wt="."):
    return [(k, e, c, env, a) for k, e, c, env, a in rbv.parse(text, wt)]


def test_no_line_no_output():
    assert rbv.parse("## 검증 명령\n`npm run build`\n") == []


def test_allowed_and_expect_exit():
    r = kinds("RUNNER_BUILD_VERIFY: npm run check:bundle expect_exit=1\nRUNNER_BUILD_VERIFY: npx next build\n")
    assert r[0] == ("command", 1, ".", "", ["npm", "run", "check:bundle"])
    assert r[1][0] == "command" and r[1][1] == 0 and r[1][4] == ["npx", "next", "build"]


def test_next_dist_dir_and_cwd(tmp_path):
    (tmp_path / "frontend").mkdir()
    t = ("RUNNER_BUILD_VERIFY_CWD: frontend\n"
         "RUNNER_BUILD_VERIFY: NEXT_DIST_DIR=.next.blue.staging npm run build\n")
    assert kinds(t, str(tmp_path)) == [("command", 0, "frontend", "NEXT_DIST_DIR=.next.blue.staging", ["npm", "run", "build"])]


@pytest.mark.parametrize("cwd", ["../x", "/etc", "missing", "a/../../b"])
def test_bad_cwd_is_not_run(tmp_path, cwd):
    r = kinds(f"RUNNER_BUILD_VERIFY_CWD: {cwd}\nRUNNER_BUILD_VERIFY: npm run build\n", str(tmp_path))
    assert r[0][0] == "skipped_bad_cwd"


@pytest.mark.parametrize("cmd", ["npm run build; rm -rf /", "npm run build && id", "npm run build | cat",
                                 "npm run build > x", "npm run build $(id)", "npm run build `id`",
                                 "NEXT_DIST_DIR=/tmp/x npm run build", "NEXT_DIST_DIR=.next;x npm run build",
                                 "FOO=1 npm run build", "npm start", "npm run deploy", "docker build .",
                                 "npm run buildx"])
def test_rejected(cmd):
    r = rbv.parse(f"RUNNER_BUILD_VERIFY: {cmd}\n")
    assert [x[0] for x in r] == ["skipped_unsafe"]


def test_limit_four():
    t = "".join(f"RUNNER_BUILD_VERIFY: npm run lint expect_exit={i}\n" for i in range(6))
    assert len([x for x in rbv.parse(t) if x[0] == "command"]) == 4


@pytest.mark.parametrize(("rc", "expect", "v"), [(0, 0, "pass"), (1, 1, "pass"), (1, 0, "fail"),
                                                  (0, 1, "fail"), (127, 0, "unavailable"), (127, 127, "unavailable")])
def test_classify(rc, expect, v):
    assert rbv.classify(rc, expect) == v


def _func():
    return re.search(r"^pre_review_build_verify\(\) \{\n.*?^\}", SRC, re.M | re.S).group()


def _run(tmp_path, inst, fake_cmd_dir):
    stub = '''
record_runner_event() { echo "EVENT:$2" >&2; }
sql_escape() { printf "'%s'" "$1"; }
db_update() { :; }
_fail_job() { echo "FAIL:$3" >&2; }
'''
    body = _func() + stub + 'SELF_VERIFY_PREFIX=""\npre_review_build_verify j s "$WT" "$INST"; rc=$?\nprintf "%s" "$SELF_VERIFY_PREFIX"\nexit $rc\n'
    return subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{fake_cmd_dir}:{os.environ['PATH']}", "WT": str(tmp_path), "INST": inst,
             "RUNNER_BUILD_VERIFY_PY": str(HELPER), "RUNNER_HEAVY_DIR": str(tmp_path / "lane")})


def _fake(tmp_path, rc):
    d = tmp_path / "bin"
    d.mkdir()
    for n in ("npm", "npx"):
        f = d / n
        f.write_text(f"#!/bin/bash\necho fake-$@ dist=$NEXT_DIST_DIR\nexit {rc}\n")
        f.chmod(0o755)
    return d


def test_header_reaches_review_prefix_and_expect_exit(tmp_path):
    r = _run(tmp_path, "RUNNER_BUILD_VERIFY: NEXT_DIST_DIR=.next.s npm run check:bundle expect_exit=1\n", _fake(tmp_path, 1))
    assert r.returncode == 0, r.stderr
    assert "[Runner build-verify] NEXT_DIST_DIR=.next.s npm run check:bundle rc=1 expect=1 verdict=pass" in r.stdout
    assert "dist=.next.s" in r.stdout
    assert "EVENT:build_verify_pass" in r.stderr


def test_fail_fails_job(tmp_path):
    r = _run(tmp_path, "RUNNER_BUILD_VERIFY: npm run build\n", _fake(tmp_path, 1))
    assert r.returncode == 1
    assert "verdict=fail" in r.stdout and "FAIL:self_verify_failed" in r.stderr
    assert "EVENT:build_verify_fail" in r.stderr


def test_unavailable_does_not_fail(tmp_path):
    r = _run(tmp_path, "RUNNER_BUILD_VERIFY: npm run build\n", _fake(tmp_path, 127))
    assert r.returncode == 0 and "rc=127 expect=0 verdict=unavailable" in r.stdout
    assert "EVENT:build_verify_unavailable" in r.stderr


def test_no_line_is_noop(tmp_path):
    r = _run(tmp_path, "`npm run build`\n", _fake(tmp_path, 1))
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_wired_into_review_input():
    call = SRC.index('pre_review_build_verify "$job_id"')
    assert SRC.index('pre_review_self_verify "$job_id"') < call
    assert 'review_diff+=$\'\\n\\n\'"${SELF_VERIFY_PREFIX}"' in SRC[call:call + 4000]


def test_h7_rule_and_reviewer_prompt():
    assert "RUNNER_BUILD_VERIFY 줄이 있으면 러너가 리뷰 전에 실행합니다" in SRC
    from app.services.code_reviewer import _REVIEW_SYSTEM_PROMPT as p
    assert "H7" in p and "[Runner build-verify]" in p and "러너 빌드 검증 미지정" in p


def test_missing_helper_is_surfaced_to_reviewer():
    """헬퍼 파일이 없으면 이벤트만 남기고 조용히 통과하지 않고, 리뷰 입력에도 미실행 사실을 남긴다."""
    runner = (Path(__file__).resolve().parents[2] / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
    body = runner[runner.index("pre_review_build_verify() {"):]
    body = body[: body.index("local secs lock_wait")]
    assert "build_verify_unavailable" in body
    assert "SELF_VERIFY_PREFIX" in body and "runner_build_verify.py 없음" in body
