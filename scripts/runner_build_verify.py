#!/usr/bin/env python3
"""RUNNER_BUILD_VERIFY 줄 파서·판정기 (pipeline-runner.sh 가 호출).

지시서 본문은 데이터다. 셸에 넘기지 않고 argv/env 로만 전달한다.

    RUNNER_BUILD_VERIFY: [NEXT_DIST_DIR=.next.x] <허용 접두 명령> [expect_exit=<정수>]
    RUNNER_BUILD_VERIFY_CWD: <워크트리 하위 상대경로>

CLI:
    extract <worktree>   stdin=지시서 → 줄마다 NUL 없이 TSV: kind, expect, cwd, env, argv(JSON)
    classify <rc> <expect>  → pass | fail | unavailable
"""
import json
import os
import re
import shlex
import sys

MAX_COMMANDS = 4
ALLOWED_PREFIXES = (
    ("npm", "run", "build"),
    ("npm", "run", "check:bundle"),
    ("npm", "run", "check:bundle:report"),
    ("npx", "next", "build"),
    ("npm", "run", "lint"),
)
_UNSAFE = set(";&|><$`\\")
_LINE_RE = re.compile(r"^\s*RUNNER_BUILD_VERIFY:\s*(.+?)\s*$")
_CWD_RE = re.compile(r"^\s*RUNNER_BUILD_VERIFY_CWD:\s*(.+?)\s*$")
_ENV_RE = re.compile(r"^NEXT_DIST_DIR=(\.next[.a-z]*)$")
_EXPECT_RE = re.compile(r"^expect_exit=(-?\d+)$")


def _resolve_cwd(worktree, rel):
    """상대경로 검증. 유효하면 (rel, None), 아니면 (None, 사유)."""
    if rel in (None, "", "."):
        return ".", None
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        return None, "bad_cwd"
    root = os.path.realpath(worktree)
    target = os.path.realpath(os.path.join(root, rel))
    if not (target == root or target.startswith(root + os.sep)) or not os.path.isdir(target):
        return None, "bad_cwd"
    return rel, None


def parse(text, worktree="."):
    """[(kind, expect, cwd, env, argv)] 반환. kind: command | skipped_unsafe | skipped_bad_cwd."""
    cwd_rel = None
    for line in text.splitlines():
        m = _CWD_RE.match(line)
        if m:
            cwd_rel = m.group(1)
            break
    cwd, cwd_err = _resolve_cwd(worktree, cwd_rel)
    out, seen, count = [], set(), 0
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        raw = m.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        if any(c in raw for c in _UNSAFE):
            out.append(("skipped_unsafe", 0, ".", "", [raw]))
            continue
        try:
            args = shlex.split(raw)
        except ValueError:
            out.append(("skipped_unsafe", 0, ".", "", [raw]))
            continue
        env = ""
        if args and args[0].startswith("NEXT_DIST_DIR="):
            em = _ENV_RE.match(args[0])
            if not em:
                out.append(("skipped_unsafe", 0, ".", "", [raw]))
                continue
            env = args[0]
            args = args[1:]
        expect = 0
        if args:
            xm = _EXPECT_RE.match(args[-1])
            if xm:
                expect = int(xm.group(1))
                args = args[:-1]
        if not args or not any(tuple(args[: len(p)]) == p for p in ALLOWED_PREFIXES):
            out.append(("skipped_unsafe", 0, ".", "", [raw]))
            continue
        if cwd_err:
            out.append(("skipped_bad_cwd", expect, cwd_rel or ".", env, args))
            continue
        if count >= MAX_COMMANDS:
            continue
        count += 1
        out.append(("command", expect, cwd, env, args))
    return out


def classify(rc, expect=0):
    if rc == 127:
        return "unavailable"
    return "pass" if rc == expect else "fail"


def main(argv):
    if len(argv) >= 3 and argv[1] == "extract":
        for kind, expect, cwd, env, args in parse(sys.stdin.read(), argv[2]):
            print("\t".join([kind, str(expect), cwd.replace("\t", " "), env or "-",
                             json.dumps(args, ensure_ascii=False)]))
        return 0
    if len(argv) == 4 and argv[1] == "classify":
        print(classify(int(argv[2]), int(argv[3])), end="")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
