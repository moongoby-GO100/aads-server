"""db_safe_write 입력 SQL 을 DB 연결 전에 한 문장으로 확정한다.

2026-09-30 리뷰 P1-B. params 없는 ``UPDATE ...; COMMIT; BEGIN`` 은 asyncpg 가
simple query 프로토콜로 보내므로 한 번에 여러 문장이 실행된다. 보호 검사가 뒤에서
"롤백" 을 말해도 COMMIT 이 이미 변경을 확정한다. 그래서 사후 검사가 아니라
실행 전에 막는다.

``split(";")`` 는 쓰지 않는다. 문자열 리터럴·인용 식별자·달러 인용·주석 안의
세미콜론은 구분자가 아니고, 반대로 그렇게 보이는 곳에 숨긴 세미콜론은 구분자다.
PostgreSQL 렉서 규칙(standard_conforming_strings=on, 중첩 블록 주석)을 따라
한 글자씩 훑는다. 모호하면(닫히지 않은 인용, U& 이스케이프) 거부한다.

2026-09-30 간접 쓰기 경로 차단. 보호 테이블 이름이 SQL 에 나타나지 않아도 저장 함수·
프로시저가 내부에서 바꿀 수 있다. 정적으로 전부 알아낼 수 없으므로 **모르는 함수는 막는다**
(fail closed): 문장 안의 모든 함수 호출 식별자를 ``ALLOWED_BUILTIN_FUNCTIONS`` 와 대조한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ALLOWED_WRITE_KEYWORDS = frozenset({"insert", "update", "delete"})

# 문장 첫 단어가 이것이면 트랜잭션 경계를 바꾸거나 임의 코드를 실행한다.
TRANSACTION_CONTROL_KEYWORDS = frozenset({
    "begin", "start", "commit", "end", "rollback", "abort",
    "savepoint", "release", "prepare", "set",
})
# DO/CALL 은 저장 프로시저·익명 블록으로 보호 테이블을 간접 변경한다. EXECUTE(준비된 문장 실행)와
# PERFORM 도 같은 이유로 첫 단어에서 막는다.
PROCEDURAL_KEYWORDS = frozenset({"do", "call", "execute", "perform"})

# 값·조건 자리에 쓰는 함수 허용 목록. 근거: 전부 pg_catalog 내장이며 테이블을 읽거나 쓰는
# SQL 을 실행하지 않는 순수 계산(또는 시퀀스 외 부작용이 없는 gen_random_uuid/random)이다.
# 여기 없는 식별자가 "(" 앞에 오면 저장 함수일 수 있으므로 거부한다. 실무에서 막히면
# 화이트리스트에 넣지 말고 docs/operations/DB_SAFE_WRITE_INDIRECT_PATH.md 의 오탐 신고 경로를 따른다.
# 의도적으로 뺀 것: set_config, pg_sleep, query_to_xml/xpath 류(SQL 문자열 실행), lo_*, dblink,
# nextval/setval(시퀀스 변경), 사용자 정의 함수 전부.
ALLOWED_BUILTIN_FUNCTIONS = frozenset({
    # 시각
    "now", "clock_timestamp", "statement_timestamp", "transaction_timestamp", "timeofday",
    "date_trunc", "date_part", "extract", "age", "make_interval", "make_date", "make_timestamp",
    "make_timestamptz", "to_timestamp", "to_date", "to_char", "to_number", "timezone",
    # NULL·비교
    "coalesce", "nullif", "greatest", "least",
    # 문자열
    "lower", "upper", "initcap", "trim", "btrim", "ltrim", "rtrim", "length", "char_length",
    "character_length", "octet_length", "substring", "substr", "left", "right", "replace",
    "regexp_replace", "split_part", "concat", "concat_ws", "format", "lpad", "rpad", "repeat",
    "reverse", "position", "overlay", "md5", "encode", "decode", "translate", "starts_with",
    # 숫자
    "abs", "round", "ceil", "ceiling", "floor", "trunc", "mod", "power", "sqrt", "sign",
    "random", "gen_random_uuid",
    # JSON
    "jsonb_set", "jsonb_insert", "jsonb_build_object", "jsonb_build_array", "json_build_object",
    "json_build_array", "jsonb_strip_nulls", "to_jsonb", "to_json", "jsonb_typeof",
    "jsonb_array_length", "jsonb_extract_path", "jsonb_extract_path_text",
    # 배열
    "array_append", "array_prepend", "array_remove", "array_cat", "array_length", "cardinality",
    "array_position", "array_to_string", "string_to_array", "unnest", "array",
    # 집계(하위 조회용)
    "count", "sum", "min", "max", "avg", "array_agg", "string_agg", "jsonb_agg", "bool_and", "bool_or",
    # 형 변환·타입 한정자(CAST(x AS numeric(10,2)), x::varchar(20))
    "cast", "varchar", "char", "character", "numeric", "decimal", "timestamp", "timestamptz",
    "time", "timetz", "interval", "bit", "varbit",
})

# "(" 앞에 와도 함수 호출이 아닌 SQL 문법 키워드.
_SYNTAX_BEFORE_PAREN = frozenset({
    "values", "in", "any", "all", "some", "exists", "on", "using", "set", "from", "where", "and",
    "or", "not", "select", "returning", "when", "then", "else", "case", "join", "between", "is",
    "like", "ilike", "conflict", "filter", "lateral", "union", "intersect", "except", "distinct",
    "by", "limit", "offset", "row", "default", "into", "as", "to", "overlaps", "having",
})


class SqlGuardError(ValueError):
    """단일 INSERT/UPDATE/DELETE 로 확정할 수 없는 SQL."""


class IndirectWriteBlocked(SqlGuardError):
    """저장 함수·프로시저 등 보호 테이블을 간접 변경할 수 있는 형태."""


@dataclass(frozen=True)
class WriteTarget:
    """쓰기 대상 테이블. 소문자로 정규화한다(인용 식별자 포함 — 보수적 매칭)."""

    schema: str | None
    name: str


@dataclass
class SqlStatementInfo:
    keyword: str
    # 식별자·키워드(소문자). 인용 식별자도 소문자로 넣는다 — 보호 대상 탐지는 보수적으로.
    words: list[str] = field(default_factory=list)
    # 문자열 리터럴·달러 인용 본문(소문자). 동적 SQL 로 테이블 이름을 넘기는 경우 탐지용.
    literals: list[str] = field(default_factory=list)
    # 대상 테이블을 확정하지 못하면 None — 호출자는 통과시키지 말고 보호 검사를 강제해야 한다.
    target: WriteTarget | None = None


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_" or ord(ch) >= 0x80


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or ord(ch) >= 0x80


def _dollar_tag_at(sql: str, i: int) -> str | None:
    """sql[i] == '$' 에서 시작하는 달러 인용 태그(``$tag$``)를 돌려준다. 아니면 None."""
    n = len(sql)
    j = i + 1
    if j < n and sql[j] == "$":
        return "$$"
    if j < n and _is_ident_start(sql[j]):
        j += 1
        while j < n and (sql[j].isalnum() or sql[j] == "_" or ord(sql[j]) >= 0x80):
            j += 1
        if j < n and sql[j] == "$":
            return sql[i:j + 1]
    return None


_Token = tuple[str, str]  # (kind, text) — w 단어 / q 인용 식별자 / s 문자열 / n 숫자 / p . ( ) , / o 그 밖의 기호 / ; 구분자


def _split_statements(
    sql: str, tokens: list[_Token] | None = None,
) -> list[tuple[list[str], list[str]]]:
    """최상위 세미콜론으로 나눈 문장별 (words, literals).

    tokens 를 주면 문장 구분 없이 전체 토큰 열을 채운다(공백·주석 제외, 순서 보존).
    함수 호출("식별자 (")과 대상 테이블 판정은 words 로는 못 하므로 — "." 와 "(" 가 없다 — 이쪽을 쓴다.
    """
    statements: list[tuple[list[str], list[str]]] = []
    words: list[str] = []
    literals: list[str] = []
    n = len(sql)
    i = 0
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif ch == ";":
            statements.append((words, literals))
            words, literals = [], []
            if tokens is not None:
                tokens.append((";", ";"))
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise SqlGuardError("닫히지 않은 블록 주석")
        elif ch == "'":
            i = _scan_quoted(sql, i, "'", backslash=False, out=literals)
            if tokens is not None:
                tokens.append(("s", literals[-1]))
        elif ch == '"':
            i = _scan_quoted(sql, i, '"', backslash=False, out=words)
            if tokens is not None:
                tokens.append(("q", words[-1]))
        elif ch == "$":
            tag = _dollar_tag_at(sql, i)
            if tag is None:
                if tokens is not None:
                    tokens.append(("o", "$"))
                i += 1  # $1 같은 바인드 파라미터 또는 연산자
                continue
            body_start = i + len(tag)
            end = sql.find(tag, body_start)
            if end < 0:
                raise SqlGuardError("닫히지 않은 달러 인용")
            literals.append(sql[body_start:end].lower())
            if tokens is not None:
                tokens.append(("s", literals[-1]))
            i = end + len(tag)
        elif _is_ident_start(ch):
            j = i + 1
            while j < n and _is_ident_char(sql[j]):
                j += 1
            word = sql[i:j].lower()
            nxt = sql[j] if j < n else ""
            if word == "u" and nxt == "&":
                # U&'..' / U&".." 는 \0075 같은 이스케이프로 이름을 숨길 수 있다.
                raise SqlGuardError("U& 유니코드 이스케이프는 허용하지 않음")
            if word == "e" and nxt == "'":
                i = _scan_quoted(sql, j, "'", backslash=True, out=literals)
                if tokens is not None:
                    tokens.append(("s", literals[-1]))
                continue
            if word in ("b", "x", "n") and nxt == "'":
                i = _scan_quoted(sql, j, "'", backslash=False, out=literals)
                if tokens is not None:
                    tokens.append(("s", literals[-1]))
                continue
            words.append(word)
            if tokens is not None:
                tokens.append(("w", word))
            i = j
        elif ch.isdigit():
            # 숫자 리터럴(1.5 의 "." 가 이름 구분자로 보이지 않게). 뒤따르는 글자는 기존처럼 단어로 남긴다.
            j = i + 1
            while j < n and (sql[j].isdigit() or (sql[j] == "." and j + 1 < n and sql[j + 1].isdigit())):
                j += 1
            if tokens is not None:
                tokens.append(("n", sql[i:j]))
            i = j
        else:
            if tokens is not None:
                # 연산자도 남긴다 — 없으면 "= (subquery)" 가 "이름 (" 으로 보인다.
                tokens.append(("p" if ch in ".()," else "o", ch))
            i += 1
    statements.append((words, literals))
    return statements


def _scan_quoted(sql: str, start: int, quote: str, *, backslash: bool, out: list[str]) -> int:
    """sql[start] == quote. 닫는 따옴표 다음 위치를 돌려주고 본문(소문자)을 out 에 넣는다."""
    n = len(sql)
    i = start + 1
    buf: list[str] = []
    while i < n:
        ch = sql[i]
        if backslash and ch == "\\":
            buf.append(sql[i:i + 2])
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                buf.append(quote)
                i += 2
                continue
            out.append("".join(buf).lower())
            return i + 1
        buf.append(ch)
        i += 1
    raise SqlGuardError("닫히지 않은 따옴표")


def _is_ident(token: _Token) -> bool:
    return token[0] in ("w", "q")


def _parse_name(tokens: list[_Token], i: int) -> tuple[WriteTarget, int] | None:
    """tokens[i:] 의 ``[schema.]name`` 을 읽는다. 3 단 이상(db.schema.name)이나 식별자가 아니면 None."""
    if i >= len(tokens) or not _is_ident(tokens[i]):
        return None
    first = tokens[i][1]
    j = i + 1
    if j + 1 < len(tokens) and tokens[j] == ("p", ".") and _is_ident(tokens[j + 1]):
        name, j = tokens[j + 1][1], j + 2
        if j < len(tokens) and tokens[j] == ("p", "."):
            return None
        return WriteTarget(first, name), j
    if j < len(tokens) and tokens[j] == ("p", "."):
        return None
    return WriteTarget(None, first), j


def extract_write_target(tokens: list[_Token]) -> WriteTarget | None:
    """UPDATE [ONLY] t / DELETE FROM [ONLY] t / INSERT INTO t 의 대상. 확정 못 하면 None."""
    if not tokens or tokens[0][0] != "w":
        return None
    keyword = tokens[0][1]
    i = 1
    if keyword == "delete":
        if tokens[1:2] != [("w", "from")]:
            return None
        i = 2
    elif keyword == "insert":
        if tokens[1:2] != [("w", "into")]:
            return None
        i = 2
    elif keyword != "update":
        return None
    if keyword != "insert" and tokens[i:i + 1] == [("w", "only")]:
        i += 1
    parsed = _parse_name(tokens, i)
    if parsed is None:
        return None
    target, end = parsed
    # UPDATE/DELETE 의 대상 뒤에 "(" 가 오면 우리가 모르는 형태다.
    if keyword != "insert" and tokens[end:end + 1] == [("p", "(")]:
        return None
    return target


def find_disallowed_function_calls(tokens: list[_Token]) -> list[str]:
    """허용 목록에 없는 함수 호출 식별자(``이름 (``)를 등장 순서대로 돌려준다.

    - 인용 식별자(``"fn"(..)``)는 렉서가 대소문자를 잃으므로 항상 불허 — ``"LOWER"`` 가 내장 lower 로 읽힌다.
    - schema-qualified 는 ``pg_catalog.`` 만 허용 목록과 대조한다.
    - ``INSERT INTO t (cols)``·``AS a (cols)``·``) a (cols)`` 는 이름 뒤 열 목록이라 호출이 아니다.
    """
    bad: list[str] = []
    for i in range(len(tokens) - 1):
        if not _is_ident(tokens[i]) or tokens[i + 1] != ("p", "("):
            continue
        start = i
        while start >= 2 and tokens[start - 1] == ("p", ".") and _is_ident(tokens[start - 2]):
            start -= 2
        prev = tokens[start - 1] if start > 0 else None
        if prev in (("w", "into"), ("w", "as"), ("p", ")")):
            continue
        kind, name = tokens[i]
        qualified = start != i
        if not qualified and kind == "w" and name in _SYNTAX_BEFORE_PAREN:
            continue
        if kind == "w" and name in ALLOWED_BUILTIN_FUNCTIONS:
            if not qualified:
                continue
            if start == i - 2 and tokens[start] == ("w", "pg_catalog"):
                continue
        label = ".".join(t[1] for t in tokens[start:i + 1] if t[0] != "p")
        if label not in bad:
            bad.append(label)
    return bad


def validate_single_write(sql: str) -> SqlStatementInfo:
    """단일 INSERT/UPDATE/DELETE 문이면 정보를, 아니면 SqlGuardError 를 낸다.

    끝의 세미콜론 하나(뒤에 공백·주석뿐)는 허용한다. 허용 목록 밖의 함수 호출은
    IndirectWriteBlocked(SqlGuardError 하위)로 거부한다.
    """
    tokens: list[_Token] = []
    statements = [s for s in _split_statements(sql, tokens) if s[0] or s[1]]
    if not statements:
        raise SqlGuardError("빈 SQL")
    for words, _ in statements:
        first = words[0] if words else ""
        if first in TRANSACTION_CONTROL_KEYWORDS:
            raise SqlGuardError(f"트랜잭션 제어문 차단: {first.upper()}")
        if first in PROCEDURAL_KEYWORDS:
            raise IndirectWriteBlocked(
                f"{first.upper()} 차단 — 저장 프로시저·익명 블록은 보호 테이블을 간접 변경할 수 있음"
            )
    if len(statements) != 1:
        raise SqlGuardError(f"단일 문장만 허용 (감지된 문장 {len(statements)}개)")
    words, literals = statements[0]
    keyword = words[0] if words else ""
    calls = find_disallowed_function_calls(tokens)
    if keyword not in ALLOWED_WRITE_KEYWORDS:
        if keyword == "select" and calls:
            raise IndirectWriteBlocked(
                "SELECT 로 함수 호출 차단(" + ", ".join(calls[:5]) + ") — 저장 함수는 보호 테이블을 간접 변경할 수 있음"
            )
        raise SqlGuardError("INSERT/UPDATE/DELETE만 허용")
    if calls:
        raise IndirectWriteBlocked(
            "허용 목록에 없는 함수 호출 차단(" + ", ".join(calls[:5]) + ") — 저장 함수는 보호 테이블을 간접 변경할 수 있음"
        )
    return SqlStatementInfo(
        keyword=keyword, words=words, literals=literals, target=extract_write_target(tokens),
    )
