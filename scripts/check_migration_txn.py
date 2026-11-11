#!/usr/bin/env python3
"""마이그레이션 내부 BEGIN/COMMIT 및 lock_timeout 누락 방어 게이트 (R-ERRBOOK).

배경:
  오류 사전 deploy.migration_inner_txn_autoverify_reject 2회 재발
  (1회차: 20261106_goal_retry_exhaust_requests.sql, 커밋 845174db,
   2회차: 20261111_canonical_document_approval_delegation.sql)
  마이그레이션 내부 BEGIN;/COMMIT; 및 lock_timeout 누락으로 deploy_runs #6151
  schema_migrations exit=7 발생. CLAUDE.md R-ERRBOOK 3 에 따라 코드 차단으로 강제.

규칙:
  1. 추가(A) 또는 수정(M)된 migrations/*.sql (하위 rollback/ 제외) 검사.
  2. 주석·문자열 밖 최상위 BEGIN; 또는 COMMIT; 문장 차단.
     (DO $$ BEGIN ... END $$ 블록 안의 BEGIN 은 허용)
  3. 첫 DDL 앞에 SET LOCAL lock_timeout 누락 시 경고 (종료코드 0).
  4. 과거 OBYS 업무 DB용 파일은 파일 머리 주석에 '업무 DB' 또는 'target=acct_business'
     표기가 있으면 건너뜀 (apply_release_migrations.sh DEFER 판정 규칙과 일치).
  5. 우회 환경변수는 두지 않는다.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

BLOCK_MESSAGE = (
    "적용기가 트랜잭션으로 감싼다 — 파일 안 BEGIN/COMMIT 제거, "
    "SET LOCAL lock_timeout/statement_timeout 사용"
    "(형식: migrations/20261106_goal_retry_exhaust_requests.sql)"
)

# 최상위 트랜잭션 제어 구문
TXN_STMT_RE = re.compile(
    r"^(BEGIN|COMMIT|END|START\s+TRANSACTION)(\s+(TRANSACTION|WORK))?(\s+AND\s+(NO\s+)?CHAIN)?$",
    re.IGNORECASE,
)

# DDL 키워드
DDL_KEYWORDS = {
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "COMMENT",
    "RENAME",
    "GRANT",
    "REVOKE",
    "REINDEX",
}

# release_migration_approval.py 에 등록된 업무 DB 마이그레이션 목록
KNOWN_ACCT_BUSINESS_MIGRATIONS = {
    "migrations/20261030_obys_work_schedules.sql",
    "migrations/20261102_obys_attendance_corrections.sql",
    "migrations/20261105_obys_attendance_self_entry.sql",
    "migrations/20261105_obys_app_install_status.sql",
}


@dataclass
class CheckResult:
    path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().upper()


def is_excluded_path(path: str | Path) -> bool:
    """rollback/ 등 검사 제외 대상 경로인지 확인한다."""
    posix = Path(path).as_posix()
    return "rollback/" in posix or posix.startswith("rollback/")


def is_migration_file(path: str | Path) -> bool:
    """검사 대상 migrations/*.sql 인지 확인한다."""
    posix = Path(path).as_posix()
    if is_excluded_path(posix):
        return False
    if not posix.endswith(".sql"):
        return False
    parts = Path(posix).parts
    if "migrations" in parts:
        idx = parts.index("migrations")
        if len(parts) == idx + 2:
            return True
    return False


def get_header_comments(text: str) -> str:
    """파일 시작부의 주석 블록을 추출한다."""
    lines = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if in_block:
            lines.append(stripped)
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            lines.append(stripped)
            if "*/" not in stripped[2:]:
                in_block = True
            continue
        if stripped.startswith("--"):
            lines.append(stripped)
            continue
        # 주석이 아닌 첫 줄에 도달하면 헤더 주석 종료
        break
    return "\n".join(lines)


def is_deferred_or_business_db(text: str, path: str | Path = "") -> bool:
    """apply_release_migrations.sh 의 DEFER 규칙에 해당하는 업무 DB 파일인지 판정한다."""
    posix = Path(path).as_posix()
    if is_excluded_path(posix):
        return True

    rel_name = "migrations/" + Path(path).name
    if rel_name in KNOWN_ACCT_BUSINESS_MIGRATIONS:
        return True

    header = get_header_comments(text)
    return bool("업무 DB" in header or "target=acct_business" in header)


def split_statements_with_lines(sql: str) -> list[dict]:
    """주석·문자열·$태그$ 본문을 존중해 최상위 문장과 시작 행 번호를 추출한다."""
    out: list[dict] = []
    buf: list[str] = []
    shape: list[str] = []
    bodies: list[str] = []
    i, n = 0, len(sql)
    paren = 0
    stmt_line = 1
    in_stmt = False

    while i < n:
        c = sql[i]

        # 단일행 주석: --
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue

        # 다중행 주석: /* ... */
        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            shape.append(" ")
            continue

        # 문장 시작 행 기록
        if not in_stmt:
            if c.isspace():
                i += 1
                continue
            in_stmt = True
            stmt_line = sql[:i].count("\n") + 1

        # 이스케이프 문자열 및 일반 단일 따옴표 문자열
        escape_string = (
            c in "Ee"
            and sql.startswith("'", i + 1)
            and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_$"))
        )
        if c == "'" or escape_string:
            j = i + (2 if escape_string else 1)
            while j < n:
                if escape_string and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            buf.append(sql[i : j + 1])
            shape.append("'S'")
            if escape_string and j >= n:
                shape.append(" $UNTERMINATED_ESCAPE_STRING$ ")
            i = j + 1
            continue

        # 이중 따옴표 식별자
        if c == '"':
            j = sql.find('"', i + 1)
            j = n - 1 if j < 0 else j
            buf.append(sql[i : j + 1])
            shape.append(sql[i : j + 1].upper())
            i = j + 1
            continue

        # Dollar quotes: $tag$ ... $tag$ (DO $$ BEGIN ... END $$ 포함)
        if c == "$":
            m = re.match(r"\$[A-Za-z0-9_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j < 0 else j
                bodies.append(sql[i + len(tag) : j])
                buf.append(sql[i : j + len(tag)])
                shape.append(" $BODY$ ")
                i = j + len(tag)
                continue

        # 괄호 깊이 추적
        if c == "(":
            paren += 1
        elif c == ")":
            paren = max(0, paren - 1)

        # 문장 종결자
        if c == ";" and paren == 0:
            text = "".join(buf).strip()
            if text:
                out.append(
                    {
                        "text": text,
                        "shape": _norm("".join(shape)),
                        "bodies": bodies,
                        "line": stmt_line,
                    }
                )
            buf, shape, bodies = [], [], []
            in_stmt = False
            i += 1
            continue

        buf.append(c)
        shape.append(c)
        i += 1

    text = "".join(buf).strip()
    if text:
        out.append(
            {
                "text": text,
                "shape": _norm("".join(shape)),
                "bodies": bodies,
                "line": stmt_line,
            }
        )
    return out


def is_transaction_statement(shape: str) -> bool:
    """최상위 BEGIN / COMMIT / START TRANSACTION / END 문인지 판별."""
    s = shape.strip().upper()
    if TXN_STMT_RE.match(s):
        return True
    if s.startswith(("BEGIN ", "START TRANSACTION ")):
        return True
    return bool(s.startswith(("COMMIT ", "END ")))


def is_ddl_statement(shape: str) -> bool:
    """DDL 문장인지 확인."""
    words = shape.strip().split()
    if not words:
        return False
    return words[0].upper() in DDL_KEYWORDS


def is_lock_timeout_statement(text: str, shape: str) -> bool:
    """SET LOCAL lock_timeout 구문인지 확인."""
    if re.search(r"\bSET\s+(?:LOCAL\s+)?lock_timeout\b", text, re.IGNORECASE):
        return True
    return bool(
        re.search(r"\bset_config\s*\(\s*['\"]lock_timeout['\"]", text, re.IGNORECASE)
    )


def check_migration_text(text: str, path: str = "") -> CheckResult:
    """마이그레이션 SQL 본문을 검사한다."""
    path_display = path or "<sql>"

    # 1. 제외 경로(rollback/) 확인
    if path and is_excluded_path(path):
        return CheckResult(
            path=path_display,
            skipped=True,
            skip_reason="rollback directory excluded",
        )

    # 2. 업무 DB 표기 파일 확인
    if is_deferred_or_business_db(text, path):
        return CheckResult(
            path=path_display,
            skipped=True,
            skip_reason="business db migration deferred",
        )

    # 3. 문장 분해 및 검사
    statements = split_statements_with_lines(text)
    errors: list[str] = []
    warnings: list[str] = []

    first_ddl_line: int | None = None
    has_lock_timeout_before_ddl = False

    for stmt in statements:
        shape = stmt["shape"]
        stmt_text = stmt["text"]
        line = stmt["line"]

        # 차단 조건: 최상위 BEGIN;/COMMIT;
        if is_transaction_statement(shape):
            errors.append(f"{path_display}:{line}: {BLOCK_MESSAGE}")

        if first_ddl_line is None:
            if is_lock_timeout_statement(stmt_text, shape):
                has_lock_timeout_before_ddl = True
            elif is_ddl_statement(shape):
                first_ddl_line = line

    # 경고 조건: 첫 DDL 앞에 SET LOCAL lock_timeout 누락
    if first_ddl_line is not None and not has_lock_timeout_before_ddl:
        warnings.append(
            f"{path_display}:{first_ddl_line}: [경고] 첫 DDL 앞에 SET LOCAL lock_timeout 이 없습니다 "
            "(권장: SET LOCAL lock_timeout = '5s';)"
        )

    return CheckResult(
        path=path_display,
        errors=errors,
        warnings=warnings,
    )


def check_migration_file(path: str | Path) -> CheckResult:
    """마이그레이션 파일 경로를 검사한다."""
    p = Path(path)
    if is_excluded_path(p):
        return CheckResult(
            path=str(path),
            skipped=True,
            skip_reason="rollback directory excluded",
        )
    if not p.is_file():
        return CheckResult(
            path=str(path),
            skipped=True,
            skip_reason="file not found",
        )
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return CheckResult(
            path=str(path),
            errors=[f"{path}:1: 파일을 읽을 수 없습니다 ({exc})"],
        )
    return check_migration_text(text, path=str(path))


def check_files(paths: Iterable[str | Path]) -> list[CheckResult]:
    """여러 파일을 순회 검사한다."""
    results: list[CheckResult] = []
    for path in paths:
        results.append(check_migration_file(path))
    return results


def get_staged_migration_files(repo_root: Path | None = None) -> list[str]:
    """git diff --cached 로 새로 추가되거나 수정된 migrations/*.sql 목록을 조회한다."""
    if repo_root is None:
        repo_root = Path.cwd()
    try:
        res = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--cached",
                "--name-only",
                "--diff-filter=AM",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        return [f for f in files if is_migration_file(f)]
    except (subprocess.SubprocessError, OSError):
        return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="마이그레이션 내부 BEGIN/COMMIT 및 lock_timeout 누락 방어 게이트"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="검사할 마이그레이션 파일 경로 (생략 시 git staged 마이그레이션 자동 검사)",
    )
    parser.add_argument(
        "--paths",
        dest="paths_flag",
        nargs="*",
        help="검사할 마이그레이션 파일 경로 (플래그 형태)",
    )
    args = parser.parse_args(argv)

    target_paths: list[str | Path] = list(args.paths)
    if args.paths_flag:
        target_paths.extend(args.paths_flag)

    if not target_paths:
        target_paths = get_staged_migration_files()

    if not target_paths:
        return 0

    results = check_files(target_paths)
    has_errors = False

    for res in results:
        if res.skipped:
            continue
        for warning in res.warnings:
            print(warning, file=sys.stderr)
        for error in res.errors:
            print(error, file=sys.stderr)
            has_errors = True

    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
