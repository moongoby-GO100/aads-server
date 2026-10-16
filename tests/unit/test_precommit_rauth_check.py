"""pre-commit R-AUTH 단계: tests/ 의 '키 부재 검사' 줄은 통과, 사용 줄은 계속 차단.

오탐이 4회 반복돼(runner-1d168a6d, 6c4be959, fc122dcd 계열, 4cfa24b2) 규칙 기록만으로는
막히지 않는다고 확인됐다 — R-ERRBOOK 에 따라 코드로 옮긴 판정이다.
키 이름 리터럴은 이 파일에도 쓰지 않는다(자기 커밋이 막힌다).
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("rauth_check", _REPO / "scripts" / "rauth_check.py")
assert _spec and _spec.loader
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

KEY = "ANTHROPIC_" + "API_KEY"


def _diff(*lines: str) -> str:
    return "+++ b/x.py\n" + "".join("+" + ln + "\n" for ln in lines)


# ── tests/ 의 부재 검사 줄은 허용 ─────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        'assert "%s" not in AUTOFILL' % KEY,
        '    assert "%s" not in os.environ' % KEY,
        "    assert not re.search(r'%s', src)" % KEY,
        "cmd = 'env -u %s claude -p hi'" % KEY,
        'monkeypatch.delenv("%s", raising=False)' % KEY,
        'assert "%s" not in a and "%s" not in b' % (KEY, KEY),
        "    assert not re.search(r'%s\\b', src)" % KEY,
    ],
)
def test_absence_checks_pass_in_tests(line):
    assert rc.violations("tests/unit/test_x.py", _diff(line)) == []


# ── 사용 줄은 계속 차단 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        'key = os.getenv("%s")' % KEY,
        'key = os.environ["%s"]' % KEY,
        'key = os.environ.get("%s")' % KEY,
        'monkeypatch.setenv("%s", "x")' % KEY,
        'assert os.getenv("%s") not in (None, "")' % KEY,
        'monkeypatch.delenv("%s"); v = os.getenv("%s")' % (KEY, KEY),
        'headers = {"x-api-key": "%s"}' % KEY,
        # 리뷰 지적 1: 다른 키의 delenv 로 위장한 같은 줄의 키 설정
        'monkeypatch.delenv("OTHER"); monkeypatch.setenv("%s", "x")' % KEY,
        'monkeypatch.delenv("OTHER", raising=False); os.environ["%s"] = "x"' % KEY,
        # 리뷰 지적 2: 부재 검사 뒤에 or/쉼표/세미콜론으로 키를 읽거나 바꾸는 코드가 섞인 줄
        'assert "%s" not in source or os.environ.pop("%s")' % (KEY, KEY),
        'assert "%s" not in source, os.environ.setdefault("%s", "x")' % (KEY, KEY),
        "assert not re.search(r'%s', src) or os.environ.pop('%s')" % (KEY, KEY),
        "cmd = 'env -u %s %s=x claude'" % (KEY, KEY),
        'assert "%s" not in a; monkeypatch.setenv("%s", "x")' % (KEY, KEY),
    ],
)
def test_usage_lines_still_blocked_in_tests(line):
    assert rc.violations("tests/integration/test_x.py", _diff(line)) == [("+" + line)]


@pytest.mark.parametrize("path", ["app/core/x.py", "scripts/x.py", "app/tests_helpers/x.py"])
@pytest.mark.parametrize(
    "line",
    [
        'key = os.getenv("%s")' % KEY,
        'assert "%s" not in AUTOFILL' % KEY,
        'monkeypatch.delenv("%s", raising=False)' % KEY,
    ],
)
def test_outside_tests_everything_stays_blocked(path, line):
    assert rc.violations(path, _diff(line)) == ["+" + line]


# ── 기존 예외는 유지 ──────────────────────────────────────────────────


def test_legacy_exemptions_kept():
    lines = [
        "%s_FALLBACK = 1" % KEY,
        "x = '%s' or 'ANTHROPIC_AUTH_TOKEN'" % KEY,
        "y = 1  # %s R-AUTH 허용" % KEY,
    ]
    assert rc.violations("app/x.py", _diff(*lines)) == []
    assert rc.violations("app/x.py.bak", _diff('os.getenv("%s")' % KEY)) == []


def test_removed_and_context_lines_are_ignored():
    diff = "--- a/x.py\n+++ b/x.py\n-os.getenv('%s')\n os.getenv('%s')\n" % (KEY, KEY)
    assert rc.violations("app/x.py", diff) == []


# ── hook 배선: 실제 R-AUTH 블록을 임시 저장소에서 돌린다 ──────────────


def _rauth_block() -> str:
    hook = (_REPO / "scripts" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    start = hook.index("# ── R-AUTH 위반 감지")
    end = hook.index("# ── AUTH 핵심 파일 보호")
    return hook[start:end]


def _run_hook_block(tmp_path: Path, rel: str, content: str) -> subprocess.CompletedProcess:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": __import__("os").environ["PATH"]}
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, env=env, check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    (tmp_path / "scripts").mkdir(exist_ok=True)
    shutil.copy(_REPO / "scripts" / "rauth_check.py", tmp_path / "scripts" / "rauth_check.py")
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git("add", rel)
    script = 'RED=""; NC=""; STAGED="%s"\n%s\necho PASSED\n' % (rel, _rauth_block())
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)


def test_hook_block_passes_absence_check_in_tests(tmp_path):
    r = _run_hook_block(tmp_path, "tests/unit/test_a.py", 'assert "%s" not in AUTOFILL\n' % KEY)
    assert r.returncode == 0 and "PASSED" in r.stdout, r.stdout + r.stderr


def test_hook_block_blocks_getenv_in_tests(tmp_path):
    r = _run_hook_block(tmp_path, "tests/unit/test_a.py", 'k = os.getenv("%s")\n' % KEY)
    assert r.returncode == 1 and "PASSED" not in r.stdout, r.stdout + r.stderr
    assert "R-AUTH" in r.stdout


@pytest.mark.parametrize(
    "line",
    [
        'monkeypatch.delenv("OTHER"); monkeypatch.setenv("%s", "x")' % KEY,
        'assert "%s" not in source or os.environ.pop("%s")' % (KEY, KEY),
    ],
)
def test_hook_block_blocks_mixed_absence_and_usage_in_tests(tmp_path, line):
    r = _run_hook_block(tmp_path, "tests/unit/test_a.py", line + "\n")
    assert r.returncode == 1 and "PASSED" not in r.stdout, r.stdout + r.stderr


def test_hook_block_blocks_getenv_in_app(tmp_path):
    r = _run_hook_block(tmp_path, "app/core/a.py", 'k = os.getenv("%s")\n' % KEY)
    assert r.returncode == 1 and "PASSED" not in r.stdout, r.stdout + r.stderr


def test_hook_block_blocks_absence_check_outside_tests(tmp_path):
    r = _run_hook_block(tmp_path, "scripts/a.py", 'assert "%s" not in X\n' % KEY)
    assert r.returncode == 1, r.stdout + r.stderr


def test_hook_wires_rauth_check_script():
    hook = (_REPO / "scripts" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "scripts/rauth_check.py" in hook
    assert "--no-verify" not in _rauth_block()
