#!/usr/bin/env python3
"""중복 재적용 패치 탐지기 (R-DEDUP).

같은 수정을 두 번 얹으면 대개 조용히 지나간다. 중복 상수 정의는 무해하고
중복 함수 정의는 나중 것이 이기기 때문이다. 그래서 알아차리지 못한다.

2026-09-15 는 무해하지 않았다. `scripts/sync_external_deploy_ledger.py` 의
collect() 블록이 통째로 한 번 더 얹히면서
`INSERT INTO deploy_runs(... current_slot, candidate_slot, current_slot ...)`
가 되었고, PostgreSQL 이 'specified more than once' 로 거절해 외부 배포 원장
동기화가 HEAD 에서 깨진 채 커밋됐다. 되돌린 커밋이 a9603307 이다.
그날 같은 수정이 세 번 들어갔고, 신호는 "같은 제목의 커밋이 연달아" 였다.

ruff F811 이 잡았어야 하지 않느냐 — 이 호스트에 ruff 가 없다.
pre-commit 의 ruff 단계는 `command -v ruff` 로 조용히 건너뛰고 있었고,
CLAUDE.md 는 그 단계가 돈다고 적어두고 있었다. 그래서 이 검사는
표준 라이브러리만 쓴다. 설치 상태와 무관하게 항상 돈다.

검사 5종
  py-dup-def     동일 스코프 함수/클래스 중복 정의
  py-dup-assign  동일 스코프에서 내용까지 같은 대입문 반복
  sh-dup-func    셸 함수 중복 정의
  sh-dup-assign  동일한 설정 대입 줄 반복
  sql-dup-col    INSERT 컬럼 목록 / UPDATE SET 대상 중복  ← 실제로 깨진 지점
  dup-block      의미 있는 연속 N줄이 한 파일 안에서 그대로 반복

기준선(baseline)을 둔다. HEAD 에 이미 있는 중복은 보고하지 않는다.
이번 커밋이 새로 만든 중복만 막는다 — 그러지 않으면 무관한 커밋이 전부
막히고, 막히는 게이트는 곧 우회된다(문서 표류 검사가 경고로 내려간 이유).

사용법
    dup_guard.py                      # 스테이지된 파일, 기준선 HEAD
    dup_guard.py --paths a.py b.sh    # 워킹트리 파일
    dup_guard.py --rev <sha>          # 해당 커밋 상태로 검사(기준선 <sha>^)
    dup_guard.py --warn               # 종료코드 0 고정(보고만)

종료코드: 0 = 새 중복 없음 / 1 = 새 중복 발견 / 2 = 실행 불가
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import defaultdict

# 연속 몇 줄이 같아야 "블록을 통째로 다시 얹었다" 로 볼 것인가.
#
# 최근 30커밋으로 캘리브레이션했다. 8줄이면 실제 사고 2건(93daf434,
# b5a03b9e)을 잡지만 오탐이 2건 붙는다 — goals.py 의 엔드포인트 두 개가
# 공유하는 준비 코드 9줄, code_reviewer.py 의 INSERT 인자 목록 8줄.
# 12줄로 올리면 오탐 0, 사고 2건은 그대로 잡힌다(반복 구간이 36줄과 21줄).
# 이보다 짧은 재적용은 아래 정밀 검사(py/sh/sql)가 맡는다.
BLOCK_MIN_LINES = 12

PY_EXT = (".py",)
SH_EXT = (".sh", ".bash")
SQL_SCAN_EXT = PY_EXT + SH_EXT + (".sql",)
BLOCK_SCAN_EXT = PY_EXT + SH_EXT + (".sql", ".ts", ".tsx", ".js", ".jsx")

# 데코레이터에 이 이름들이 있으면 같은 이름 재정의가 정상이다.
# @property/@x.setter, @overload, @singledispatch 의 @f.register 등.
_LEGIT_REDEF = ("setter", "getter", "deleter", "overload", "register")

SH_FUNC_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")
# 설정 대입만 본다: FOO="${FOO:-3}" 꼴. 일반 변수 재대입(total=0 → total=1)은
# 셸에서 정상이므로 건드리지 않는다.
SH_CONF_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=\"?\$\{[A-Za-z0-9_]+:?[-=][^}]*\}\"?\s*$")

# 컬럼 목록 안에 괄호가 들어가는 일은 없으므로 [^()]* 로 끊는다.
# 중첩 반복을 쓰지 않는다 — 백트래킹 폭발 금지(R-BG 3).
SQL_INSERT_RE = re.compile(r"INSERT\s+INTO\s+([A-Za-z_][\w.]*)\s*\(([^()]*)\)", re.IGNORECASE)
SQL_SET_RE = re.compile(r"(?:DO\s+UPDATE\s+SET|UPDATE\s+[\w.\"']+\s+SET)", re.IGNORECASE)
SQL_SET_END_RE = re.compile(r"\bWHERE\b|\bRETURNING\b|;|\"\"\"|'''", re.IGNORECASE)
IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")
SET_TARGET_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=[^=]")


class Finding:
    """한 건의 중복. key 는 기준선 대조용이라 줄 번호를 넣지 않는다."""

    def __init__(self, kind: str, path: str, ident: str, detail: str, line: int):
        self.kind = kind
        self.path = path
        self.ident = ident
        self.detail = detail
        self.line = line

    @property
    def key(self):
        return (self.kind, self.path, self.ident)

    def __str__(self):
        return "%s:%d  [%s] %s" % (self.path, self.line, self.kind, self.detail)


# ── 소스 획득 ────────────────────────────────────────────────────────────


def _git(args):
    return subprocess.run(["git"] + args, capture_output=True, text=True)


def read_blob(rev: str, path: str):
    """rev 가 ':' 면 인덱스(스테이지된 내용), 아니면 해당 커밋의 내용."""
    out = _git(["show", "%s:%s" % (rev, path)])
    return out.stdout if out.returncode == 0 else None


def read_worktree(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


# ── 검사 ─────────────────────────────────────────────────────────────────


def _decorated_legit(node) -> bool:
    for dec in getattr(node, "decorator_list", []):
        src = ast.dump(dec)
        if any(word in src for word in _LEGIT_REDEF):
            return True
    return False


def _scan_py_scope(body, prefix: str, path: str, findings: list) -> None:
    defs = defaultdict(list)
    assigns = defaultdict(list)
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, ast.ClassDef):
                _scan_py_scope(node.body, prefix + node.name + ".", path, findings)
            if _decorated_legit(node):
                continue
            defs[node.name].append(node.lineno)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            try:
                src = ast.unparse(node)
            except Exception:  # noqa: BLE001 - 3.8 이하 등 unparse 불가 환경
                continue
            assigns[(target.id, src)].append(node.lineno)

    for name, lines in sorted(defs.items()):
        if len(lines) > 1:
            findings.append(Finding(
                "py-dup-def", path, prefix + name,
                "%s%s 중복 정의 %d회 (줄 %s)" % (prefix, name, len(lines),
                                              ", ".join(str(n) for n in lines)),
                lines[-1]))
    for (name, _src), lines in sorted(assigns.items()):
        if len(lines) > 1:
            findings.append(Finding(
                "py-dup-assign", path, prefix + name,
                "%s%s 에 똑같은 대입이 %d회 (줄 %s)" % (prefix, name, len(lines),
                                                   ", ".join(str(n) for n in lines)),
                lines[-1]))


def check_python(path: str, text: str) -> list:
    findings = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # 구문 오류는 pre-commit 앞 단계가 이미 차단한다. 여기서는 침묵.
        return findings
    _scan_py_scope(tree.body, "", path, findings)
    return findings


def check_shell(path: str, text: str) -> list:
    findings = []
    funcs = defaultdict(list)
    confs = defaultdict(list)
    for idx, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        m = SH_FUNC_RE.match(raw)
        if m:
            funcs[m.group(1)].append(idx)
            continue
        m = SH_CONF_RE.match(raw)
        if m:
            confs[(m.group(1), raw.strip())].append(idx)
    for name, lines in sorted(funcs.items()):
        if len(lines) > 1:
            findings.append(Finding(
                "sh-dup-func", path, name,
                "%s() 중복 정의 %d회 (줄 %s)" % (name, len(lines),
                                             ", ".join(str(n) for n in lines)),
                lines[-1]))
    for (name, _line), lines in sorted(confs.items()):
        if len(lines) > 1:
            findings.append(Finding(
                "sh-dup-assign", path, name,
                "%s 설정이 똑같이 %d회 (줄 %s)" % (name, len(lines),
                                              ", ".join(str(n) for n in lines)),
                lines[-1]))
    return findings


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check_sql(path: str, text: str) -> list:
    """INSERT 컬럼 목록과 SET 대상의 중복. 여기가 실제로 운영을 깨뜨린 지점이다."""
    findings = []

    for m in SQL_INSERT_RE.finditer(text):
        table, cols = m.group(1), m.group(2)
        seen = defaultdict(int)
        for piece in cols.split(","):
            name = piece.strip().strip('"').strip("`")
            if IDENT_RE.match(name):
                seen[name.lower()] += 1
        dups = sorted(n for n, c in seen.items() if c > 1)
        for name in dups:
            findings.append(Finding(
                "sql-dup-col", path, "%s.%s" % (table, name),
                "INSERT INTO %s 컬럼 '%s' 이 %d번 나열됨 — PostgreSQL 이 "
                "'specified more than once' 로 거절한다" % (table, name, seen[name]),
                _line_of(text, m.start())))

    for m in SQL_SET_RE.finditer(text):
        tail = text[m.end():m.end() + 2000]
        end = SQL_SET_END_RE.search(tail)
        body = tail[:end.start()] if end else tail
        seen = defaultdict(int)
        for piece in body.split(","):
            t = SET_TARGET_RE.match(piece)
            if t:
                seen[t.group(1).lower()] += 1
        for name in sorted(n for n, c in seen.items() if c > 1):
            findings.append(Finding(
                "sql-dup-col", path, "SET." + name,
                "UPDATE SET 에서 '%s' 에 %d번 대입 — 같은 이유로 거절된다"
                % (name, seen[name]),
                _line_of(text, m.start())))
    return findings


def check_dup_block(path: str, text: str) -> list:
    """의미 있는 연속 N줄이 파일 안에서 그대로 반복되는가.

    개별 검사가 놓치는 '블록을 통째로 다시 얹은' 경우를 잡는 그물이다.
    빈 줄과 아주 짧은 줄은 건너뛰고, 그래서 사이에 빈 줄이 섞여도 잡힌다.
    """
    sig = [(i, ln.strip()) for i, ln in enumerate(text.splitlines(), 1)
           if len(ln.strip()) >= 4]
    if len(sig) < BLOCK_MIN_LINES * 2:
        return []

    seen = {}
    regions = []  # [첫등장 sig 인덱스, 반복 sig 인덱스, 길이(줄)]
    for i in range(len(sig) - BLOCK_MIN_LINES + 1):
        window = tuple(t for _, t in sig[i:i + BLOCK_MIN_LINES])
        if len(set(window)) < 3:  # 같은 줄만 반복되는 표·데이터는 제외
            continue
        if window not in seen:
            seen[window] = i
            continue
        first = seen[window]
        # 창이 한 칸씩 밀리며 같은 구간을 다시 잡는다. 이어지는 창은
        # 새 항목이 아니라 앞 구간의 연장이다 — 한 건으로 합친다.
        if regions and regions[-1][0] == first - 1 and regions[-1][1] == i - 1:
            regions[-1][0] = first
            regions[-1][1] = i
            regions[-1][2] += 1
        else:
            regions.append([first, i, BLOCK_MIN_LINES])

    findings = []
    for first, dup, length in regions:
        start_first = sig[first - (length - BLOCK_MIN_LINES)][0]
        start_dup = sig[dup - (length - BLOCK_MIN_LINES)][0]
        head = sig[dup - (length - BLOCK_MIN_LINES)][1]
        findings.append(Finding(
            "dup-block", path, "%x" % (hash(head) & 0xFFFFFFFF),
            "%d줄 블록이 그대로 반복됨 (줄 %d 와 줄 %d) — 첫 줄: %s"
            % (length, start_first, start_dup, head[:60]),
            start_dup))
    return findings


# 검사에서 빼려면 파일 어딘가에 이 표시를 남긴다. 이유를 옆에 적어야
# 의미가 있다 — 지금 쓰이는 곳은 이 검사의 테스트 픽스처 하나뿐이다.
# (깨진 SQL 을 일부러 담고 있으므로 검사 대상이 되면 자기 자신을 막는다.)
IGNORE_MARKER = "dup-guard: ignore-file"


def check_text(path: str, text: str) -> list:
    if IGNORE_MARKER in text:
        return []
    findings = []
    if path.endswith(PY_EXT):
        findings += check_python(path, text)
    if path.endswith(SH_EXT):
        findings += check_shell(path, text)
    if path.endswith(SQL_SCAN_EXT):
        findings += check_sql(path, text)
    if path.endswith(BLOCK_SCAN_EXT):
        findings += check_dup_block(path, text)
    return findings


# ── 실행 ─────────────────────────────────────────────────────────────────


def target_paths(args) -> list:
    if args.paths:
        return list(args.paths)
    if args.rev:
        out = _git(["show", "--name-only", "--pretty=format:", "--diff-filter=ACM", args.rev])
        return [p for p in out.stdout.split("\n") if p.strip()]
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACM"])
    return [p for p in out.stdout.split("\n") if p.strip()]


def scan(paths, rev=None, baseline=True) -> list:
    """새로 생긴 중복만 돌려준다."""
    new = []
    for path in paths:
        if not path.endswith(BLOCK_SCAN_EXT):
            continue
        if rev:
            text = read_blob(rev, path)
            base_rev = rev + "^"
        else:
            text = read_blob("", path)  # ':path' = 인덱스
            if text is None:
                text = read_worktree(path)
            base_rev = "HEAD"
        if text is None:
            continue
        found = check_text(path, text)
        if not found:
            continue
        base_keys = set()
        if baseline:
            base_text = read_blob(base_rev, path)
            if base_text is not None:
                base_keys = {f.key for f in check_text(path, base_text)}
        new += [f for f in found if f.key not in base_keys]
    return new


def main() -> int:
    ap = argparse.ArgumentParser(description="중복 재적용 패치 탐지 (R-DEDUP)")
    ap.add_argument("--paths", nargs="*", help="검사할 파일(생략 시 스테이지된 파일)")
    ap.add_argument("--rev", help="해당 커밋 상태로 검사하고 <rev>^ 를 기준선으로 쓴다")
    ap.add_argument("--warn", action="store_true", help="보고만 하고 종료코드 0")
    ap.add_argument("--no-baseline", action="store_true", help="기준선 대조 없이 전부 보고")
    args = ap.parse_args()

    if _git(["rev-parse", "--git-dir"]).returncode != 0:
        print("[dup-guard] git 저장소가 아닙니다 — 검사를 실행할 수 없습니다", file=sys.stderr)
        return 2

    paths = target_paths(args)
    if not paths:
        return 0

    findings = scan(paths, rev=args.rev, baseline=not args.no_baseline)
    if not findings:
        return 0

    print("중복 %d건" % len(findings))
    for f in findings:
        print("  " + str(f))
    return 0 if args.warn else 1


if __name__ == "__main__":
    sys.exit(main())
