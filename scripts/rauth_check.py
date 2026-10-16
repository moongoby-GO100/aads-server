#!/usr/bin/env python3
"""pre-commit R-AUTH 단계 — 스테이징 diff 의 추가 줄에서 Anthropic API 키 환경변수의 직접 사용을 찾는다.

사용: git diff --cached -- <파일> | python3 scripts/rauth_check.py <파일>
종료코드: 0 위반 없음 · 1 위반(해당 줄을 stdout 에 출력)

tests/ 아래 파일에서는 '부재 검사' 줄만 허용한다 (R-ERRBOOK: 같은 오탐 4회).
  assert ... not in / assert not re.search( / env -u <KEY> / monkeypatch.delenv(
허용되는 것은 키가 그 부재 검사 조각 안에서만 나타나는 줄이다 — 조각을 지운 뒤에도 키가 남으면(setenv·pop·getenv 등) 차단한다.
app/·scripts/ 와 tests/ 의 그 밖의 줄은 예전처럼 전부 차단한다.
"""
from __future__ import annotations

import re
import sys

_KEY = "ANTHROPIC_" + "API_KEY"
_Q = r"""["']"""

# 키가 '부재 검사'로만 쓰인 조각. 줄에서 이 조각을 지운 뒤에도 키가 남으면 사용이다.
_ASSERT_LINE = re.compile(r"^\s*assert\b")
_ABSENCE_FRAGMENTS = (
    (re.compile(_Q + _KEY + _Q + r"\s+not\s+in\b"), True),
    (re.compile(r"\bassert\s+not\s+re\.search\(\s*[rbu]*(" + _Q + r")(?:(?!\1).)*?" + _KEY + r"(?:(?!\1).)*\1"), False),
    (re.compile(r"\benv\s+-u\s+" + _KEY + r"\b"), False),
    (re.compile(r"\bmonkeypatch\.delenv\(\s*" + _Q + _KEY + _Q), False),
)


def is_tests_path(path: str) -> bool:
    return path.startswith("tests/")


def is_absence_check(line: str) -> bool:
    """키가 부재 검사 조각 안에서만 나타나면 True. 그 밖에 키가 한 번이라도 남으면 False."""
    rest = line
    for pattern, assert_only in _ABSENCE_FRAGMENTS:
        if assert_only and not _ASSERT_LINE.search(line):
            continue
        rest = pattern.sub(" ", rest)
    return _KEY not in rest and rest != line


def violations(path: str, diff_text: str) -> list[str]:
    if ".bak" in path:
        return []
    found = []
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:]
        if _KEY not in line:
            continue
        if "FALLBACK" in line or "AUTH_TOKEN" in line or ".bak" in line:
            continue
        if re.search(r"#.*R-AUTH", line):
            continue
        if is_tests_path(path) and is_absence_check(line):
            continue
        found.append(raw)
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: rauth_check.py <staged-path>  (diff on stdin)", file=sys.stderr)
        return 2
    bad = violations(argv[1], sys.stdin.read())
    for line in bad:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
