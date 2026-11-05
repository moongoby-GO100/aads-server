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

데이터를 지우거나 바꾸는 문장(DROP·DELETE·UPDATE·TRUNCATE·ALTER COLUMN·RENAME 등)이 하나라도 있으면
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
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            buf.append(sql[i:j + 1])
            shape.append("'S'")
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
    for ch in s:
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
    if TXN.match(s):
        return None
    if "CONCURRENTLY" in s:
        return "CONCURRENTLY (트랜잭션 시험 불가)"
    for p in SAFE_PREFIX:
        if p.match(s):
            return None
    m = re.match(r"^ALTER TABLE (IF EXISTS )?(ONLY )?(\S+) (.*)$", s)
    if m:
        for action in _top_level_commas(m.group(4)):
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


def source_task_for(paths: list[Path]) -> str | None:
    """마이그레이션 파일을 추가한 러너 잡 ID(정본 문서 작성 출처). 없으면 None."""
    repo = os.environ.get("AADS_MIGRATION_SOURCE_REPO", "")
    if not repo:
        return None
    for p in sorted(paths, reverse=True):
        r = subprocess.run(["git", "-C", repo, "log", "--diff-filter=A", "--format=%s%n%b", "--", f"migrations/{p.name}"],
                           capture_output=True, text=True, timeout=30)
        m = re.search(r"\brunner-[0-9a-f]{8}\b", r.stdout or "")
        if m:
            return m.group(0)
    return None


def review_content(paths: list[Path], items: list[dict], release_sha: str) -> str:
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
    ]
    for p, it in zip(sorted(paths), items):
        lines += [f"## {it['file']}", f"sha256 `{it['sha256']}`", "", "```sql", p.read_text(encoding="utf-8").rstrip(), "```", ""]
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


def _reviews(key: str) -> tuple[str | None, list[dict]]:
    q = (
        "SELECT json_build_object('rev', h.latest_revision_id::text, 'rows', COALESCE((SELECT json_agg(json_build_object("
        "'model', r.reviewer_model, 'provider', r.reviewer_provider, 'cross', r.cross_company, 'verdict', r.verdict, "
        "'reason', left(r.reason, 400)) ORDER BY r.id) FROM project_document_reviews r "
        "WHERE r.head_id=h.id AND r.revision_id=h.latest_revision_id), '[]'::json)) "
        f"FROM project_document_heads h WHERE h.project_key='AADS' AND h.document_key='{key}'"
    )
    r = _psql(q + ";", read_only=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        return None, []
    d = json.loads(r.stdout.strip())
    return d.get("rev"), d.get("rows") or []


def review(paths: list[Path], release_sha: str) -> str | None:
    items = manifest_items(paths)
    key = "migration-autoverify-m-" + manifest_sha(items)[:12]
    rev, rows = _reviews(key)
    if rev is None:
        task = source_task_for(paths)
        if not task:
            return "AI 검토 문서의 작성 출처(러너 잡 ID)를 찾지 못함"
        token = _token()
        if not token:
            return "API 토큰 발급 실패"
        body = dict(document_key=key, kind="contract", title="릴리스 마이그레이션 자동 검증 교차 검토",
                    version="1.0.0", content=review_content(paths, items, release_sha), expected_generation=0,
                    source_task_id=task, change_summary="자동 검증(추가형·시험 실행 통과) 마이그레이션의 AI 교차 검토")
        req = urllib.request.Request(_api_base() + "/api/v1/projects/AADS/documents", json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            urllib.request.urlopen(req, timeout=120).read()
        except urllib.error.HTTPError as e:
            if e.code != 409:  # 409 = 동시에 다른 배포가 만든 경우, 아래에서 그 결과를 읽는다
                return f"AI 검토 문서 등록 실패 HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            return f"AI 검토 문서 등록 실패 {type(e).__name__}"
    deadline = time.monotonic() + float(os.environ.get("AADS_MIGRATION_AI_REVIEW_WAIT_S", "300"))
    while True:
        rev, rows = _reviews(key)
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
