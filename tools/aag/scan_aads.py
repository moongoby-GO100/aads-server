#!/usr/bin/env python3
"""AAG L1/L2 — AADS 본체 아키텍처 추출기 (구조 + 계약).

behavior_check.py(L3)가 **실행 기록**에서 설계 사고를 찾는다면, 이 스크립트는
**코드**에서 같은 종류의 부채를 찾는다. 읽기 전용이다 — app/** 를 한 줄도 쓰지 않는다.

왜 만들었나. `app/api/chat.py` 와 `app/routers/chat.py` 가 둘 다 `/api/v1` 아래
`/chat` 네임스페이스를 소유한 채 몇 달을 굴렀다. 그런데 두 모듈의 정확한
METHOD+경로 충돌은 **0건**이다 — 흔한 "중복 라우트" 검사는 이 부채를
"위반 없음" 으로 보고한다. DOUBLE_MOUNT 는 같은 라우터 모듈이 같은
엔트리포인트에 반복 등록된 경우를 잡고, 정확한 METHOD+경로 충돌은 별도의
ROUTE_SHADOWED 규칙이 잡는다.

파서는 regex 가 아니라 `ast` 다. `app/api/yeoljeong_accounting.py:5` 의
`include_router` 는 **독스트링 안의 예시**이고, grep 기반 추출기는 이 파일
첫 화면부터 오탐을 낸다. SQL 도 `ast.Constant` 문자열에서만 뽑는다.

종료코드: 0 = 결함 없음 / 1 = 결함 있음 / 2 = 실행 불가(스캔 대상 0건 포함).
"실행 불가" 와 "결함 0건" 을 절대 같은 코드로 내보내지 않는다 — 0건 보고는
게이트가 죽었을 때도 똑같이 생겼기 때문에 가장 위험한 거짓말이다.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import socket
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

KST = timezone(timedelta(hours=9))

HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")

# rules_aads.yml 을 못 읽어도 도구가 죽지는 않되, 조용히 기본값으로 돌지도 않는다.
DEFAULT_RULES: dict[str, Any] = {
    "scan": {
        "router_dirs": ["app/api", "app/routers"],
        "app_roots": ["app"],
        "backup_roots": ["app", "scripts", "migrations", "tools", "config", "docker"],
        "schema_roots": ["migrations", "scripts", "docker"],
        "exclude_dir_names": ["__pycache__", "node_modules", ".git", ".next", "venv", ".venv"],
    },
    "entrypoints": {"primary": "app/main.py", "secondary": ["app/yeoljeong_main.py"]},
    "frontend": {
        "roots": ["aads-dashboard/src"],
        "extensions": [".ts", ".tsx"],
        "base_env_names": ["NEXT_PUBLIC_API_URL"],
        "fallback_base": "",
        "call_names": ["fetch", "EventSource"],
        "path_helpers": {"request": "BASE_URL"},
    },
    "backup_patterns": [
        r"\.bak$", r"\.bak[._-].*$", r"\.backup$", r"\.orig$", r"\.old$",
        r"\.save$", r"\.disabled$", r"~$", r"\.bak_.*$",
        r"_backup\.[A-Za-z0-9]+$", r"\.[A-Za-z0-9]+\.bak\..*$",
    ],
    "sql": {"ignore_tables": ["pg_catalog", "information_schema"], "known_external_tables": []},
    "severity": {
        "DUP_MODULE": "P1", "DOUBLE_MOUNT": "P1", "ORPHAN_ROUTER": "P2",
        "TABLE_NO_MODEL": "P1", "PATH_DRIFT": "P1", "ROUTE_MISSING": "P0",
        "STALE_BACKUP": "P2",
    },
    "output": {
        "graph_json": "reports/aag/aads-graph.json",
        "mermaid": "reports/aag/aads-arch.mmd",
        "findings_md": "reports/aag/aads-findings.md",
        "mermaid_max_namespaces": 40,
    },
}


def load_rules(path: Path) -> tuple[dict[str, Any], list[str]]:
    """규칙 파일을 읽는다. 없거나 깨지면 기본값 + 경고를 돌려준다."""
    warnings: list[str] = []
    if not path.exists():
        warnings.append(f"규칙 파일이 없어 내장 기본값으로 실행합니다: {path}")
        return json.loads(json.dumps(DEFAULT_RULES)), warnings
    try:
        import yaml  # 운영 이미지에 pyyaml 6.0.3 존재 (실측 2026-09-16)
    except ImportError:
        warnings.append("pyyaml 이 없어 내장 기본값으로 실행합니다.")
        return json.loads(json.dumps(DEFAULT_RULES)), warnings
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"규칙 파일 파싱 실패 — 기본값으로 실행합니다: {exc}")
        return json.loads(json.dumps(DEFAULT_RULES)), warnings

    merged = json.loads(json.dumps(DEFAULT_RULES))
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged, warnings


# ══════════════════════════════════════════════════════════════════════
# 순수 함수 — DB·FS 없이 단위테스트한다
# ══════════════════════════════════════════════════════════════════════

_PARAM_RE = re.compile(r"\{[^{}]*\}")
_MULTISLASH_RE = re.compile(r"/{2,}")


def join_path(*parts: str) -> str:
    """FastAPI 의 prefix 합성. 빈 조각은 건너뛰고 끝 슬래시는 떼어낸다.

    include_router(prefix=...) 와 APIRouter(prefix=...) 가 둘 다 있을 수 있어
    합성은 두 단계다. 합성을 빼면 프런트 호출이 전부 ROUTE_MISSING 으로 터진다.
    """
    out = ""
    for part in parts:
        if not part:
            continue
        if not part.startswith("/"):
            part = "/" + part
        out += part.rstrip("/")
    return out or "/"


def normalize_route(path: str) -> str:
    """경로 파라미터의 *이름* 을 지워 비교 가능한 형태로 만든다.

    `/chat/messages/{message_id}` 와 프런트의 `/chat/messages/${msgId}` 는
    같은 라우트다. 이름을 남겨두면 전부 불일치로 보고된다.
    """
    if not path:
        return "/"
    p = _PARAM_RE.sub("{}", path)
    if not p.startswith("/"):
        p = "/" + p
    p = _MULTISLASH_RE.sub("/", p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def segments(path: str) -> list[str]:
    return [s for s in normalize_route(path).split("/") if s]


def namespace_key(full_path: str, api_roots: Sequence[str]) -> str:
    """네임스페이스 = API 루트 + 그 아래 첫 세그먼트.

    DOUBLE_MOUNT 의 정본 기준. api_roots 를 먼저 벗기지 않으면
    self-prefix 라우터(`APIRouter(prefix="/api/v1/review")`)가 전부
    `/api` 네임스페이스로 뭉쳐 가짜 충돌이 난다.
    """
    segs = segments(full_path)
    best: list[str] = []
    for root in api_roots:
        rsegs = segments(root) if root else []
        if rsegs and segs[: len(rsegs)] == rsegs and len(rsegs) > len(best):
            best = rsegs
    rest = segs[len(best):]
    head = rest[0] if rest else ""
    return "/" + "/".join([*best, head]) if (best or head) else "/"


def route_matches(fe_path: str, be_path: str) -> bool:
    """프런트 경로가 백엔드 라우트에 맞는가.

    백엔드의 `{}` 는 프런트의 어떤 리터럴 세그먼트와도 맞는다(런타임 값이므로).
    반대로 프런트의 `{}` 는 백엔드 `{}` 하고만 맞춘다 — 변수 자리에 백엔드
    리터럴이 오는 매칭을 허용하면 `/chat/messages/search` 같은 고정 라우트가
    아무 변수 호출에나 걸려 드리프트를 놓친다.
    """
    fe, be = segments(fe_path), segments(be_path)
    if len(fe) != len(be):
        return False
    for f, b in zip(fe, be):
        if b == "{}":
            continue
        if f != b:
            return False
    return True


def is_backup_path(path: str, patterns: Sequence[str]) -> bool:
    """파일명이 낡은 사본 꼴인가."""
    name = os.path.basename(path)
    return any(re.search(p, name) for p in patterns)


# SQL 로 볼 문자열은 **문장 키워드로 시작**하고 **그 문장의 골격**을 갖춰야 한다.
# 시작 키워드만 보면 'update failed' 같은 평범한 안내문에서 `failed` 라는 테이블이
# 태어난다. 실측으로 TABLE_NO_MODEL 93건 중 산문 기원이 다수였다.
_SQL_START_RE = re.compile(r"^\s*([a-z]+)\b", re.I)

# 문장별로 반드시 있어야 하는 뼈대. 없으면 SQL 이 아니다.
_SQL_SHAPE = {
    "select": re.compile(r"\bfrom\b", re.I),
    "with": re.compile(r"\bas\s*\(", re.I),
    "update": re.compile(r"\bset\b", re.I),
    "insert": re.compile(r"\binto\b", re.I),
    "delete": re.compile(r"\bfrom\b", re.I),
    "merge": re.compile(r"\binto\b", re.I),
    "create": re.compile(
        r"\b(table|view|index|type|schema|extension|function|trigger|sequence|materialized)\b", re.I
    ),
    "alter": re.compile(r"\b(table|view|index|type|schema|sequence)\b", re.I),
    "truncate": re.compile(r"\w", re.I),
}

_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
# SQL 안의 작은따옴표 리터럴. 주석 안 문자열(`'Runner/review … from settings order'`)이
# `settings` 를 테이블로 만들었다 — 리터럴은 통째로 비운다.
_SQL_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
# `FOR UPDATE OF m` 의 UPDATE 는 문장이 아니다 → 별칭 `of` 가 테이블이 됐다.
_FOR_LOCK_RE = re.compile(r"\bfor\s+(?:no\s+key\s+)?(?:update|share|key\s+share)\b", re.I)
# `EXTRACT(EPOCH FROM ts)` · `x IS DISTINCT FROM y` 의 FROM 은 테이블 자리가 아니다.
_FROM_IN_FUNC_RE = re.compile(
    r"\b(extract|substring|trim|overlay)\s*\(([^()]*?)\bfrom\b", re.I
)
_DISTINCT_FROM_RE = re.compile(r"\bis\s+(?:not\s+)?distinct\s+from\b", re.I)

_TABLE_REF_RE = re.compile(
    r"\b(?:from|join|insert\s+into|update|delete\s+from)\s+"
    r"(?:only\s+)?(?:\"?([a-zA-Z_][a-zA-Z0-9_$]*)\"?\.)?\"?([a-zA-Z_][a-zA-Z0-9_$]*)\"?",
    re.I,
)

# `WITH x AS (…), y AS (…)` 의 y 를 놓치면 CTE 이름이 테이블로 둔갑한다.
# `\b` 를 콤마 앞에 두면 `),\n  y AS (` 에서 경계가 성립하지 않아 매칭이 빠진다.
_CTE_RE = re.compile(
    r"(?:\bwith\b(?:\s+recursive\b)?|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", re.I
)

# 동적 테이블명(f-string 보간)이 있던 자리. UNRESOLVED 로 분리한다.
_DYNAMIC_TABLE_RE = re.compile(
    r"\b(?:from|join|insert\s+into|update|delete\s+from)\s+\{\}", re.I
)

_SQL_NOISE = {"select", "where", "values", "set", "lateral", "dual", "only", "table"}


def sql_preprocess(sql: str) -> str:
    """테이블 추출 전에 오탐의 원천을 걷어낸다.

    주석 → 리터럴 → 잠금 절 → 함수 안의 FROM 순서로 지운다. 주석을 먼저 지워야
    `),\n -- 주석\n eff AS (` 의 CTE 가 보이고, 리터럴을 지워야 안내 문구 속
    단어가 테이블로 잡히지 않는다.
    """
    text = _SQL_BLOCK_COMMENT_RE.sub(" ", sql or "")
    text = _SQL_LINE_COMMENT_RE.sub(" ", text)
    text = _SQL_LITERAL_RE.sub("''", text)
    text = _FOR_LOCK_RE.sub(" ", text)
    text = _DISTINCT_FROM_RE.sub(" IS_DISTINCT ", text)
    return _FROM_IN_FUNC_RE.sub(lambda m: f"{m.group(1)}({m.group(2)},", text)


def looks_like_sql(text: str) -> bool:
    """문장 키워드로 시작하고 그 문장의 뼈대를 갖춘 문자열만 SQL 로 본다."""
    if not text:
        return False
    m = _SQL_START_RE.match(sql_preprocess(text))
    if not m:
        return False
    shape = _SQL_SHAPE.get(m.group(1).lower())
    return bool(shape and shape.search(text))


def extract_sql_tables(sql: str, ignore: Sequence[str] = ()) -> set[str]:
    """SQL 문자열에서 참조 테이블 이름을 뽑는다.

    걸러내는 것 — 산문, 주석, 문자열 리터럴, CTE 이름, `FOR UPDATE OF`,
    `EXTRACT(… FROM col)`/`IS DISTINCT FROM`, 그리고
    `FROM jsonb_array_elements(...)` 처럼 뒤에 `(` 가 붙는 집합 반환 함수.
    """
    if not looks_like_sql(sql):
        return set()
    body = sql_preprocess(sql)
    ctes = {m.group(1).lower() for m in _CTE_RE.finditer(body)}
    ignored = {i.lower() for i in ignore}
    found: set[str] = set()
    for m in _TABLE_REF_RE.finditer(body):
        schema, name = m.group(1), m.group(2)
        low = name.lower()
        if body[m.end():m.end() + 1] == "(":
            continue  # 테이블이 아니라 함수 호출
        if low in _SQL_NOISE or low in ctes or low in ignored:
            continue
        if schema and schema.lower() in ignored:
            continue
        found.add(low)
    return found


def sql_unresolved_reasons(sql: str) -> list[str]:
    """테이블을 확정할 수 없는 사유. 결함이 아니라 UNRESOLVED 로 간다."""
    reasons = []
    if looks_like_sql(sql) and _DYNAMIC_TABLE_RE.search(sql):
        reasons.append("테이블 이름이 런타임 보간이라 확정 불가")
    elif not looks_like_sql(sql) and re.search(r"\bfrom\s+[a-zA-Z_]", sql or ""):
        if re.search(r"\b(select|insert|update|delete)\b", sql or "", re.I):
            reasons.append("SQL 조각(문장 키워드로 시작하지 않음) — 테이블 확정 불가")
    return reasons


_DDL_RE = re.compile(
    r"create\s+(?:unlogged\s+|temp(?:orary)?\s+)?"
    r"(?:table|materialized\s+view|view)\s+(?:if\s+not\s+exists\s+)?"
    r"(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?\"?([a-zA-Z_][a-zA-Z0-9_$]*)\"?",
    re.I,
)


def extract_ddl_tables(text: str) -> set[str]:
    """CREATE TABLE / VIEW 로 정의되는 이름."""
    return {m.group(1).lower() for m in _DDL_RE.finditer(text or "")}


# ── 프런트엔드 URL 해석 (순수) ────────────────────────────────────────

_VAR_MARK = "\x00VAR\x00"
_ENV_DEFAULT_RE = re.compile(
    r"process\.env\.([A-Z0-9_]+)\s*\|\|\s*(['\"])(.*?)\2", re.S
)
_QUOTED_RE = re.compile(r"^(['\"])(.*)\1$", re.S)


def url_path_of(raw: str) -> str:
    """절대 URL 이면 경로만, 아니면 그대로. 쿼리/해시는 버린다."""
    s = raw.strip()
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*(/.*)?$", s, re.S)
    if m:
        s = m.group(1) or "/"
    for cut in ("?", "#"):
        idx = s.find(cut)
        if idx >= 0:
            s = s[:idx]
    return s


_ABS_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]*)", re.S)


def external_host_of(raw: str, internal_hosts: Sequence[str]) -> str | None:
    """절대 URL 이고 호스트가 우리 것이 아니면 그 호스트, 아니면 None.

    2026-09-16: `fetch("https://raw.githubusercontent.com/...")` 두 건이
    ROUTE_MISSING(P0) 으로 잡혔다. 남의 서버에 대고 쏘는 호출을 우리 백엔드
    라우트 계약으로 재는 것은 틀렸고, 틀린 P0 가 섞이면 나머지 진짜 P0 도
    같이 무시된다. 외부 오리진은 계약 대상에서 뺀다.
    """
    # 허용 목록이 비어 있으면 아무것도 외부로 보지 않는다. 비었을 때 전부
    # 외부로 처리하면 규칙 파일 한 줄이 빠진 순간 실제 부채가 통째로 사라진다.
    if not internal_hosts:
        return None
    m = _ABS_URL_RE.match(raw.strip())
    if not m:
        return None
    host = (m.group(1) or "").split("@")[-1].split(":")[0].lower()
    if not host:
        return None
    for allowed in internal_hosts:
        a = allowed.strip().lower()
        if a and (host == a or host.endswith("." + a)):
            return None
    return host


def resolve_base_expr(expr: str, base_env_names: Sequence[str]) -> str | None:
    """`process.env.NEXT_PUBLIC_API_URL || "https://host/api/v1"` → `/api/v1`.

    기본값이 `""` 인 표현식도 **해석 성공**으로 본다 — 결과는 빈 base 이고,
    그러면 `/api/v1` 접두가 빠진 경로가 나온다. 그것이 바로 잡아야 할 드리프트다.
    UNRESOLVED 로 밀어내면 실제 부채가 통계에서 사라진다.
    """
    e = expr.strip()
    m = _ENV_DEFAULT_RE.search(e)
    if m and m.group(1) in base_env_names:
        return url_path_of(m.group(3)) if m.group(3) else ""
    q = _QUOTED_RE.match(e)
    if q:
        return url_path_of(q.group(2))
    return None


def split_template(raw: str) -> list[tuple[str, str]] | None:
    """백틱 템플릿 리터럴을 [("lit", 문자열) | ("expr", 내부식)] 로 쪼갠다.

    중첩 `${ ... }` 안의 중괄호·따옴표·중첩 템플릿을 세어 닫는 지점을 찾는다.
    """
    s = raw.strip()
    if not (s.startswith("`") and s.endswith("`") and len(s) >= 2):
        return None
    body = s[1:-1]
    parts: list[tuple[str, str]] = []
    buf = ""
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            buf += body[i:i + 2]
            i += 2
            continue
        if ch == "$" and i + 1 < len(body) and body[i + 1] == "{":
            if buf:
                parts.append(("lit", buf))
                buf = ""
            depth = 1
            j = i + 2
            while j < len(body) and depth:
                c = body[j]
                if c in "'\"`":
                    quote = c
                    j += 1
                    while j < len(body) and body[j] != quote:
                        j += 2 if body[j] == "\\" else 1
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            parts.append(("expr", body[i + 2:j]))
            i = j + 1
            continue
        buf += ch
        i += 1
    if buf:
        parts.append(("lit", buf))
    return parts


def _strip_outer_parens(expr: str) -> str:
    e = expr.strip()
    while e.startswith("(") and e.endswith(")"):
        depth = 0
        for i, ch in enumerate(e):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(e) - 1:
                    return e
        e = e[1:-1].strip()
    return e


def split_top_level(expr: str, seps: str) -> list[str]:
    """따옴표·괄호 깊이를 세며 최상위 구분자로 자른다.

    `?.` 와 `??` 는 삼항 연산자가 아니므로 구분자로 보지 않는다 — 이걸 놓치면
    옵셔널 체이닝 한 줄에 파서가 무너진다.
    """
    out: list[str] = []
    buf = ""
    depth = 0
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch in "'\"`":
            quote = ch
            j = i + 1
            tdepth = 0
            while j < n:
                c = expr[j]
                if c == "\\":
                    j += 2
                    continue
                if quote == "`" and c == "$" and j + 1 < n and expr[j + 1] == "{":
                    tdepth += 1
                    j += 2
                    continue
                if quote == "`" and c == "}" and tdepth:
                    tdepth -= 1
                elif c == quote and not tdepth:
                    break
                j += 1
            buf += expr[i:j + 1]
            i = j + 1
            continue
        elif depth == 0 and ch in seps:
            nxt = expr[i + 1:i + 2]
            prv = expr[i - 1:i] if i else ""
            if ch == "?" and (nxt in (".", "?") or prv == "?"):
                buf += ch
                i += 1
                continue
            if ch == ":" and nxt == ":":
                buf += expr[i:i + 2]
                i += 2
                continue
            out.append(buf)
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    out.append(buf)
    return out


def leading_literal(expr: str) -> str | None:
    """식의 맨 앞 문자열 리터럴의 내용. 리터럴로 시작하지 않으면 None."""
    e = _strip_outer_parens(expr)
    if not e:
        return ""
    if e[0] not in "'\"`":
        return None
    if e[0] == "`":
        parts = split_template(e[: e.index("`", 1) + 1]) if e.count("`") >= 2 else None
        if parts is None:
            return None
        return parts[0][1] if parts and parts[0][0] == "lit" else ""
    q = e[0]
    j = 1
    while j < len(e):
        if e[j] == "\\":
            j += 2
            continue
        if e[j] == q:
            break
        j += 1
    return e[1:j]


def interpolation_is_query_only(expr: str) -> bool:
    """이 보간이 만들어 낼 수 있는 것이 **쿼리스트링뿐** 인가.

    `${qs ? "?" + qs : ""}` 는 경로를 바꾸지 않는다. 이걸 UNRESOLVED 로 밀어내면
    api.ts 의 실제 API 표면 대부분이 검사 밖으로 빠진다 — 반대로 경로 세그먼트를
    만들 수 있는 보간까지 삼키면 없는 라우트를 있다고 보고하게 된다.
    그래서 삼항의 **모든 가지**가 빈 문자열이거나 `?`/`&` 로 시작할 때만 참이다.
    """
    e = _strip_outer_parens(expr)
    if not e:
        return False
    head = split_top_level(e, "?")
    branches = split_top_level("?".join(head[1:]), ":") if len(head) > 1 else [e]
    for branch in branches:
        lit = leading_literal(branch)
        if lit is None:
            return False
        if lit and lit[0] not in "?&":
            return False
    return True


def split_concat(expr: str) -> list[tuple[str, str]] | None:
    """`"/documents/" + encodeURIComponent(id)` 를 템플릿과 같은 조각 목록으로.

    첫 조각이 문자열 리터럴이 아니면 경로를 앵커할 수 없으므로 None.
    """
    pieces = split_top_level(expr, "+")
    if len(pieces) < 2:
        return None
    parts: list[tuple[str, str]] = []
    for i, piece in enumerate(pieces):
        piece = piece.strip()
        lit = leading_literal(piece)
        is_pure_literal = lit is not None and _QUOTED_RE.match(_strip_outer_parens(piece))
        if i == 0 and not is_pure_literal:
            return None
        if is_pure_literal:
            parts.append(("lit", lit or ""))
        else:
            parts.append(("expr", piece))
    return parts


def resolve_call_url(
    expr: str,
    consts: dict[str, str],
    base_env_names: Sequence[str],
    allow_relative: bool = False,
    internal_hosts: Sequence[str] = (),
) -> tuple[str, str, str]:
    """호출 첫 인자 식 → (상태, 경로, 사유).

    상태는 "resolved" | "unresolved" | "external". 판정 불가한 것을 억지로 경로로 만들면
    결함 통계가 오염되므로 UNRESOLVED 로 따로 뺀다(결함 수에서 제외).

    변수 보간은 **완전한 한 세그먼트**일 때만 `{}` 로 바꾼다.
    `${BASE_URL}${path}` 처럼 세그먼트 중간에 붙는 변수는 경로 전체를 알 수 없다.

    allow_relative 는 base 를 스스로 붙이는 래퍼(`request("/health")`)용이다.
    그 경우 여기서는 상대 경로를 그대로 돌려주고 base 합성은 호출부가 한다.
    """
    def _finish(path: str) -> tuple[str, str, str]:
        if not path.startswith("/") and not allow_relative:
            return "unresolved", "", "상대 경로 — 기준 base 를 알 수 없음"
        return "resolved", normalize_route(path), ""

    parts = split_template(expr)
    if parts is None:
        q = _QUOTED_RE.match(expr.strip())
        if q:
            ext = external_host_of(q.group(2), internal_hosts)
            if ext:
                return "external", "", f"외부 오리진 {ext}"
            return _finish(url_path_of(q.group(2)))
        parts = split_concat(expr)
    if parts is None:
        return "unresolved", "", "URL 이 문자열/템플릿이 아님 (변수 또는 함수 결과)"

    pieces: list[str] = []
    for index, (kind, value) in enumerate(parts):
        if kind == "lit":
            pieces.append(value)
            continue
        at_front = "".join(pieces) == ""
        base = resolve_base_expr(value, base_env_names)
        if base is not None and at_front:
            pieces.append(base)
            continue
        if at_front and value.strip() in consts:
            pieces.append(consts[value.strip()])
            continue
        # 쿼리스트링만 만들 수 있는 보간은 경로에 영향이 없다. 단, 뒤에 경로
        # 세그먼트가 더 붙는다면 이야기가 달라지므로 남은 조각에 `/` 가 없을 때만.
        rest = "".join(v for k, v in parts[index + 1:] if k == "lit")
        if interpolation_is_query_only(value) and "/" not in rest:
            pieces.append("?")
            break
        pieces.append(_VAR_MARK)

    joined = "".join(pieces)
    if joined.startswith(_VAR_MARK):
        return "unresolved", "", "base 를 알 수 없는 변수로 시작"
    ext = external_host_of(joined, internal_hosts)
    if ext:
        return "external", "", f"외부 오리진 {ext}"
    path = url_path_of(joined)
    if _VAR_MARK not in path:
        return _finish(path)

    out: list[str] = []
    for seg in path.split("/"):
        if _VAR_MARK not in seg:
            out.append(seg)
        elif seg == _VAR_MARK:
            out.append("{}")
        else:
            return "unresolved", "", "변수가 경로 세그먼트 일부에만 붙어 경로를 확정할 수 없음"
    return _finish("/".join(out))


def classify_frontend_call(
    method: str,
    path: str,
    routes_by_method: dict[str, list[str]],
) -> tuple[str, str]:
    """(판정, 근거). 판정은 "OK" | "PATH_DRIFT" | "ROUTE_MISSING"."""
    method = method.upper()
    same = routes_by_method.get(method, [])
    for be in same:
        if route_matches(path, be):
            return "OK", be

    for other, paths in routes_by_method.items():
        if other == method:
            continue
        for be in paths:
            if route_matches(path, be):
                return "PATH_DRIFT", f"경로는 있으나 메서드가 {other} 다 ({be})"

    all_paths = [p for paths in routes_by_method.values() for p in paths]
    fe_segs = segments(path)
    for be in all_paths:
        be_segs = segments(be)
        if len(be_segs) > len(fe_segs) and be_segs[-len(fe_segs):] == fe_segs:
            missing = "/" + "/".join(be_segs[: len(be_segs) - len(fe_segs)])
            return "PATH_DRIFT", f"접두 {missing} 가 빠졌다 (실제 라우트 {be})"
        if len(fe_segs) > len(be_segs) and be_segs and fe_segs[-len(be_segs):] == be_segs:
            extra = "/" + "/".join(fe_segs[: len(fe_segs) - len(be_segs)])
            return "PATH_DRIFT", f"접두 {extra} 가 더 붙었다 (실제 라우트 {be})"

    # 프런트 `{}` 자리에 백엔드 리터럴이 오는 경우 — 판정 불가지 결함이 아니다.
    # 2026-09-16: `/api/v1/loops/{}/{}` 가 P0 로 잡혔는데 실제 action 값은
    # pause|resume|cancel 셋뿐이고 셋 다 백엔드에 있다. 변수 값을 모른다는
    # 이유로 없는 결함을 세면 P0 통계가 오염된다 — UNRESOLVED 로 뺀다.
    if "{}" in fe_segs:
        for be in all_paths:
            be_segs = segments(be)
            if len(be_segs) != len(fe_segs):
                continue
            if all(f == "{}" or f == b or b == "{}" for f, b in zip(fe_segs, be_segs)):
                return "VAR_SEGMENT", f"변수 세그먼트라 확정 불가 (후보 {be})"

    return "ROUTE_MISSING", "일치하는 라우트 없음"


_NEXT_DYNAMIC_RE = re.compile(r"^\[.*\]$")


def collect_next_route_handlers(fe_files: Sequence[str]) -> list[str]:
    """Next.js `app/**/route.ts` 가 스스로 서빙하는 URL 경로 목록.

    `src/app/runtime/dashboard-slot/route.ts` → `/runtime/dashboard-slot`.
    라우트 그룹 `(group)` 은 URL 에 나타나지 않고 동적 세그먼트 `[id]` 는 `{}` 다.
    이걸 모르면 프런트가 자기 자신에게 쏘는 호출이 백엔드 ROUTE_MISSING 으로 잡힌다.
    """
    out: list[str] = []
    for rel in fe_files:
        parts = rel.replace("\\", "/").split("/")
        if not parts or not parts[-1].startswith("route."):
            continue
        if "app" not in parts[:-1]:
            continue
        idx = len(parts[:-1]) - 1 - parts[:-1][::-1].index("app")
        segs: list[str] = []
        for seg in parts[idx + 1:-1]:
            if seg.startswith("(") and seg.endswith(")"):
                continue
            segs.append("{}" if _NEXT_DYNAMIC_RE.match(seg) else seg)
        out.append("/" + "/".join(segs))
    return sorted(set(out))


# ══════════════════════════════════════════════════════════════════════
# 수집 — 백엔드 (ast)
# ══════════════════════════════════════════════════════════════════════


@dataclass
class RouteDef:
    module: str
    method: str
    path: str          # 라우터 자체 prefix 까지 합성된 모듈-로컬 경로
    lineno: int


@dataclass
class IncludeCall:
    entrypoint: str
    target_expr: str   # include_router 의 첫 인자 식 (덤프용)
    target_module: str  # 해결된 모듈 경로 ("" 면 미해결)
    prefix: str
    lineno: int


@dataclass
class ModuleInfo:
    path: str
    router_vars: dict[str, str] = field(default_factory=dict)
    routes: list[RouteDef] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    parse_error: str = ""

    @property
    def defines_router(self) -> bool:
        return bool(self.router_vars)


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _expr_src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "<expr>"


def parse_router_module(rel_path: str, source: str) -> ModuleInfo:
    """한 모듈이 정의하는 APIRouter 와 그 라우트를 뽑는다.

    ast 로 읽기 때문에 독스트링 안의 `include_router`/`@router.get` 예시는
    애초에 노드가 되지 않는다 — grep 기반이라면 여기서부터 오탐이다.
    """
    info = ModuleInfo(path=rel_path)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        info.parse_error = f"{exc.__class__.__name__}: {exc}"
        return info

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or _callee_name(value.func) != "APIRouter":
            continue
        prefix_node = _kwarg(value, "prefix")
        prefix = _const_str(prefix_node) or ""
        if prefix_node is not None and prefix == "":
            info.unresolved.append({
                "kind": "ROUTER_PREFIX", "module": rel_path, "lineno": node.lineno,
                "detail": f"APIRouter prefix 가 상수가 아님: {_expr_src(prefix_node)}",
            })
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                info.router_vars[tgt.id] = prefix

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            method = dec.func.attr.lower()
            if method not in HTTP_METHODS:
                continue
            holder = dec.func.value
            if not isinstance(holder, ast.Name) or holder.id not in info.router_vars:
                continue
            raw = _const_str(dec.args[0]) if dec.args else None
            if raw is None:
                info.unresolved.append({
                    "kind": "ROUTE_PATH", "module": rel_path, "lineno": dec.lineno,
                    "detail": f"라우트 경로가 상수가 아님: {_expr_src(dec)}",
                })
                continue
            info.routes.append(RouteDef(
                module=rel_path,
                method=method.upper(),
                path=join_path(info.router_vars[holder.id], raw),
                lineno=dec.lineno,
            ))
    # ast.walk 는 너비 우선이라 소스 순서를 보장하지 않는다. FastAPI 는 모듈이
    # 실행되는 순서(=소스 순서)로 라우트를 등록하고, 같은 METHOD+경로가 두 번
    # 등록되면 **먼저 온 쪽이 이긴다**. ROUTE_SHADOWED 가 "어느 쪽이 죽었나" 를
    # 말하려면 이 순서가 실제 등록 순서와 같아야 한다.
    info.routes.sort(key=lambda r: r.lineno)
    return info


def parse_entrypoint(rel_path: str, source: str) -> tuple[list[IncludeCall], dict[str, str], str]:
    """엔트리포인트의 include_router 호출과 import 별칭→모듈 지도를 뽑는다."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], {}, f"{exc.__class__.__name__}: {exc}"

    alias_to_module: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "router" and alias.asname:
                    alias_to_module[alias.asname] = node.module
                else:
                    alias_to_module[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                alias_to_module[alias.asname or alias.name.split(".")[0]] = alias.name

    includes: list[IncludeCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node.func) != "include_router":
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name) and arg.attr == "router":
            key = arg.value.id
        elif isinstance(arg, ast.Name):
            key = arg.id
        else:
            key = ""
        includes.append(IncludeCall(
            entrypoint=rel_path,
            target_expr=_expr_src(arg),
            target_module=alias_to_module.get(key, ""),
            prefix=_const_str(_kwarg(node, "prefix")) or "",
            lineno=node.lineno,
        ))
    return includes, alias_to_module, ""


def dotted_to_relpath(root: Path, dotted: str) -> str:
    """`app.api.health` → `app/api/health.py` (존재할 때만)."""
    if not dotted:
        return ""
    parts = dotted.split(".")
    for cut in (len(parts), len(parts) - 1):
        if cut <= 0:
            continue
        cand = Path(*parts[:cut]).with_suffix(".py")
        if (root / cand).is_file():
            return cand.as_posix()
        pkg = Path(*parts[:cut]) / "__init__.py"
        if (root / pkg).is_file():
            return pkg.as_posix()
    return ""


def flatten_fstring(node: ast.JoinedStr) -> str:
    """f-string 을 상수 조각 + `{}` 자리표시자로 되돌린다.

    보간 부분을 그냥 **빼버리면** `FROM {table} e` 가 `FROM  e` 가 되어
    별칭 `e` 가 테이블로 잡힌다(실측: a/d/e/child 4건이 이 경로였다).
    자리표시자를 남기면 테이블 정규식이 아예 매칭되지 않고,
    그 자리는 UNRESOLVED 로 따로 샌다.
    """
    out = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out.append(value.value)
        else:
            out.append("{}")
    return "".join(out)


def collect_sql_tables(
    source: str, ignore: Sequence[str]
) -> tuple[set[str], list[dict]]:
    """`ast.Constant` 문자열에서만 테이블을 뽑는다. → (테이블, UNRESOLVED 사유)

    주석 밖의 진짜 문자열만 본다. f-string 은 조각이 아니라 전체를 한 번에 본다 —
    조각 단위로 보면 문장 키워드로 시작하는 조각이 없어 SQL 이 통째로 사라진다.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), []

    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for child in ast.walk(node):
                if child is not node:
                    inside_fstring.add(id(child))

    found: set[str] = set()
    notes: list[dict] = []

    def take(text: str, lineno: int) -> None:
        found.update(extract_sql_tables(text, ignore))
        for reason in sql_unresolved_reasons(text):
            notes.append({"lineno": lineno, "detail": reason})

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            take(flatten_fstring(node), node.lineno)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in inside_fstring
        ):
            take(node.value, node.lineno)
    return found, notes


# ══════════════════════════════════════════════════════════════════════
# 수집 — 프런트엔드 (TS/TSX)
# ══════════════════════════════════════════════════════════════════════

_CONST_RE = re.compile(r"^[^\S\n]*const\s+([A-Za-z_$][\w$]*)\s*=\s*(.+?);?[^\S\n]*$", re.M)
_METHOD_RE = re.compile(r"\bmethod\s*:\s*(['\"])([A-Za-z]+)\1")
_METHOD_ANY_RE = re.compile(r"\bmethod\s*:")


def collect_ts_consts(text: str, base_env_names: Sequence[str]) -> dict[str, str]:
    """`const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "https://…/api/v1"` 를 모은다.

    이 합성을 빼면 프런트 호출이 전부 ROUTE_MISSING 으로 터진다 —
    잡음 100% 인 리포트는 아무도 읽지 않으므로 규칙 자체가 죽는다.
    """
    consts: dict[str, str] = {}
    for m in _CONST_RE.finditer(text):
        base = resolve_base_expr(m.group(2), base_env_names)
        if base is not None:
            consts[m.group(1)] = base
    return consts


def split_call_args(text: str, open_idx: int) -> tuple[list[str], int]:
    """`(` 위치에서 시작해 최상위 콤마로 인자를 나눈다. → (인자들, 닫는 괄호 index)

    템플릿 리터럴·문자열·중첩 괄호를 세며 걷는다. 정규식으로 인자를 자르면
    `fetch(\\`${a},${b}\\`)` 같은 것에서 바로 어긋난다.
    """
    args: list[str] = []
    depth = 0
    buf = ""
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "([{":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                if buf.strip():
                    args.append(buf.strip())
                return args, i
        elif ch in "'\"`":
            quote = ch
            j = i + 1
            tdepth = 0
            while j < n:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if quote == "`" and c == "$" and j + 1 < n and text[j + 1] == "{":
                    tdepth += 1
                    j += 2
                    continue
                if quote == "`" and c == "}" and tdepth:
                    tdepth -= 1
                elif c == quote and not tdepth:
                    break
                j += 1
            buf += text[i:j + 1]
            i = j + 1
            continue
        elif ch == "," and depth == 1:
            args.append(buf.strip())
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    return args, -1


_IMPORT_NAMED_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]")


def parse_ts_imports(text: str) -> dict[str, str]:
    """`import { BASE_URL } from "./api"` → {"BASE_URL": "./api"}.

    페이지가 base 상수를 옆 모듈에서 가져오는 것이 이 대시보드의 관행이다.
    따라가지 않으면 가장 호출이 많은 파일이 통째로 UNRESOLVED 가 된다.
    """
    out: dict[str, str] = {}
    for m in _IMPORT_NAMED_RE.finditer(text):
        for raw in m.group(1).split(","):
            name = raw.strip()
            if not name:
                continue
            if " as " in name:
                name = name.split(" as ")[-1].strip()
            out[name] = m.group(2)
    return out


def resolve_ts_module(spec: str, from_rel: str, known: set[str],
                      aliases: dict[str, str]) -> str:
    """import 지정자를 저장소 상대 경로로. 찾지 못하면 빈 문자열."""
    if spec.startswith("."):
        base = (Path(from_rel).parent / spec).as_posix()
        base = os.path.normpath(base).replace(os.sep, "/")
    else:
        base = ""
        for prefix, target in aliases.items():
            if spec.startswith(prefix):
                base = target + spec[len(prefix):]
                break
        if not base:
            return ""
    for cand in (f"{base}.ts", f"{base}.tsx", f"{base}/index.ts", f"{base}/index.tsx", base):
        if cand in known:
            return cand
    return ""


_DECL_TAIL_RE = re.compile(r"\b(?:function|class)\s+$")


_CALL_HEAD_RE_CACHE: dict[str, re.Pattern] = {}


def find_ts_calls(text: str, call_names: Sequence[str]) -> list[dict]:
    """소스에서 `fetch(...)`/`request(...)` 등의 호출을 찾는다."""
    key = "|".join(sorted(call_names))
    pattern = _CALL_HEAD_RE_CACHE.get(key)
    if pattern is None:
        alts = "|".join(re.escape(n) for n in call_names)
        pattern = re.compile(rf"(?<![\w.$])(?:new\s+)?({alts})\s*(?:<[^<>()]*>\s*)?\(")
        _CALL_HEAD_RE_CACHE[key] = pattern

    calls: list[dict] = []
    for m in pattern.finditer(text):
        # `function request<T>(path: string, …)` 는 호출이 아니라 선언이다.
        if _DECL_TAIL_RE.search(text[max(0, m.start() - 24):m.start()]):
            continue
        open_idx = text.index("(", m.end() - 1)
        args, close_idx = split_call_args(text, open_idx)
        if close_idx < 0 or not args:
            continue
        calls.append({
            "callee": m.group(1),
            "url_expr": args[0],
            "options": args[1] if len(args) > 1 else "",
            "lineno": text.count("\n", 0, m.start()) + 1,
        })
    return calls


def method_of(options: str, default: str = "GET") -> tuple[str, bool]:
    """옵션 객체에서 HTTP 메서드. → (메서드, 확정여부)"""
    m = _METHOD_RE.search(options or "")
    if m:
        return m.group(2).upper(), True
    if _METHOD_ANY_RE.search(options or ""):
        return default, False
    return default, True


# ══════════════════════════════════════════════════════════════════════
# 결함 규칙 (순수 함수)
# ══════════════════════════════════════════════════════════════════════


def find_dup_modules(module_paths: Iterable[str], router_dirs: Sequence[str]) -> list[dict]:
    """같은 파일명이 두 라우터 디렉터리에 동시에 사는 경우.

    `app/api/chat.py` 와 `app/routers/chat.py` 가 이것이다. 이름이 같으면
    import 한 줄만 잘못 봐도 다른 모듈을 고치게 된다.
    """
    by_name: dict[str, list[str]] = {}
    dirs = set(router_dirs)
    for path in module_paths:
        parent = os.path.dirname(path)
        name = os.path.basename(path)
        if parent not in dirs or name == "__init__.py":
            continue
        by_name.setdefault(name, []).append(path)

    findings = []
    for name, paths in sorted(by_name.items()):
        if len(paths) < 2:
            continue
        findings.append({
            "rule": "DUP_MODULE",
            "key": name,
            "modules": sorted(paths),
            "detail": f"모듈명 `{name}` 이 {len(paths)}개 디렉터리에 중복 존재: "
                      + ", ".join(f"`{p}`" for p in sorted(paths)),
        })
    return findings


def find_double_mounts(mounted_routes: Iterable[dict], api_roots: Sequence[str]) -> list[dict]:
    """같은 라우터 모듈이 한 엔트리포인트에 반복 마운트된 경우.

    서로 다른 모듈이 상위 네임스페이스를 공유하는 것은 FastAPI의 정상적인
    라우터 분할 방식이다. 그것을 중복 마운트로 판정해 include_router를
    제거하면 서로 다른 엔드포인트가 통째로 사라진다.
    """
    by_ns: dict[tuple[str, str], dict] = {}
    for r in mounted_routes:
        ns = namespace_key(r["full_path"], api_roots)
        slot = by_ns.setdefault((r["entrypoint"], ns), {"owners": {}, "routes": []})
        slot["owners"].setdefault(r["module"], 0)
        slot["owners"][r["module"]] += 1
        slot["routes"].append(r)

    findings = []
    for (entrypoint, ns), slot in sorted(by_ns.items()):
        owners = slot["owners"]
        duplicate_routes: dict[tuple[str, str, str], int] = {}
        for r in slot["routes"]:
            key = (r["module"], r["method"], normalize_route(r["full_path"]))
            duplicate_routes[key] = duplicate_routes.get(key, 0) + 1
        duplicated_modules = sorted({
            module for (module, _method, _path), count in duplicate_routes.items()
            if count > 1
        })
        if not duplicated_modules:
            continue
        seen: dict[tuple[str, str], set[str]] = {}
        for r in slot["routes"]:
            seen.setdefault((r["method"], normalize_route(r["full_path"])), set()).add(r["module"])
        exact = sorted(
            (
                {"method": method, "path": path, "modules": sorted(mods)}
                for (method, path), mods in seen.items()
                if len(mods) > 1 or duplicate_routes.get((next(iter(mods)), method, path), 0) > 1
            ),
            key=lambda d: (d["path"], d["method"]),
        )
        owner_txt = ", ".join(f"`{m}`({c}개 라우트)" for m, c in sorted(owners.items()))
        findings.append({
            "rule": "DOUBLE_MOUNT",
            "key": ns,
            "entrypoint": entrypoint,
            "namespace": ns,
            "owners": duplicated_modules,
            "owner_route_counts": dict(sorted(owners.items())),
            "exact_conflicts": exact,
            "detail": (
                f"네임스페이스 `{ns}` 에서 라우터 모듈이 중복 마운트됨 — "
                f"{', '.join(f'`{m}`' for m in duplicated_modules)}. "
                f"등록된 소유자: {owner_txt}; 중복 METHOD+경로 {len(exact)}건"
            ),
        })
    return findings


def find_route_shadowed(mounted_routes: Iterable[dict]) -> list[dict]:
    """같은 엔트리포인트에 METHOD+경로가 두 번 등록된 경우 — 뒤엣것은 죽는다.

    FastAPI 는 매칭되는 **첫 번째** 라우트를 쓴다. 같은 METHOD+경로를 두 번
    등록해도 예외가 나지 않고 조용히 뒤엣것이 무시된다.

    DOUBLE_MOUNT 로는 이걸 못 잡는다. 그 규칙은 네임스페이스 소유자가 2개
    이상일 때만 돌고 `exact_conflicts` 도 서로 **다른 모듈** 일 때만 세기
    때문에, 한 모듈이 같은 경로를 두 번 쓴 경우는 소유자가 1개라 아예 검사
    대상에서 빠진다. 2026-09-16 `app/api/ops.py` 의
    `GET /api/v1/ops/codex-usage` 가 그랬다(2677, 2877) — 마스킹·30초 캐시가
    붙은 나중 구현이 등록조차 되지 않은 채 죽어 있었고, 함수 이름이 달라
    ruff F811 도 잡지 못했다.

    엔트리포인트별로 센다. `app/main.py` 와 `app/yeoljeong_main.py` 는 서로
    다른 ASGI 앱이므로 같은 경로를 가져도 충돌이 아니다.
    """
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    for r in mounted_routes:
        key = (r["entrypoint"], r["method"], normalize_route(r["full_path"]))
        by_key.setdefault(key, []).append(r)

    findings = []
    for (entrypoint, method, path), routes in sorted(by_key.items()):
        if len(routes) < 2:
            continue
        # mounted_routes 는 include_router 순서 → 모듈 내 소스 순서로 쌓인다.
        # 따라서 첫 번째가 FastAPI 가 실제로 쓰는 라우트다.
        winner, *shadowed = routes
        where = ", ".join(f"`{r['module']}:{r['lineno']}`" for r in shadowed)
        findings.append({
            "rule": "ROUTE_SHADOWED",
            "key": f"{entrypoint} {method} {path}",
            "entrypoint": entrypoint,
            "method": method,
            "path": path,
            "winner": {"module": winner["module"], "lineno": winner["lineno"]},
            "shadowed": [
                {"module": r["module"], "lineno": r["lineno"]} for r in shadowed
            ],
            "same_module": len({r["module"] for r in routes}) == 1,
            "detail": (
                f"`{entrypoint}` 에 `{method} {path}` 가 {len(routes)}번 등록됐다 — "
                f"`{winner['module']}:{winner['lineno']}` 만 살고 {where} 는 "
                f"도달 불가다(FastAPI 는 먼저 등록된 라우트를 쓴다). "
                f"예외가 나지 않으므로 HTTP 로는 보이지 않는다"
            ),
        })
    return findings


def find_orphan_routers(
    router_modules: Iterable[str],
    mounted_by: dict[str, list[str]],
) -> list[dict]:
    """APIRouter 를 정의했는데 어떤 엔트리포인트에도 마운트되지 않은 모듈."""
    findings = []
    for module in sorted(router_modules):
        if mounted_by.get(module):
            continue
        findings.append({
            "rule": "ORPHAN_ROUTER",
            "key": module,
            "module": module,
            "detail": f"`{module}` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 "
                      f"include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다",
        })
    return findings


def find_table_no_model(
    referenced: dict[str, list[str]],
    defined: set[str],
    known_external: Sequence[str] = (),
) -> list[dict]:
    """앱 코드가 쓰는데 어떤 마이그레이션·스크립트도 만들지 않는 테이블.

    AADS 에는 ORM 모델이 없다(`__tablename__` 0건 — 전부 raw SQL). 그래서
    "모델 없음" 은 곧 "스키마 정의를 코드에서 찾을 수 없음" 이다. 배포된 DB 를
    직접 고쳐 만든 테이블이 여기 걸리는데, 그것이야말로 재현 불가능한 스키마다.
    """
    external = {t.lower() for t in known_external}
    findings = []
    for table, users in sorted(referenced.items()):
        if table in defined or table in external:
            continue
        findings.append({
            "rule": "TABLE_NO_MODEL",
            "key": table,
            "table": table,
            "users": sorted(users)[:8],
            "user_count": len(users),
            "detail": f"테이블 `{table}` 을 {len(users)}개 파일이 참조하지만 "
                      f"CREATE TABLE 정의를 코드에서 찾을 수 없다 "
                      f"(예: {', '.join('`' + u + '`' for u in sorted(users)[:3])})",
        })
    return findings


def find_stale_backups(paths: Iterable[str], patterns: Sequence[str]) -> list[dict]:
    """낡은 사본. 지금 도는 코드와 구분이 안 되는 파일은 그 자체로 오독의 씨앗이다."""
    findings = []
    for path in sorted(paths):
        if not is_backup_path(path, patterns):
            continue
        findings.append({
            "rule": "STALE_BACKUP",
            "key": path,
            "path": path,
            "detail": f"`{path}` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 "
                      f"검색·grep 결과에 섞여 낡은 코드를 읽게 된다",
        })
    return findings


# ══════════════════════════════════════════════════════════════════════
# 스캔 구동
# ══════════════════════════════════════════════════════════════════════


_IGNORED_CACHE: dict[str, set[str]] = {}


def git_ignored_files(repo_root: Path) -> set[str]:
    """git 이 무시하는 파일 절대경로 집합. 저장소가 아니면 빈 집합.

    같은 커밋이 서버마다 다른 결과를 내면 baseline 은 의미가 없다.
    2026-09-16 실측: 116 서버 워킹트리에는 gitignore 된 `.bak` 파일이 120개 있어
    STALE_BACKUP 이 34(깨끗한 체크아웃) → 113 으로 부풀었다. 스캔 결과는
    **커밋 내용**만으로 결정돼야 한다.
    """
    key = str(repo_root)
    if key in _IGNORED_CACHE:
        return _IGNORED_CACHE[key]
    found: set[str] = set()
    try:
        proc = subprocess.run(
            # core.quotePath=false 가 없으면 git 은 비ASCII 경로를
            # `"app/static/reports/\355\225\234\352\270\200.html.bak_aads"` 처럼
            # 이스케이프해 내놓는다. 그 문자열은 실제 경로와 한 글자도 맞지 않아
            # **한글 이름 파일만 무시 목록에서 새어 나간다.**
            # 2026-09-16 실측: 워킹트리와 깨끗한 체크아웃의 STALE_BACKUP 이
            # 정확히 3 벌어졌고, 그 3개가 전부 한글 파일명의 `.bak_aads` 였다.
            # 게이트가 "커밋 내용으로만 결정된다" 는 전제를 이 한 줄이 깨고 있었다.
            ["git", "-C", str(repo_root), "-c", "core.quotePath=false",
             "ls-files", "--others", "--ignored", "--exclude-standard"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            for line in proc.stdout.split("\n"):
                if line.strip():
                    found.add((repo_root / line.strip()).as_posix())
    except (OSError, subprocess.SubprocessError):
        pass
    _IGNORED_CACHE[key] = found
    return found


def iter_files(root: Path, rel_roots: Sequence[str], suffixes: Sequence[str],
               exclude_dirs: Sequence[str]) -> list[str]:
    excluded = set(exclude_dirs)
    ignored = git_ignored_files(root)
    out: list[str] = []

    def key_for(p: Path) -> str:
        """저장소 밖 경로(예: 별도 저장소인 대시보드)도 다룬다.

        2026-09-16: 대시보드 소스가 aads-server 안에 13파일짜리 유령 미러로
        추적되고 있었고, 그 미러를 지우자 프런트 스캔이 0파일이 되어 계약
        검사(PATH_DRIFT/ROUTE_MISSING)가 조용히 꺼졌다. 정본은 별도 저장소
        `/root/aads/aads-dashboard` 한 곳이므로 root 밖을 가리킬 수 있어야 한다.
        root 밖이면 절대경로를 키로 쓴다 — `root / "/abs"` 는 pathlib 에서
        `/abs` 가 되므로 이후 읽기 경로가 그대로 성립한다.
        """
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return p.resolve().as_posix()

    for rel in rel_roots:
        base = (root / rel).resolve() if rel.startswith("..") else root / rel
        if not base.exists():
            continue
        if base.is_file():
            if base.as_posix() not in ignored:
                out.append(key_for(base))
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in excluded]
            for fn in filenames:
                if suffixes and not any(fn.endswith(s) for s in suffixes):
                    continue
                p = Path(dirpath) / fn
                if p.as_posix() in ignored:
                    continue
                out.append(key_for(p))
    return sorted(set(out))


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


class Scan:
    """한 번의 스캔 결과. 규칙 판정은 순수 함수에 맡기고 여기서는 모으기만 한다."""

    def __init__(self, root: Path, rules: dict[str, Any]):
        self.root = root
        self.rules = rules
        self.warnings: list[str] = []
        self.unresolved: list[dict] = []
        self.findings: list[dict] = []
        self.stats: dict[str, Any] = {}
        self.nodes: list[dict] = []
        self.edges: list[dict] = []

    # ── 백엔드 ──────────────────────────────────────────────────────
    def scan_backend(self) -> None:
        cfg = self.rules["scan"]
        exclude = cfg["exclude_dir_names"]
        self.py_files = iter_files(self.root, cfg["app_roots"], [".py"], exclude)
        self.router_dir_files = iter_files(self.root, cfg["router_dirs"], [".py"], exclude)

        entry_cfg = self.rules["entrypoints"]
        primary = entry_cfg["primary"]
        secondaries = list(entry_cfg.get("secondary") or [])
        entrypoints = [primary, *secondaries]

        self.modules: dict[str, ModuleInfo] = {}
        for rel in self.py_files:
            if rel in entrypoints:
                continue
            info = parse_router_module(rel, read_text(self.root / rel))
            if info.parse_error:
                self.warnings.append(f"파싱 실패 {rel}: {info.parse_error}")
                continue
            if info.defines_router:
                self.modules[rel] = info
                self.unresolved.extend(info.unresolved)

        self.includes: list[IncludeCall] = []
        self.entrypoints_present: list[str] = []
        for rel in entrypoints:
            path = self.root / rel
            if not path.is_file():
                self.warnings.append(f"엔트리포인트 없음: {rel}")
                continue
            self.entrypoints_present.append(rel)
            incs, _aliases, err = parse_entrypoint(rel, read_text(path))
            if err:
                self.warnings.append(f"엔트리포인트 파싱 실패 {rel}: {err}")
                continue
            for inc in incs:
                inc.target_module = dotted_to_relpath(self.root, inc.target_module)
                if not inc.target_module:
                    self.unresolved.append({
                        "kind": "INCLUDE_TARGET", "module": rel, "lineno": inc.lineno,
                        "detail": f"include_router 대상 모듈을 해석하지 못함: {inc.target_expr}",
                    })
                self.includes.append(inc)

        self.mounted_by: dict[str, list[str]] = {}
        self.mounted_routes: list[dict] = []
        api_roots = sorted(
            {inc.prefix for inc in self.includes if inc.prefix},
            key=len, reverse=True,
        )
        self.api_roots = api_roots
        for inc in self.includes:
            if not inc.target_module:
                continue
            self.mounted_by.setdefault(inc.target_module, []).append(inc.entrypoint)
            info = self.modules.get(inc.target_module)
            if info is None:
                continue
            for route in info.routes:
                self.mounted_routes.append({
                    "entrypoint": inc.entrypoint,
                    "module": route.module,
                    "method": route.method,
                    "mount_prefix": inc.prefix,
                    "full_path": normalize_route(join_path(inc.prefix, route.path)),
                    "lineno": route.lineno,
                })

        self.primary_routes = [r for r in self.mounted_routes if r["entrypoint"] == primary]
        self.routes_by_method: dict[str, list[str]] = {}
        for r in self.primary_routes:
            self.routes_by_method.setdefault(r["method"], []).append(r["full_path"])

        # SQL
        ignore = self.rules["sql"]["ignore_tables"]
        self.table_refs: dict[str, list[str]] = {}
        for rel in self.py_files:
            tables, notes = collect_sql_tables(read_text(self.root / rel), ignore)
            for table in tables:
                self.table_refs.setdefault(table, []).append(rel)
            for note in notes:
                self.unresolved.append({
                    "kind": "SQL_TABLE", "module": rel,
                    "lineno": note["lineno"], "detail": note["detail"],
                })

        schema_files = iter_files(
            self.root, cfg["schema_roots"], [".sql", ".py", ".sh"], exclude
        ) + self.py_files
        self.defined_tables: set[str] = set()
        for rel in sorted(set(schema_files)):
            self.defined_tables |= extract_ddl_tables(read_text(self.root / rel))

        self.backup_candidates = iter_files(self.root, cfg["backup_roots"], [], exclude)

    # ── 프런트엔드 ──────────────────────────────────────────────────
    def scan_frontend(self) -> None:
        """두 번 읽는다. 1패스로 파일별 base 상수를 모으고, 2패스에서 import 를
        따라간 뒤 호출을 해석한다. 한 번에 하면 `import { BASE_URL }` 이
        아직 읽지 않은 파일을 가리킬 때 순서에 따라 결과가 달라진다."""
        fe = self.rules["frontend"]
        self.fe_files = iter_files(
            self.root, fe["roots"], fe["extensions"], self.rules["scan"]["exclude_dir_names"]
        )
        helpers: dict[str, str] = dict(fe.get("path_helpers") or {})
        aliases: dict[str, str] = dict(fe.get("path_aliases") or {})
        call_names = list(fe["call_names"]) + list(helpers)
        known = set(self.fe_files)
        self.external_calls: list[dict] = []
        self.fe_self_routes = collect_next_route_handlers(self.fe_files)

        texts: dict[str, str] = {}
        consts_by_file: dict[str, dict[str, str]] = {}
        imports_by_file: dict[str, dict[str, str]] = {}
        for rel in self.fe_files:
            text = read_text(self.root / rel)
            if not text:
                continue
            texts[rel] = text
            consts_by_file[rel] = collect_ts_consts(text, fe["base_env_names"])
            imports_by_file[rel] = parse_ts_imports(text)

        self.fe_calls: list[dict] = []
        for rel, text in texts.items():
            consts = dict(consts_by_file.get(rel, {}))
            missing_imports: dict[str, str] = {}
            for name, spec in imports_by_file.get(rel, {}).items():
                if name in consts:
                    continue
                target = resolve_ts_module(spec, rel, known, aliases)
                if target and name in consts_by_file.get(target, {}):
                    consts[name] = consts_by_file[target][name]
                elif name.upper() == name:
                    missing_imports[name] = spec

            for call in find_ts_calls(text, call_names):
                is_helper = call["callee"] in helpers
                status, path, why = resolve_call_url(
                    call["url_expr"], consts, fe["base_env_names"], allow_relative=is_helper,
                    internal_hosts=fe.get("internal_hosts") or [],
                )
                if status == "external":
                    self.external_calls.append({
                        "module": rel, "lineno": call["lineno"], "detail": why,
                    })
                    continue
                if status == "resolved" and is_helper:
                    base = consts.get(helpers[call["callee"]], fe.get("fallback_base") or "")
                    path = normalize_route(join_path(base, path))
                if status != "resolved":
                    extra = ""
                    for name, spec in missing_imports.items():
                        if f"${{{name}}}" in call["url_expr"]:
                            extra = (f" — `{name}` 은 `{spec}` 에서 import 되는데 "
                                     f"그 모듈이 스캔 범위 안에 없다")
                            break
                    self.unresolved.append({
                        "kind": "FRONTEND_URL", "module": rel, "lineno": call["lineno"],
                        "detail": f"{call['callee']}() URL 해석 불가 — {why}"
                                  f"{extra}: {call['url_expr'][:120]}",
                    })
                    continue
                method, certain = method_of(call["options"])
                if not certain:
                    self.unresolved.append({
                        "kind": "FRONTEND_METHOD", "module": rel, "lineno": call["lineno"],
                        "detail": f"HTTP 메서드가 상수가 아님: {path}",
                    })
                    continue
                self.fe_calls.append({
                    "file": rel, "lineno": call["lineno"], "callee": call["callee"],
                    "expr": call["url_expr"][:160], "method": method, "path": path,
                })

    # ── 규칙 적용 ──────────────────────────────────────────────────
    def apply_rules(self) -> None:
        sev = self.rules["severity"]
        raw: list[dict] = []
        raw += find_dup_modules(self.router_dir_files, self.rules["scan"]["router_dirs"])
        raw += find_double_mounts(self.mounted_routes, self.api_roots)
        raw += find_route_shadowed(self.mounted_routes)
        raw += find_orphan_routers(self.modules, self.mounted_by)
        raw += find_table_no_model(
            self.table_refs, self.defined_tables, self.rules["sql"]["known_external_tables"]
        )
        raw += find_stale_backups(self.backup_candidates, self.rules["backup_patterns"])

        for call in self.fe_calls:
            verdict, why = classify_frontend_call(call["method"], call["path"], self.routes_by_method)
            if verdict == "OK":
                continue
            # 프런트가 스스로 서빙하는 route handler(Next.js app/**/route.ts)는
            # 백엔드 계약 대상이 아니다. 2026-09-16 `/runtime/dashboard-slot`
            # 한 건이 이 이유로 P0 에 섞여 있었다(실제 라우트는 대시보드 안에 있다).
            if any(route_matches(call["path"], self_route)
                   for self_route in self.fe_self_routes):
                continue
            if verdict == "VAR_SEGMENT":
                self.unresolved.append({
                    "kind": "FRONTEND_VAR_SEGMENT", "module": call["file"],
                    "lineno": call["lineno"],
                    "detail": f"{call['method']} {call['path']} — {why}",
                })
                continue
            raw.append({
                "rule": verdict,
                "key": f"{call['file']}:{call['lineno']}",
                "file": call["file"], "lineno": call["lineno"],
                "method": call["method"], "path": call["path"],
                "detail": f"`{call['file']}:{call['lineno']}` {call['method']} "
                          f"`{call['path']}` — {why}",
            })

        for f in raw:
            f["severity"] = sev.get(f["rule"], "P2")
        self.findings = sorted(raw, key=lambda f: (f["severity"], f["rule"], str(f.get("key", ""))))

    # ── 그래프 ─────────────────────────────────────────────────────
    def build_graph(self) -> None:
        nodes: dict[str, dict] = {}
        edges: list[dict] = []

        def add(node_id: str, kind: str, **extra) -> None:
            if node_id not in nodes:
                nodes[node_id] = {"id": node_id, "kind": kind, **extra}

        for rel in self.entrypoints_present:
            add(rel, "entrypoint")
        for rel, info in self.modules.items():
            add(rel, "router_module", routes=len(info.routes))
        for inc in self.includes:
            if not inc.target_module:
                continue
            edges.append({
                "from": inc.entrypoint, "to": inc.target_module,
                "kind": "mounts", "prefix": inc.prefix, "lineno": inc.lineno,
            })
        ns_owners: dict[str, set[str]] = {}
        for r in self.mounted_routes:
            ns = namespace_key(r["full_path"], self.api_roots)
            add(f"ns:{ns}", "namespace", path=ns)
            ns_owners.setdefault(ns, set()).add(r["module"])
        for ns, owners in ns_owners.items():
            for owner in sorted(owners):
                edges.append({"from": owner, "to": f"ns:{ns}", "kind": "owns"})
        for rel in self.fe_files:
            add(rel, "frontend")
        for call in self.fe_calls:
            ns = namespace_key(call["path"], self.api_roots)
            add(f"ns:{ns}", "namespace", path=ns)
            edges.append({
                "from": call["file"], "to": f"ns:{ns}", "kind": "calls",
                "method": call["method"], "path": call["path"], "lineno": call["lineno"],
            })
        for table, users in sorted(self.table_refs.items()):
            add(f"table:{table}", "table",
                defined=table in self.defined_tables, users=len(users))
            for user in sorted(users):
                add(user, "module")
                edges.append({"from": user, "to": f"table:{table}", "kind": "queries"})

        self.nodes = sorted(nodes.values(), key=lambda n: (n["kind"], n["id"]))
        self.edges = edges
        self.ns_owners = {k: sorted(v) for k, v in sorted(ns_owners.items())}


# ══════════════════════════════════════════════════════════════════════
# 출력
# ══════════════════════════════════════════════════════════════════════

RULE_ORDER = ["DUP_MODULE", "DOUBLE_MOUNT", "ROUTE_SHADOWED", "ORPHAN_ROUTER",
              "TABLE_NO_MODEL", "PATH_DRIFT", "ROUTE_MISSING", "STALE_BACKUP"]


def counts_by_rule(findings: Sequence[dict]) -> dict[str, int]:
    counts = {rule: 0 for rule in RULE_ORDER}
    for f in findings:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    return counts


def _mermaid_id(raw: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9]", "_", raw)


def render_mermaid(scan: Scan) -> str:
    """네임스페이스 단위 그래프. 모듈 80개를 그대로 그리면 사람이 못 읽는다."""
    limit = int(scan.rules["output"].get("mermaid_max_namespaces", 40))
    # classDef 를 먼저 선언한다 — 뒤에 두면 렌더러에 따라 class 지정이 무시된다.
    lines = ["graph LR", "  classDef dbl fill:#ffd7d7,stroke:#c00,stroke-width:2px;"]
    dup_ns = {f["namespace"] for f in scan.findings if f["rule"] == "DOUBLE_MOUNT"}
    ordered = sorted(
        scan.ns_owners.items(), key=lambda kv: (-len(kv[1]), kv[0])
    )
    shown = ordered[:limit]

    for entry in scan.entrypoints_present:
        lines.append(f'  {_mermaid_id(entry)}["{entry}"]')

    for ns, owners in shown:
        nid = _mermaid_id("ns" + ns)
        marker = " ⚠2+" if ns in dup_ns else ""
        lines.append(f'  {nid}(["{ns}{marker}"])')
        for owner in owners:
            oid = _mermaid_id(owner)
            lines.append(f'  {oid}["{owner}"]')
            lines.append(f"  {oid} --> {nid}")
        if ns in dup_ns:
            lines.append(f"  class {nid} dbl;")

    mounts = {(e["from"], e["to"]) for e in scan.edges if e["kind"] == "mounts"}
    shown_modules = {m for _ns, owners in shown for m in owners}
    for src, dst in sorted(mounts):
        if dst in shown_modules:
            lines.append(f"  {_mermaid_id(src)} --> {_mermaid_id(dst)}")

    fe_edges: dict[tuple[str, str], int] = {}
    for e in scan.edges:
        if e["kind"] == "calls":
            fe_edges[(e["from"], e["to"][3:])] = fe_edges.get((e["from"], e["to"][3:]), 0) + 1
    shown_ns = {ns for ns, _ in shown}
    for (fe, ns), n in sorted(fe_edges.items()):
        if ns not in shown_ns:
            continue
        lines.append(f'  {_mermaid_id(fe)}[/"{fe}"/]')
        lines.append(f"  {_mermaid_id(fe)} -. {n}건 .-> {_mermaid_id('ns' + ns)}")

    if len(ordered) > limit:
        lines.append(f"  %% 네임스페이스 {len(ordered)}개 중 상위 {limit}개만 표시")
    return "\n".join(lines)


def render_findings_md(scan: Scan) -> str:
    counts = counts_by_rule(scan.findings)
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    out = [
        "# AAG L1/L2 — AADS 아키텍처 결함 리포트",
        "",
        f"생성 {now} · 결함 {len(scan.findings)}건 · 판정 불가(UNRESOLVED) {len(scan.unresolved)}건",
        "",
        "UNRESOLVED 는 **결함 수에 포함하지 않는다**. 판정 못 한 것을 결함으로 세면",
        "숫자가 부풀고, 부풀린 숫자는 아무도 손대지 않아 규칙 전체가 무시된다.",
        "",
        "## 스캔 범위",
        "",
        "| 대상 | 수 |",
        "|---|---|",
        f"| 앱 파이썬 파일 | {len(scan.py_files)} |",
        f"| 라우터 디렉터리 파일 | {len(scan.router_dir_files)} |",
        f"| APIRouter 정의 모듈 | {len(scan.modules)} |",
        f"| include_router 호출 | {len(scan.includes)} |",
        f"| 마운트된 라우트 | {len(scan.mounted_routes)} |",
        f"| 네임스페이스 | {len(scan.ns_owners)} |",
        f"| 프런트 파일 | {len(scan.fe_files)} |",
        f"| 해석된 프런트 호출 | {len(scan.fe_calls)} |",
        f"| SQL 참조 테이블 | {len(scan.table_refs)} |",
        f"| 그래프 노드/엣지 | {len(scan.nodes)} / {len(scan.edges)} |",
        "",
        "## 규칙별 건수",
        "",
        "| 규칙 | 심각도 | 건수 |",
        "|---|---|---|",
    ]
    sev = scan.rules["severity"]
    for rule in RULE_ORDER:
        out.append(f"| `{rule}` | {sev.get(rule, 'P2')} | {counts.get(rule, 0)} |")
    out += ["", f"| **합계** | | **{len(scan.findings)}** |", ""]

    for rule in RULE_ORDER:
        items = [f for f in scan.findings if f["rule"] == rule]
        if not items:
            continue
        out += [f"## {rule} ({len(items)}건)", ""]
        for f in items:
            out.append(f"- [{f['severity']}] {f['detail']}")
            for ex in f.get("exact_conflicts", []):
                out.append(f"    - exact 충돌: {ex['method']} `{ex['path']}` ← {', '.join(ex['modules'])}")
        out.append("")

    if scan.unresolved:
        out += ["## UNRESOLVED (결함 아님 — 판정 불가)", ""]
        by_kind: dict[str, list[dict]] = {}
        for u in scan.unresolved:
            by_kind.setdefault(u["kind"], []).append(u)
        for kind, items in sorted(by_kind.items()):
            out.append(f"### {kind} ({len(items)}건)")
            for u in items[:20]:
                out.append(f"- `{u['module']}:{u['lineno']}` {u['detail']}")
            if len(items) > 20:
                out.append(f"- … 외 {len(items) - 20}건")
            out.append("")

    if scan.warnings:
        out += ["## 경고", ""] + [f"- {w}" for w in scan.warnings] + [""]
    return "\n".join(out)


def build_baseline(scan: Scan) -> dict[str, Any]:
    """다음 스캔이 이 수치를 넘으면 부채가 늘어난 것이다."""
    return {
        "generated_at": datetime.now(KST).isoformat(),
        "note": (
            "AAG L1/L2 고정선. 이 숫자는 '허용치'가 아니라 '현재 빚'이다. "
            "줄이는 방향으로만 갱신한다. 늘려서 통과시키려면 왜 늘었는지를 "
            "먼저 적어라."
        ),
        "scope": {
            "app_py_files": len(scan.py_files),
            "router_dir_files": len(scan.router_dir_files),
            "router_modules": len(scan.modules),
            "include_router_calls": len(scan.includes),
            "mounted_routes": len(scan.mounted_routes),
            "namespaces": len(scan.ns_owners),
            "frontend_files": len(scan.fe_files),
            "frontend_calls_resolved": len(scan.fe_calls),
            "sql_tables_referenced": len(scan.table_refs),
            "graph_nodes": len(scan.nodes),
            "graph_edges": len(scan.edges),
        },
        "findings_by_rule": counts_by_rule(scan.findings),
        "findings_total": len(scan.findings),
        "unresolved_total": len(scan.unresolved),
        "unresolved_by_kind": {
            kind: sum(1 for u in scan.unresolved if u["kind"] == kind)
            for kind in sorted({u["kind"] for u in scan.unresolved})
        },
    }


def compare_baseline(current: dict, saved: dict) -> list[str]:
    """고정선 대비 증가분. 줄어든 것은 지적하지 않는다."""
    regressions = []
    for rule, count in current["findings_by_rule"].items():
        before = saved.get("findings_by_rule", {}).get(rule)
        if before is None:
            regressions.append(f"{rule}: 고정선에 없던 규칙 (현재 {count}건)")
        elif count > before:
            regressions.append(f"{rule}: {before} → {count} (+{count - before})")
    return regressions


# ══════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════


def zero_target_guard(scan: Scan) -> list[str]:
    """스캔 대상이 없으면 무엇이 비었는지 말한다.

    "위반 0건" 과 "아무것도 못 읽었다" 는 결과 화면이 똑같이 생겼다.
    같은 종료코드로 내보내면 게이트가 죽은 날에도 초록불이 켜진다.
    """
    empty = []
    if not scan.py_files:
        empty.append("app_roots 아래 .py 파일 0개")
    if not scan.router_dir_files:
        empty.append("router_dirs 아래 .py 파일 0개")
    if not scan.modules:
        empty.append("APIRouter 를 정의하는 모듈 0개")
    if not scan.entrypoints_present:
        empty.append("엔트리포인트 파일 0개")
    # 프런트 계약 검사는 "설정했는데 0파일" 이 가장 위험하다 — 전체 스캔은
    # 백엔드 파일이 있으니 통과하고, PATH_DRIFT/ROUTE_MISSING 만 영원히 0 이 된다.
    # 2026-09-16 유령 미러를 지운 직후 실제로 이 상태가 됐다.
    fe_roots = (getattr(scan, "rules", None) or {}).get("frontend", {}).get("roots")
    if fe_roots and not getattr(scan, "fe_files", None):
        empty.append("frontend.roots 가 설정됐는데 대상 파일 0개 (경로가 실재하는지 확인하라)")
    return empty


def push_snapshot(payload: dict[str, Any]) -> None:
    """Best-effort delivery; scanner status must never depend on the API."""
    base = os.getenv("AADS_API_BASE", "http://127.0.0.1:8000").rstrip("/")
    headers = {"Content-Type": "application/json"}
    tenant_id = os.getenv("AADS_TENANT_ID", "").strip()
    if tenant_id:
        headers["X-Tenant-ID"] = tenant_id
    request = urllib.request.Request(
        f"{base}/api/v1/aag/snapshot",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 — configured internal API
            response.read()
    except Exception as exc:  # noqa: BLE001 — explicitly best-effort
        print(f"[AAG] 경고: 스냅샷 전송 실패 — {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="AAG L1/L2 AADS 아키텍처 추출기")
    ap.add_argument("--root", default=str(here.parents[1]), help="저장소 루트")
    ap.add_argument("--rules", default=str(here / "rules_aads.yml"), help="규칙 파일")
    ap.add_argument("--out-dir", help="산출물 디렉터리 (기본: 규칙의 output 경로)")
    ap.add_argument("--json", action="store_true", help="요약을 JSON 으로 표준출력")
    ap.add_argument("--write-baseline", action="store_true",
                    help="현재 수치를 baseline_aads.json 으로 고정")
    ap.add_argument("--baseline", default=str(here / "baseline_aads.json"))
    ap.add_argument("--check-baseline", action="store_true",
                    help="고정선보다 결함이 늘었으면 종료코드 1")
    ap.add_argument("--no-write", action="store_true", help="리포트 파일을 쓰지 않는다")
    ap.add_argument("--push-snapshot", action="store_true", help="스캔 요약과 결함을 AADS API로 전송")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: 루트가 디렉터리가 아님 — {root}", file=sys.stderr)
        return 2

    rules, warnings = load_rules(Path(args.rules))
    scan = Scan(root, rules)
    scan.warnings.extend(warnings)
    try:
        scan.scan_backend()
        scan.scan_frontend()
    except Exception as exc:  # noqa: BLE001 — 실행 불가와 결함 0건을 반드시 구분한다
        print(f"ERROR: 스캔 실패 — {exc}", file=sys.stderr)
        return 2

    empty = zero_target_guard(scan)
    if empty:
        print("ERROR: 스캔 대상이 비었습니다 — " + "; ".join(empty), file=sys.stderr)
        print("       '위반 0건' 이 아니라 '점검하지 못함' 입니다.", file=sys.stderr)
        return 2

    scan.apply_rules()
    scan.build_graph()

    out_cfg = rules["output"]

    def resolve_out(key: str) -> Path:
        rel = Path(out_cfg[key])
        return (Path(args.out_dir) / rel.name) if args.out_dir else (root / rel)

    baseline = build_baseline(scan)
    written: list[str] = []
    if not args.no_write:
        graph_path = resolve_out("graph_json")
        mmd_path = resolve_out("mermaid")
        md_path = resolve_out("findings_md")
        for p in {graph_path.parent, mmd_path.parent, md_path.parent}:
            p.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(json.dumps({
            "generated_at": baseline["generated_at"],
            "root": str(root),
            "api_roots": scan.api_roots,
            "stats": baseline["scope"],
            "nodes": scan.nodes,
            "edges": scan.edges,
            "namespace_owners": scan.ns_owners,
            "findings": scan.findings,
            "unresolved": scan.unresolved,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mmd_path.write_text(render_mermaid(scan) + "\n", encoding="utf-8")
        md_path.write_text(render_findings_md(scan) + "\n", encoding="utf-8")
        written = [str(graph_path), str(mmd_path), str(md_path)]

    if args.write_baseline:
        # 고정선을 올린 이유(note_last_increase)는 다시 스캔한다고 알 수 있는 게
        # 아니다. 사람이 적은 것이므로 덮어쓰지 말고 이어 간다 — 이유가 사라지면
        # 남는 건 "언젠가 늘어난 숫자" 뿐이고, 그건 허용치와 구분되지 않는다.
        prior = {}
        try:
            prior = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if prior.get("note_last_increase"):
            baseline.setdefault("note_last_increase", prior["note_last_increase"])
        Path(args.baseline).write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written.append(args.baseline)

    counts = counts_by_rule(scan.findings)
    push_enabled = args.push_snapshot or os.getenv("AAG_PUSH_SNAPSHOT", "").lower() in {"1", "true", "yes", "on"}
    if push_enabled:
        push_snapshot({
            "project": "AADS", "host": socket.gethostname(),
            "generated_at": baseline["generated_at"],
            "commit_sha": os.getenv("AADS_COMMIT_SHA") or os.getenv("GIT_COMMIT"),
            "stats": baseline["scope"], "findings": scan.findings,
            "unresolved": scan.unresolved, "node_count": len(scan.nodes),
            "edge_count": len(scan.edges),
        })
    if args.json:
        print(json.dumps({
            "scope": baseline["scope"],
            "findings_by_rule": counts,
            "findings_total": len(scan.findings),
            "unresolved_total": len(scan.unresolved),
            "written": written,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"[AAG] root={root}")
        print(f"[AAG] 노드 {len(scan.nodes)} · 엣지 {len(scan.edges)} · "
              f"앱 파이썬 {len(scan.py_files)}개 · 라우터 모듈 {len(scan.modules)}개 · "
              f"프런트 {len(scan.fe_files)}개")
        print(f"[AAG] 마운트 라우트 {len(scan.mounted_routes)} · "
              f"네임스페이스 {len(scan.ns_owners)} · "
              f"프런트 호출(해석) {len(scan.fe_calls)} · 테이블 {len(scan.table_refs)}")
        for rule in RULE_ORDER:
            print(f"[AAG]   {rule:<15} {counts.get(rule, 0):>5}")
        print(f"[AAG] 결함 합계 {len(scan.findings)} · UNRESOLVED {len(scan.unresolved)} (결함 아님)")
        for w in scan.warnings:
            print(f"[AAG] 경고: {w}")
        for p in written:
            print(f"[AAG] 기록 {p}")

    if args.check_baseline:
        try:
            saved = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: 고정선을 읽지 못했습니다 — {exc}", file=sys.stderr)
            return 2
        regressions = compare_baseline(baseline, saved)
        if regressions:
            print("[AAG] 고정선 대비 증가:", file=sys.stderr)
            for r in regressions:
                print(f"  - {r}", file=sys.stderr)
            return 1
        print("[AAG] 고정선 대비 증가 없음")
        return 0

    return 1 if scan.findings else 0


if __name__ == "__main__":
    sys.exit(main())
