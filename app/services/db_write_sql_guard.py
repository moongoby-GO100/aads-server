"""db_safe_write 입력 SQL 을 DB 연결 전에 한 문장으로 확정한다.

2026-09-30 리뷰 P1-B. params 없는 ``UPDATE ...; COMMIT; BEGIN`` 은 asyncpg 가
simple query 프로토콜로 보내므로 한 번에 여러 문장이 실행된다. 보호 검사가 뒤에서
"롤백" 을 말해도 COMMIT 이 이미 변경을 확정한다. 그래서 사후 검사가 아니라
실행 전에 막는다.

``split(";")`` 는 쓰지 않는다. 문자열 리터럴·인용 식별자·달러 인용·주석 안의
세미콜론은 구분자가 아니고, 반대로 그렇게 보이는 곳에 숨긴 세미콜론은 구분자다.
PostgreSQL 렉서 규칙(standard_conforming_strings=on, 중첩 블록 주석)을 따라
한 글자씩 훑는다. 모호하면(닫히지 않은 인용, U& 이스케이프) 거부한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ALLOWED_WRITE_KEYWORDS = frozenset({"insert", "update", "delete"})

# 문장 첫 단어가 이것이면 트랜잭션 경계를 바꾸거나 임의 코드를 실행한다.
TRANSACTION_CONTROL_KEYWORDS = frozenset({
    "begin", "start", "commit", "end", "rollback", "abort",
    "savepoint", "release", "prepare", "set",
})
PROCEDURAL_KEYWORDS = frozenset({"do", "call"})


class SqlGuardError(ValueError):
    """단일 INSERT/UPDATE/DELETE 로 확정할 수 없는 SQL."""


@dataclass
class SqlStatementInfo:
    keyword: str
    # 식별자·키워드(소문자). 인용 식별자도 소문자로 넣는다 — 보호 대상 탐지는 보수적으로.
    words: list[str] = field(default_factory=list)
    # 문자열 리터럴·달러 인용 본문(소문자). 동적 SQL 로 테이블 이름을 넘기는 경우 탐지용.
    literals: list[str] = field(default_factory=list)


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


def _split_statements(sql: str) -> list[tuple[list[str], list[str]]]:
    """최상위 세미콜론으로 나눈 문장별 (words, literals)."""
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
        elif ch == '"':
            i = _scan_quoted(sql, i, '"', backslash=False, out=words)
        elif ch == "$":
            tag = _dollar_tag_at(sql, i)
            if tag is None:
                i += 1  # $1 같은 바인드 파라미터 또는 연산자
                continue
            body_start = i + len(tag)
            end = sql.find(tag, body_start)
            if end < 0:
                raise SqlGuardError("닫히지 않은 달러 인용")
            literals.append(sql[body_start:end].lower())
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
                continue
            if word in ("b", "x", "n") and nxt == "'":
                i = _scan_quoted(sql, j, "'", backslash=False, out=literals)
                continue
            words.append(word)
            i = j
        else:
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


def validate_single_write(sql: str) -> SqlStatementInfo:
    """단일 INSERT/UPDATE/DELETE 문이면 정보를, 아니면 SqlGuardError 를 낸다.

    끝의 세미콜론 하나(뒤에 공백·주석뿐)는 허용한다.
    """
    statements = [s for s in _split_statements(sql) if s[0] or s[1]]
    if not statements:
        raise SqlGuardError("빈 SQL")
    for words, _ in statements:
        first = words[0] if words else ""
        if first in TRANSACTION_CONTROL_KEYWORDS:
            raise SqlGuardError(f"트랜잭션 제어문 차단: {first.upper()}")
        if first in PROCEDURAL_KEYWORDS:
            raise SqlGuardError(f"{first.upper()} 차단")
    if len(statements) != 1:
        raise SqlGuardError(f"단일 문장만 허용 (감지된 문장 {len(statements)}개)")
    words, literals = statements[0]
    keyword = words[0] if words else ""
    if keyword not in ALLOWED_WRITE_KEYWORDS:
        raise SqlGuardError("INSERT/UPDATE/DELETE만 허용")
    return SqlStatementInfo(keyword=keyword, words=words, literals=literals)
