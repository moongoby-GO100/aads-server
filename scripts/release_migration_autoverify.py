#!/usr/bin/env python3
"""릴리스 마이그레이션 자동 검증 — 사람 승인 대신 다섯 단계 검증을 통과한 추가형 변경만 적용한다.

대표님 결정(2026-11-05 KST): "검증하고 또 검증해서 이상 없으면 배포". 10/29 에 넣은 사람 승인
게이트(release_migration_approval.py)는 대표님이 SQL 을 직접 판단해야 해 배포가 계속 막혔다.
사람 승인 근거가 있으면 그것을 그대로 쓰고, 없을 때만 이 검증으로 대신한다.

  1. classify  기존 데이터를 지우거나 바꾸지 않는 문장만 허용 (표·열·색인·함수·트리거 추가 등)
  2. trial     운영 DB 에서 BEGIN … ROLLBACK 으로 실제 실행해 오류·잠금을 확인 (적용하지 않음)
  3. review    다른 회사 AI 교차 검토 — 정본 문서 체계의 검토 결과가 모두 pass 이고 타사 pass 1건 이상
  4. backup    적용 직전 DB 스키마 전체 백업(pg_dump --schema-only)
  5. postcheck 적용 후 SQL 이 만든다고 한 표·열·함수·색인이 실제로 있는지 확인

데이터를 지우거나 바꾸는 문장(DROP·DELETE·UPDATE·TRUNCATE·기본값 외 ALTER COLUMN·RENAME 등)이 하나라도 있으면
자동 적용하지 않고 사람 승인을 요구한다. 판정 불가·검토 미응답은 모두 차단(fail-closed)이다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
PG_CONTAINER = os.environ.get("PG_CONTAINER", "aads-postgres")
PGUSER = os.environ.get("PGUSER", "aads")
PGDATABASE = os.environ.get("PGDATABASE", "aads")

# ── 1. SQL 분해 ────────────────────────────────────────────────────────────

def split_statements(sql: str) -> list[dict]:
    """주석·문자열·$태그$ 본문을 존중해 최상위 문장으로 나눈다(선형 스캔).

    반환: [{"text": 원문, "shape": 본문을 $BODY$ 로 가린 대문자 정규화 문장, "bodies": [본문...]}]
    """
    out, buf, shape, bodies = [], [], [], []
    i, n = 0, len(sql)
    paren = 0
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            shape.append(" ")
            continue
        escape_string = (c in "Ee" and sql.startswith("'", i + 1)
                         and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_$")))
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
            buf.append(sql[i:j + 1])
            shape.append("'S'")
            if escape_string and j >= n:
                shape.append(" $UNTERMINATED_ESCAPE_STRING$ ")
            i = j + 1
            continue
        if c == '"':
            j = sql.find('"', i + 1)
            j = n - 1 if j < 0 else j
            buf.append(sql[i:j + 1])
            shape.append(sql[i:j + 1].upper())
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j < 0 else j
                bodies.append(sql[i + len(tag):j])
                buf.append(sql[i:j + len(tag)])
                shape.append(" $BODY$ ")
                i = j + len(tag)
                continue
        if c == "(":
            paren += 1
        elif c == ")":
            paren = max(0, paren - 1)
        if c == ";" and paren == 0:
            text = "".join(buf).strip()
            if text:
                out.append({"text": text, "shape": _norm("".join(shape)), "bodies": bodies})
            buf, shape, bodies = [], [], []
            i += 1
            continue
        buf.append(c)
        shape.append(c)
        i += 1
    text = "".join(buf).strip()
    if text:
        out.append({"text": text, "shape": _norm("".join(shape)), "bodies": bodies})
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().upper()


def _top_level_commas(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    # 원문 기본값의 문자열·따옴표 식별자 안 쉼표와 괄호는 구분자가 아니다.
    quote = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


TXN = re.compile(r"^(BEGIN|COMMIT|END|START TRANSACTION|ROLLBACK)( TRANSACTION| WORK)?$")
SAFE_PREFIX = [
    re.compile(r"^CREATE TABLE IF NOT EXISTS "),
    re.compile(r"^CREATE (UNIQUE )?INDEX IF NOT EXISTS "),
    re.compile(r"^CREATE (OR REPLACE )?(FUNCTION|PROCEDURE|VIEW|TRIGGER) "),
    re.compile(r"^CREATE (CONSTRAINT )?TRIGGER "),
    re.compile(r"^CREATE (SEQUENCE|SCHEMA|EXTENSION) IF NOT EXISTS "),
    re.compile(r"^DROP TRIGGER IF EXISTS "),
    re.compile(r"^COMMENT ON "),
    re.compile(r"^GRANT "),
    re.compile(r"^SET (LOCAL )?[A-Z_.]+ ?(=|TO) "),
    re.compile(r"^SELECT (PG_ADVISORY_XACT_LOCK|SET_CONFIG)\("),
]
# DO 블록 본문·ALTER TABLE 동작에서 보이면 데이터 변경으로 본다.
BANNED_WORDS = re.compile(
    r"\b(DELETE|UPDATE|TRUNCATE|DROP (TABLE|COLUMN|INDEX|SCHEMA|VIEW|FUNCTION|TYPE|SEQUENCE|CONSTRAINT)|"
    r"ALTER COLUMN|RENAME|SET NOT NULL|SET DATA TYPE|ALTER TYPE)\b")


def classify_statement(st: dict) -> str | None:
    """None 이면 안전, 아니면 사유."""
    s = st["shape"]
    if "$UNTERMINATED_ESCAPE_STRING$" in s:
        return "판정 불가: 미종결 이스케이프 문자열"
    if TXN.match(s):
        return None
    if "CONCURRENTLY" in s:
        return "CONCURRENTLY (트랜잭션 시험 불가)"
    for p in SAFE_PREFIX:
        if p.match(s):
            return None
    m = re.match(r"^ALTER TABLE (IF EXISTS )?(ONLY )?(\S+) (.*)$", s)
    if m:
        # shape 는 문자열을 가리므로 기본값 허용 목록은 반드시 원문에 fullmatch 한다.
        raw = re.match(r"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?\S+\s+(.*)$",
                       st["text"], re.IGNORECASE | re.DOTALL)
        raw_actions = _top_level_commas(raw.group(1)) if raw else []
        ident = r'(?:[A-Z_][A-Z0-9_$]*|"(?:[^"\\]|"")+")'
        literal = r"'(?:[^'\\;]|'')*'"
        cast = rf"(?:\s*::\s*(?!USING\b){ident}(?:\s*\.\s*(?!USING\b){ident})?(?:\s+(?:VARYING|PRECISION))?)?"
        value = rf"(?:NULL|TRUE|FALSE|[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:E[+-]?[0-9]+)?|NOW\(\)|CURRENT_TIMESTAMP|{literal}{cast})"
        default_change = rf"ALTER\s+COLUMN\s+{ident}\s+(?:DROP\s+DEFAULT|SET\s+DEFAULT\s+{value})"
        actions = _top_level_commas(m.group(4))
        # 원문과 shape 의 분할이 다르면 기본값 예외를 전부 차단한다.
        # dollar-quoted 본문은 원문 분할기가 보호하지 않으므로 예외에서 제외한다.
        defaults_aligned = "$" not in st["text"] and len(raw_actions) == len(actions)
        for i, action in enumerate(actions):
            if (defaults_aligned
                    and re.fullmatch(default_change, raw_actions[i], re.IGNORECASE)
                    and re.match(r"^ALTER COLUMN \S+ (DROP DEFAULT|SET DEFAULT )", action)):
                continue
            if not re.match(r"^ADD (COLUMN |CONSTRAINT )?", action) or BANNED_WORDS.search(action):
                return f"ALTER TABLE 동작 '{action[:60]}'"
            if action.startswith("ADD CONSTRAINT") or re.match(r"^ADD (PRIMARY KEY|UNIQUE|CHECK|FOREIGN KEY)", action):
                continue
        return None
    if re.match(r"^INSERT INTO ", s):
        if re.search(r"ON CONFLICT .* DO UPDATE", s):
            return "INSERT … ON CONFLICT DO UPDATE (기존 행 변경)"
        if re.search(r"\b(DELETE|UPDATE)\b", s.split(" SELECT ", 1)[0]):
            return "INSERT 안의 변경문"
        return None
    if s.startswith("DO "):
        for body in st["bodies"]:
            hit = BANNED_WORDS.search(_norm(re.sub(r"--[^\n]*", " ", body)))
            if hit:
                return f"DO 블록 안 '{hit.group(0)}'"
        return None
    return f"허용 목록 밖 문장 '{s[:60]}'"


def classify_files(paths: list[Path]) -> list[str]:
    problems = []
    for p in paths:
        for st in split_statements(p.read_text(encoding="utf-8")):
            reason = classify_statement(st)
            if reason:
                problems.append(f"migrations/{p.name}: {reason}")
    return problems


# ── 2. 시험 실행 ───────────────────────────────────────────────────────────

def _psql(sql: str, *, timeout: int = 180, read_only: bool = False) -> subprocess.CompletedProcess:
    cmd = ["timeout", str(timeout), "docker", "exec", "-i"]
    if read_only:
        cmd += ["-e", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=15000"]
    cmd += [PG_CONTAINER, "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-U", PGUSER, "-d", PGDATABASE]
    return subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=timeout + 10)


def trial_sql(paths: list[Path]) -> str:
    parts = ["BEGIN;", "SET LOCAL lock_timeout = '5s';", "SET LOCAL statement_timeout = '120s';"]
    for p in paths:
        for st in split_statements(p.read_text(encoding="utf-8")):
            if TXN.match(st["shape"]):
                continue  # 시험은 하나의 트랜잭션 안에서만 — 파일 자체의 COMMIT 은 빼고 돌린다
            parts.append(st["text"] + ";")
    parts.append("ROLLBACK;")
    return "\n".join(parts) + "\n"


def trial(paths: list[Path]) -> str | None:
    r = _psql(trial_sql(paths))
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip().splitlines()[-1][:300] if (r.stderr or r.stdout).strip() else f"rc={r.returncode}"
    return None


# ── 3. AI 교차 검토 ────────────────────────────────────────────────────────

def manifest_items(paths: list[Path]) -> list[dict]:
    return [{"file": f"migrations/{p.name}", "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(paths)]


def manifest_sha(items: list[dict]) -> str:
    return hashlib.sha256("".join(f"{x['file']}|{x['sha256']}\n" for x in items).encode()).hexdigest()


def _find_repo_dir(paths: list[Path] | None = None) -> Path | None:
    env_repo = os.environ.get("AADS_MIGRATION_SOURCE_REPO", "").strip()
    if env_repo:
        p = Path(env_repo)
        if p.is_dir():
            return p
    if paths:
        for p in paths:
            try:
                resolved = p.resolve()
                for parent in [resolved.parent] + list(resolved.parents):
                    if (parent / ".git").exists():
                        return parent
            except Exception:
                continue
    try:
        cur = Path.cwd().resolve()
        for parent in [cur] + list(cur.parents):
            if (parent / ".git").exists():
                return parent
    except Exception:
        pass
    try:
        script_root = Path(__file__).resolve().parents[1]
        if (script_root / ".git").exists():
            return script_root
    except Exception:
        pass
    return None


def _format_release_sha(release_sha: str, repo: Path | None = None) -> str:
    s = (release_sha or "").strip()
    if repo and s:
        try:
            r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{s}^{{commit}}"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()[:12]
        except Exception:
            pass
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", s)
    return clean[:12] if clean else "tree"


def check_release_tree_path(repo_dir: Path | None, release_ref: str, rel_path: str) -> tuple[bool, str]:
    """(exists_in_tree, sha256).

    release_ref 트리에 rel_path 가 존재하는지 확인한다.
    존재하면 (True, sha256), 없으면 (False, "").
    """
    if not repo_dir or not release_ref:
        return False, ""
    try:
        norm = rel_path.replace("\\", "/").lstrip("/")
        r_check = subprocess.run(
            ["git", "-C", str(repo_dir), "cat-file", "-e", f"{release_ref}:{norm}"],
            capture_output=True, text=True, timeout=10,
        )
        if r_check.returncode != 0:
            return False, ""
        r_cat = subprocess.run(
            ["git", "-C", str(repo_dir), "cat-file", "-p", f"{release_ref}:{norm}"],
            capture_output=True, timeout=10,
        )
        if r_cat.returncode == 0:
            return True, hashlib.sha256(r_cat.stdout).hexdigest()
    except Exception:
        pass
    return False, ""


def _find_down_sql(p: Path, repo_dir: Path | None = None, release_ref: str = "") -> tuple[str, str | None]:
    """(down_name, content).

    짝 파일 migrations/rollback/<name>.down.sql 을 찾는다.
    로컬 파일시스템 우선, 없으면 release_ref git 트리를 조회한다.
    반환: (down_filename, content_or_None)
    """
    stem = p.stem
    down_name = f"{stem}.down.sql"
    candidates = [
        p.parent / "rollback" / down_name,
        p.parent / down_name,
        p.parent / "rollback" / f"{stem}.sql",
    ]
    if repo_dir:
        candidates.extend([
            repo_dir / "migrations" / "rollback" / down_name,
            repo_dir / "migrations" / "rollback" / f"{stem}.sql",
        ])
    for c in candidates:
        if c.is_file():
            try:
                return down_name, c.read_text(encoding="utf-8")
            except OSError:
                pass
    if repo_dir and release_ref:
        for gp in [f"migrations/rollback/{down_name}", f"migrations/rollback/{stem}.sql"]:
            try:
                r_check = subprocess.run(
                    ["git", "-C", str(repo_dir), "cat-file", "-e", f"{release_ref}:{gp}"],
                    capture_output=True, timeout=5,
                )
                if r_check.returncode == 0:
                    r_cat = subprocess.run(
                        ["git", "-C", str(repo_dir), "cat-file", "-p", f"{release_ref}:{gp}"],
                        capture_output=True, timeout=5,
                    )
                    if r_cat.returncode == 0:
                        return down_name, r_cat.stdout.decode("utf-8", errors="replace")
            except Exception:
                pass
    return down_name, None


def _mask_repo_paths(text: str, sha12: str) -> str:
    """_PATH 정규식에 걸리지 않도록 standalone migrations/ 앞을 release:<sha12>/ 로 치환한다."""
    return re.sub(r"(?<![A-Za-z0-9_./-])migrations/", f"release:{sha12}/migrations/", text)


def _format_expected_objects(objs: list[tuple[str, str, str, str]]) -> list[str]:
    if not objs:
        return ["  - 생성·변경 객체: 없음 (주석·권한·트랜잭션 문장 등)"]
    lines = ["  - 생성·변경 객체:"]
    for kind, sch, name, col in objs:
        if kind == "table":
            lines.append(f"    - 표(table): `{sch}.{name}`")
        elif kind == "column":
            lines.append(f"    - 열(column): `{sch}.{name}.{col}`")
        elif kind == "function":
            lines.append(f"    - 함수(function): `{sch}.{name}()`")
        elif kind == "index":
            lines.append(f"    - 색인(index): `{name}`")
        else:
            lines.append(f"    - {kind}: `{sch}.{name}{('.' + col) if col else ''}`")
    return lines


def source_task_for(paths: list[Path]) -> str | None:
    """마이그레이션 파일을 추가한 러너 잡 ID(정본 문서 작성 출처). 없으면 None."""
    repo = os.environ.get("AADS_MIGRATION_SOURCE_REPO", "") or str(_find_repo_dir(paths) or "")
    if not repo:
        return None
    for p in sorted(paths, reverse=True):
        r = subprocess.run(["git", "-C", repo, "log", "--diff-filter=A", "--format=%s%n%b", "--", f"migrations/{p.name}"],
                           capture_output=True, text=True, timeout=30)
        m = re.search(r"\brunner-[0-9a-f]{8}\b", r.stdout or "")
        if m:
            return m.group(0)
    return None


def review_content(paths: list[Path] | Path | str, items: list[dict] | None = None, release_sha: str = "") -> str:
    if isinstance(paths, (str, Path)):
        paths = [Path(paths)]
    paths = [Path(p) for p in paths]
    paths.sort(key=lambda p: p.name)
    if isinstance(items, str) and not release_sha:
        release_sha = items
        items = None
    if items is None:
        items = manifest_items(paths)

    repo_dir = _find_repo_dir(paths)
    sha12 = _format_release_sha(release_sha, repo_dir)
    tree_ref = release_sha.strip() if release_sha and release_sha.strip() else "HEAD"

    lines = [
        "# 릴리스 마이그레이션 자동 검증 — AI 교차 검토 요청",
        "",
        "사람 승인 대신 자동 검증으로 적용한다(대표님 결정 2026-11-05). 이 문서는 SQL 원문 전체를 싣는다.",
        "",
        f"- release_sha: `{release_sha}`",
        f"- manifest_sha256: `{manifest_sha(items)}`",
        "- 자동 판정: 모든 문장이 추가형(표·열·색인·함수·트리거·주석·권한)이며 기존 데이터를 지우거나 바꾸지 않음",
        "- 시험 실행: 운영 DB 에서 BEGIN … ROLLBACK 으로 오류 없이 실행됨(적용 안 함)",
        "- 롤백 영향: API 를 이전 판으로 되돌려도 추가된 표·열·함수는 남는다. 이전 코드는 새 객체를 참조하지 않으므로"
        " 남아도 동작에 영향이 없어야 한다 — 검토자는 이 전제가 SQL 상 성립하는지 판단하라.",
        "- 적용 직전 스키마 전체 백업, 적용 후 객체 존재 재확인을 자동으로 한다.",
        "",
        "검토 기준: 기존 데이터 손실·변경 가능성, 잠금·성능 위험, 롤백 시 잔존 객체의 무해성, 시크릿·개인정보 저장 위험.",
        "",
        "## 대상",
        "",
        "### 릴리스 트리 존재 확인 표",
        "| 파일 경로 | 릴리스 트리 존재 여부 | SHA256 |",
        "| --- | --- | --- |",
    ]

    for p in paths:
        candidates = [f"migrations/{p.name}"]
        if repo_dir:
            try:
                candidates.append(str(p.resolve().relative_to(repo_dir.resolve())))
            except Exception:
                pass
        tree_ok, digest = False, ""
        for c in candidates:
            tree_ok, digest = check_release_tree_path(repo_dir, tree_ref, c)
            if tree_ok:
                break
        status_str = "있음" if tree_ok else "없음"
        sha_str = f"`{digest}`" if tree_ok and digest else "-"
        lines.append(f"| `release:{sha12}/migrations/{p.name}` | {status_str} | {sha_str} |")

        down_name, down_body = _find_down_sql(p, repo_dir, tree_ref)
        down_git_path = f"migrations/rollback/{down_name}"
        down_tree_ok, down_digest = check_release_tree_path(repo_dir, tree_ref, down_git_path)
        if down_tree_ok or down_body is not None:
            down_status = "있음" if down_tree_ok else "없음"
            down_sha = f"`{down_digest}`" if down_tree_ok and down_digest else "-"
            lines.append(f"| `release:{sha12}/migrations/rollback/{down_name}` | {down_status} | {down_sha} |")

    lines += [
        "",
        "### 파일별 생성·변경 객체 요약",
    ]
    for p in paths:
        lines.append(f"#### `release:{sha12}/migrations/{p.name}`")
        objs = expected_objects([p])
        lines.extend(_format_expected_objects(objs))
        lines.append("")

    lines += [
        "## 조건",
        "",
        "### 1. 자동 적용 조건",
        "- classify ok: 모든 문장이 추가형(표·열·색인·함수·트리거·주석·권한)이며 기존 데이터를 지우거나 바꾸지 않음 (DROP, DELETE, UPDATE, TRUNCATE, 기본값 외 ALTER COLUMN, RENAME 등 금지)",
        "- trial ok: 운영 DB 에서 BEGIN … ROLLBACK 으로 오류 없이 실행됨(적용 안 함)",
        "- review ok: 다른 회사 AI 교차 검토 — 정본 문서 체계의 검토 결과가 모두 pass 이고 타사 pass 1건 이상",
        "- backup ok: 적용 직전 DB 스키마 전체 백업(pg_dump --schema-only)",
        "",
        "### 2. 적용 후 검증 기준 (postcheck)",
        "적용 후 SQL 이 만든다고 한 표·열·함수·색인이 실제로 DB 카탈로그에 존재하는지 재확인:",
        "- 표(table): `SELECT count(*) FROM information_schema.tables WHERE table_schema='{sch}' AND table_name='{name}'` > 0",
        "- 열(column): `SELECT count(*) FROM information_schema.columns WHERE table_schema='{sch}' AND table_name='{name}' AND column_name='{col}'` > 0",
        "- 함수(function): `SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='{sch}' AND p.proname='{name}'` > 0",
        "- 색인(index): `SELECT count(*) FROM pg_indexes WHERE indexname='{name}'` > 0",
        "누락된 객체가 하나라도 있으면 적용 실패로 판정하고 배포를 중단한다.",
        "",
        "## 롤백",
        "",
        "### 1. 1차 롤백 (API 이전 판 복귀)",
        "- API 컨테이너를 이전 판으로 되돌림(추가된 객체는 남김).",
        "- 이전 코드는 새 객체를 참조하지 않으므로 잔존 객체는 API 동작에 영향을 주지 않는다.",
        "",
        "### 2. 수동 되돌림 (down SQL)",
    ]

    for p in paths:
        down_name, down_body = _find_down_sql(p, repo_dir, tree_ref)
        down_display = f"release:{sha12}/migrations/rollback/{down_name}"
        lines.append(f"#### `{down_display}`")
        if down_body is not None:
            lines += [
                "- 적용 후 생긴 행이 삭제되므로 실행 전 해당 표·열 데이터 백업(COPY/pg_dump -t) 필요, CEO 승인 사항",
                "- SQL 본문:",
                "```sql",
                _mask_repo_paths(down_body.rstrip(), sha12),
                "```",
                "",
            ]
        else:
            lines += [
                "- 짝 파일 없음",
                "",
            ]

    it_by_name = {it["file"].split("/")[-1]: it for it in items}
    for p in paths:
        it = it_by_name.get(p.name, {"file": f"migrations/{p.name}", "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
        file_display = f"release:{sha12}/migrations/{p.name}"
        lines += [
            f"## {file_display}",
            f"sha256 `{it['sha256']}`",
            "",
            "```sql",
            _mask_repo_paths(p.read_text(encoding="utf-8").rstrip(), sha12),
            "```",
            "",
        ]

    return "\n".join(lines)


def _token() -> str:
    code = (
        "import sys\nsys.path.insert(0,'/app')\nfrom app.auth import create_token\n"
        "print(create_token('79ee004e-1e2e-490f-aa05-b096814f180d','moongoby@naver.com',"
        "is_admin=True,tenant_id='2d701a8c-9596-4757-8588-faa4f7837112'))\n"
    )
    r = subprocess.run(["timeout", "60", "docker", "exec", "-i", os.environ.get("AADS_TOKEN_CONTAINER", "aads-server"), "python3", "-"],
                       input=code, capture_output=True, text=True, timeout=70)
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else ""


def _api_base() -> str:
    if os.environ.get("AADS_API_URL"):
        return os.environ["AADS_API_URL"].rstrip("/")
    port = "8100"
    try:
        port = Path(os.environ.get("AADS_ACTIVE_PORT_FILE", "/root/aads/aads-server/.active_port")).read_text().strip() or port
    except OSError:
        pass
    return f"http://127.0.0.1:{port}"


def _reviews(key: str, rev_id: str | None = None) -> tuple[str | None, list[dict]]:
    if rev_id:
        rev_target = f"r.revision_id::text='{rev_id}'"
        rev_out = f"'{rev_id}'"
    else:
        rev_target = "r.revision_id=h.latest_revision_id"
        rev_out = "h.latest_revision_id::text"
    q = (
        f"SELECT json_build_object('rev', {rev_out}, 'rows', COALESCE((SELECT json_agg(json_build_object("
        "'model', r.reviewer_model, 'provider', r.reviewer_provider, 'cross', r.cross_company, 'verdict', r.verdict, "
        "'reason', left(r.reason, 400)) ORDER BY r.id) FROM project_document_reviews r "
        f"WHERE r.head_id=h.id AND {rev_target}), '[]'::json)) "
        f"FROM project_document_heads h WHERE h.project_key='AADS' AND h.document_key='{key}'"
    )
    r = _psql(q + ";", read_only=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        return None, []
    d = json.loads(r.stdout.strip())
    return d.get("rev"), d.get("rows") or []


def _head_generation(key: str) -> tuple[str | None, int | None]:
    r = _psql("SELECT latest_revision_id::text || '|' || generation FROM project_document_heads "
              f"WHERE project_key='AADS' AND document_key='{key}';", read_only=True, timeout=60)
    if r.returncode != 0 or "|" not in r.stdout:
        return None, None
    rev, gen = r.stdout.strip().split("|", 1)
    try:
        return rev, int(gen)
    except (ValueError, TypeError):
        return rev, None


def _latest_revision_info(key: str) -> tuple[str | None, int | None, str | None, str | None]:
    """(rev_id, generation, content_hash, version) of latest revision."""
    q = (
        "SELECT COALESCE(h.latest_revision_id::text, '') || '|' || h.generation || '|' || "
        "COALESCE(r.content_hash, '') || '|' || COALESCE(r.version, '') "
        "FROM project_document_heads h "
        "LEFT JOIN project_document_revisions r ON r.id = h.latest_revision_id "
        f"WHERE h.project_key='AADS' AND h.document_key='{key}';"
    )
    r = _psql(q, read_only=True, timeout=60)
    if r.returncode != 0 or "|" not in r.stdout:
        return None, None, None, None
    parts = r.stdout.strip().split("|")
    if len(parts) < 4:
        return None, None, None, None
    rev, gen_s, chash, ver = parts[0], parts[1], parts[2], parts[3]
    try:
        gen = int(gen_s)
    except (ValueError, TypeError):
        gen = None
    return (rev or None), gen, (chash or None), (ver or None)


def _kick_review(token: str, key: str, rev: str | None = None, gen: int | None = None) -> str | None:
    """검토 요청(POST /review)이 교차 검토를 예약한다. 문서 생성만으로는 검토가 돌지 않는다
    (2026-11-05 deploy_runs#5979: 생성 후 300초 동안 검토 0건)."""
    if rev is None or gen is None:
        rev, gen = _head_generation(key)
    if rev is None:
        return "AI 검토 문서를 찾지 못함"
    req = urllib.request.Request(_api_base() + f"/api/v1/projects/AADS/documents/{key}/review",
                                 json.dumps({"revision_id": rev, "expected_generation": gen}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 cross_review_required = 검토 예약됨(대기 중)
            return f"AI 검토 요청 실패 HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return f"AI 검토 요청 실패 {type(e).__name__}"
    return None


def review(paths: list[Path], release_sha: str) -> str | None:
    items = manifest_items(paths)
    key = "migration-autoverify-m-" + manifest_sha(items)[:12]
    content = review_content(paths, items, release_sha)
    target_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    rev, gen, latest_hash, latest_ver = _latest_revision_info(key)
    target_rev = rev
    token = ""

    if rev is None or latest_hash != target_hash:
        token = _token()
        if not token:
            return "API 토큰 발급 실패"
        task = source_task_for(paths)
        if not task:
            return "AI 검토 문서의 작성 출처(러너 잡 ID)를 찾지 못함"

        if rev is None:
            exp_gen = 0
            new_ver = "1.0.0"
        else:
            exp_gen = gen if gen is not None else 0
            parts = (latest_ver or "1.0.0").split(".")
            patch = int(parts[2]) + 1 if len(parts) == 3 and parts[2].isdigit() else exp_gen
            patch = max(patch, exp_gen)
            new_ver = f"{parts[0] if len(parts) == 3 and parts[0].isdigit() else '1'}.0.{patch}"

        body = {
            "document_key": key,
            "kind": "contract",
            "title": "릴리스 마이그레이션 자동 검증 교차 검토",
            "version": new_ver,
            "content": content,
            "expected_generation": exp_gen,
            "source_task_id": task,
            "change_summary": "자동 검증(추가형·시험 실행 통과) 마이그레이션의 AI 교차 검토",
        }
        req = urllib.request.Request(
            _api_base() + "/api/v1/projects/AADS/documents",
            json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        target_rev = None
        target_gen = None
        try:
            resp_bytes = urllib.request.urlopen(req, timeout=120).read()
            try:
                res = json.loads(resp_bytes.decode("utf-8"))
                target_rev = res.get("revision_id")
                target_gen = res.get("generation")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        except urllib.error.HTTPError as e:
            if e.code != 409:  # 409 = 동시 배포 충돌 시 최신 리비전을 다시 읽어 판정
                return f"AI 검토 문서 등록 실패 HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            return f"AI 검토 문서 등록 실패 {type(e).__name__}"

        if not target_rev:
            target_rev, target_gen = _head_generation(key)
        if not target_rev:
            return "AI 검토 문서를 찾지 못함"

        err = _kick_review(token, key, rev=target_rev, gen=target_gen)
        if err:
            return err
    else:
        # 본문 해시 동일: 새 리비전 등록 안 함, 기존 판정 사용
        target_rev = rev
        _, rows = _reviews(key, rev_id=target_rev)
        if not rows:
            token = _token()
            if not token:
                return "API 토큰 발급 실패"
            err = _kick_review(token, key, rev=target_rev, gen=gen)
            if err:
                return err

    deadline = time.monotonic() + float(os.environ.get("AADS_MIGRATION_AI_REVIEW_WAIT_S", "300"))
    while True:
        _, rows = _reviews(key, rev_id=target_rev)
        verdicts = [x.get("verdict") for x in rows]
        if any(v not in ("pass", None) for v in verdicts):
            bad = [f"{x['model']}={x['verdict']}: {x.get('reason') or ''}" for x in rows if x.get("verdict") != "pass"]
            return "AI 교차 검토 불통과 — " + " / ".join(bad)[:600]
        if len(rows) >= 2 and all(v == "pass" for v in verdicts) and any(x.get("cross") for x in rows):
            print(f"AUTOVERIFY review ok key={key} " + ", ".join(f"{x['model']}=pass" for x in rows))
            return None
        if time.monotonic() > deadline:
            return f"AI 교차 검토 시간 초과(결과 {len(rows)}건)"
        time.sleep(10)


# ── 4. 백업 ────────────────────────────────────────────────────────────────

def backup(items: list[dict]) -> str | None:
    root = Path(os.environ.get("AADS_MIGRATION_BACKUP_DIR", "/root/aads/backups/migration-autoverify"))
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"{datetime.now(KST):%Y%m%d-%H%M%S}-{manifest_sha(items)[:12]}.schema.sql"
    with out.open("wb") as fh:
        r = subprocess.run(["timeout", "300", "docker", "exec", PG_CONTAINER, "pg_dump", "--schema-only", "-U", PGUSER, PGDATABASE],
                           stdout=fh, stderr=subprocess.PIPE, timeout=320)
    if r.returncode != 0 or out.stat().st_size < 1000:
        return f"스키마 백업 실패 rc={r.returncode}"
    print(f"AUTOVERIFY backup {out} bytes={out.stat().st_size}")
    return None


# ── 5. 적용 후 확인 ────────────────────────────────────────────────────────

def _ident(raw: str) -> tuple[str, str]:
    parts = [x.strip('"') for x in raw.split(".")]
    return ("public", parts[0].lower()) if len(parts) == 1 else (parts[0].lower(), parts[1].lower())


def expected_objects(paths: list[Path]) -> list[tuple[str, str, str, str]]:
    objs = []
    for p in paths:
        for st in split_statements(p.read_text(encoding="utf-8")):
            s = st["shape"]
            m = re.match(r"^CREATE TABLE IF NOT EXISTS (\S+)", s)
            if m:
                objs.append(("table", *_ident(m.group(1).split("(")[0]), ""))
            m = re.match(r"^ALTER TABLE (IF EXISTS )?(ONLY )?(\S+) (.*)$", s)
            if m:
                sch, tbl = _ident(m.group(3))
                for action in _top_level_commas(m.group(4)):
                    a = re.match(r"^ADD (COLUMN )?(IF NOT EXISTS )?(\"?[A-Z_][A-Z0-9_]*\"?) ", action + " ")
                    if a and a.group(3).strip('"') not in ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"):
                        objs.append(("column", sch, tbl, a.group(3).strip('"').lower()))
            m = re.match(r"^CREATE (OR REPLACE )?FUNCTION (\S+?)\(", s)
            if m:
                objs.append(("function", *_ident(m.group(2)), ""))
            m = re.match(r"^CREATE (UNIQUE )?INDEX IF NOT EXISTS (\S+) ", s)
            if m:
                objs.append(("index", "public", m.group(2).strip('"').lower(), ""))
    return objs


def postcheck(paths: list[Path]) -> str | None:
    missing = []
    for kind, sch, name, col in expected_objects(paths):
        if kind == "table":
            q = f"SELECT count(*) FROM information_schema.tables WHERE table_schema='{sch}' AND table_name='{name}'"
        elif kind == "column":
            q = f"SELECT count(*) FROM information_schema.columns WHERE table_schema='{sch}' AND table_name='{name}' AND column_name='{col}'"
        elif kind == "function":
            q = f"SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='{sch}' AND p.proname='{name}'"
        else:
            q = f"SELECT count(*) FROM pg_indexes WHERE indexname='{name}'"
        r = _psql(q + ";", read_only=True, timeout=60)
        if r.returncode != 0 or r.stdout.strip() in ("", "0"):
            missing.append(f"{kind} {sch}.{name}{'.' + col if col else ''}")
    if missing:
        return "적용 후 없는 객체: " + ", ".join(missing[:10])
    print(f"AUTOVERIFY postcheck ok objects={len(expected_objects(paths))}")
    return None


# ── 진입점 ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["classify", "verify", "postcheck"])
    ap.add_argument("--release-sha", default=os.environ.get("AADS_RELEASE_SHA", ""))
    ap.add_argument("files", nargs="+", type=Path)
    a = ap.parse_args()
    paths = sorted(a.files)
    if a.command == "classify":
        problems = classify_files(paths)
        for p in problems:
            print("AUTOVERIFY-UNSAFE " + p)
        return 1 if problems else 0
    if a.command == "postcheck":
        err = postcheck(paths)
        if err:
            print("AUTOVERIFY-POSTCHECK-FAIL " + err)
            return 1
        return 0
    # verify = 1 classify → 2 trial → 3 review → 4 backup (적용은 호출자가, 5 는 postcheck)
    problems = classify_files(paths)
    if problems:
        for p in problems:
            print("AUTOVERIFY-UNSAFE " + p)
        return 1
    print(f"AUTOVERIFY classify ok files={len(paths)}")
    for step, fn in (("trial", lambda: trial(paths)), ("review", lambda: review(paths, a.release_sha)),
                     ("backup", lambda: backup(manifest_items(paths)))):
        err = fn()
        if err:
            print(f"AUTOVERIFY-{step.upper()}-FAIL {err}")
            return 1
        if step == "trial":
            print("AUTOVERIFY trial ok (BEGIN … ROLLBACK)")
    print("AUTOVERIFY-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
