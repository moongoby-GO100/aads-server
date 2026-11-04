"""commit-msg 훅은 제목 줄만 무의미 검사하고, 러너 커밋 제목은 한 줄·단어 경계로 만든다."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "hooks" / "commit-msg"
SCRIPT = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
_START = SCRIPT.index("        commit_msg=$(printf")
_END = SCRIPT.index("\n        commit_out=", _START)
GENERATOR = SCRIPT[_START:_END]


def run_hook(tmp_path, message):
    path = tmp_path / "msg"
    path.write_text(message, encoding="utf-8")
    return subprocess.run(["bash", str(HOOK), str(path)], cwd=tmp_path,
                          capture_output=True, text=True, check=False)


def generate(instruction, job="runner-e93f7592"):
    result = subprocess.run(
        ["bash", "-c", "instruction=$1; job_id=$2; " + GENERATOR + '\nprintf "%s" "$commit_msg"',
         "test", instruction, job], text=True, capture_output=True, check=True,
    )
    return result.stdout


@pytest.mark.parametrize("subject", ["fix", ".", "wip", "update", "test", "and", "Claude Code"])
def test_meaningless_subject_is_rejected(tmp_path, subject):
    out = run_hook(tmp_path, subject + "\n\n충분히 긴 본문 줄입니다\n")
    assert out.returncode == 1 and "무의미" in out.stdout


def test_comment_lines_before_subject_are_skipped(tmp_path):
    out = run_hook(tmp_path, "# 주석\nfix\n")
    assert out.returncode == 1 and "무의미" in out.stdout


@pytest.mark.parametrize("body_line", ["G", "-", "."])
def test_single_char_body_line_is_not_rejected(tmp_path, body_line):
    out = run_hook(tmp_path, "feat(runner): 정상 제목입니다\n\n" + body_line + "\n본문 두 글자 이상\n")
    assert "무의미" not in out.stdout
    # 제목 검사를 통과해도 pre-commit 서명 검사는 그대로 막는다(게이트 약화 없음).
    assert out.returncode == 1 and "pre-commit hook 서명 없음" in out.stdout


def test_hook_keeps_dedup_and_signature_gates():
    text = HOOK.read_text(encoding="utf-8")
    assert "ALLOW_DUP_COMMIT" in text and "--since='3 days ago'" in text
    assert "HOOK_VERIFIED" in text


def test_runner_subject_uses_title_line():
    msg = generate("GOAL_ID: 4\nMILESTONE_ID: 7\nTITLE: 급여 PDF 생성 오류 수정\nTARGET: /x")
    assert msg == "fix(pipeline-runner): runner-e93f7592 — 급여 PDF 생성 오류 수정"


def test_runner_subject_skips_meta_lines_without_title():
    msg = generate("GOAL_ID: 4\n\nMILESTONE_ID: 7\nALLOW_DUP_JOB AUTO_REWORK_OF: runner-1\nPUSH_ONLY:true\n"
                   "DEPLOY_POLICY:push_only\nTARGET: /x\n급여 PDF 오류를 고친다")
    assert msg == "fix(pipeline-runner): runner-e93f7592 — 급여 PDF 오류를 고친다"


def test_runner_subject_cuts_on_word_boundary_within_80_chars():
    msg = generate("TITLE: " + " ".join(["가나다라마바사"] * 20))
    summary = msg.split(" — ", 1)[1]
    assert len(summary) <= 80 and "\n" not in msg
    assert summary == " ".join(["가나다라마바사"] * 10)
    assert summary.endswith("가나다라마바사")


def test_runner_subject_counts_characters_not_bytes():
    summary = generate("TITLE: " + "한" * 200).split(" — ", 1)[1]
    assert summary == "한" * 80
    summary.encode("utf-8")


def test_runner_subject_never_has_single_char_line():
    msg = generate("GOAL_ID: 4\nMILESTONE_ID: 7\nALLOW_DUP_JOB\nPUSH_ONLY:true\nDEPLOY_POLICY:push_only\n"
                   "TARGET: /root/aads/aads-server GOAL_ID: 4")
    assert "\n" not in msg and "\r" not in msg
