"""마이그레이션 적용 문서(migration-apply-*)의 위임 승인 게이트.

위임 승인(canonical_document_delegation)이 migration-apply-* 문서에 적용되려면 교차검수 통과(blocker 0)에 더해
아래를 모두 만족해야 한다. 하나라도 어긋나면 사유 코드 목록을 돌려주고, 호출자는 대표 승인으로 남긴다.

  (b) 문서 manifest 의 각 파일·sha256 이 origin/main 의 파일과 일치
  (c) SQL 정적 검사: 엄격한 허용 목록 — 모든 문장이 허용 형식(SET LOCAL 시간 제한·CREATE TABLE/INDEX IF NOT EXISTS·
      제약 없는 ADD COLUMN IF NOT EXISTS·COMMENT ON)과 정확히 일치해야 한다. 하나라도 어긋나면 대표 승인
  (d) rollback 파일(migrations/rollback/<이름>.down.sql)이 origin/main 에 존재
  (e) lock_timeout 이 정확히 1회, 첫 DDL 앞에 있고 값이 1 이상 (BEGIN/COMMIT 은 허용 목록에 없으므로 자연히 거절)

사람 승인 경로와 doc_auto_approval 기준은 건드리지 않는다. 이 모듈은 읽기 전용이다.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

from app.services.doc_auto_approval import MIGRATION_KEY_PREFIX

BLOCK_RE = re.compile(r'^```aads-migration-approval\s*\n(.*?)^```\s*$', re.M | re.S)
FILE_RE = re.compile(r'migrations/[A-Za-z0-9_.-]+\.sql')
SHA_RE = re.compile(r'[0-9a-f]{64}')
MAX_FILES = 50

# 호출자가 주입하는 원본 읽기 함수: 저장소 상대경로 -> origin/main 의 바이트(없거나 읽지 못하면 None)
Reader = Callable[[str], Awaitable[Optional[bytes]]]


def is_migration_document(head: Mapping) -> bool:
    return str(head.get('document_key') or '').lower().startswith(MIGRATION_KEY_PREFIX)


def origin_main_reader() -> Reader:
    """GitHub 기본 브랜치(origin/main)의 파일을 읽는다.

    운영 API 컨테이너에는 .git 도 저장소 마운트도 없으므로 git 이 아니라 정본 문서 원격 확인과 같은
    GitHub contents API(canonical_documents._github_fetch)를 쓴다. 호출마다 원격 최신 main 을 읽으므로
    로컬 추적 참조가 낡아 있을 일이 없다. 읽지 못하면 None — 불일치로 취급해 대표 승인으로 남긴다(실패 시 닫힘).
    """
    async def read(path: str) -> Optional[bytes]:
        from app.api import canonical_documents as docs
        try:
            return await docs._github_fetch(path)
        except Exception:  # 404·네트워크·한도 초과 모두 '확인 불가'
            return None

    return read


def rollback_path(file: str) -> str:
    return 'migrations/rollback/' + Path(file).stem + '.down.sql'


def parse_manifest(content: str) -> tuple[Optional[list[dict]], Optional[str]]:
    blocks = BLOCK_RE.findall(content or '')
    if len(blocks) != 1:
        return None, 'migration_doc_invalid'
    try:
        data = json.loads(blocks[0])
    except (ValueError, TypeError):
        return None, 'migration_doc_invalid'
    items = data.get('manifest') if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or len(items) > MAX_FILES:
        return None, 'migration_doc_invalid'
    for x in items:
        if (not isinstance(x, dict) or set(x) != {'file', 'sha256'} or not isinstance(x['file'], str)
                or not FILE_RE.fullmatch(x['file']) or not isinstance(x['sha256'], str)
                or not SHA_RE.fullmatch(x['sha256'])):
            return None, 'migration_doc_invalid'
    return items, None


# ── 정적 검사: 엄격한 허용 목록 ─────────────────────────────────────────────────────────────
# 금지 목록 방식(위험 구문을 찾아 막기)은 두 회차 연속 우회됐다. 그래서 반대로 한다: 파일을 문장으로 나눠
# 모든 문장이 아래 형식 중 하나와 '정확히' 일치할 때만 위임 대상이고, 하나라도 어긋나면 대표 승인으로 넘긴다.
# 허용 형식을 넓히려면 코드를 고치고 CEO 검토를 받는다 — 예외 정규식을 덧대지 않는다.

_FORBIDDEN_WORDS = ('DO', 'EXECUTE', 'FUNCTION', 'PROCEDURE', 'TRIGGER', 'RULE', 'set_config', 'DELETE', 'TRUNCATE',
                    'DROP', 'UPDATE', 'INSERT', 'GRANT', 'REVOKE', 'RENAME', 'TYPE', 'USING')
_FORBIDDEN_RE = re.compile(r'(?<![A-Za-z0-9_])(' + '|'.join(_FORBIDDEN_WORDS) + r')(?![A-Za-z0-9_])', re.I)
_DOLLAR_TAG_RE = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$')  # $$ 와 $tag$ ('...{0,63}$' 같은 단독 $ 는 해당 없음)

_ID = r'(?-i:[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)'  # 따옴표 없는 소문자 식별자, 스키마 접두사 1개까지
_ID_COL = r'(?-i:[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*){1,2})'  # COMMENT ON COLUMN [스키마.]표.열
_FLAGS = re.I | re.S
_LOCK_RE = re.compile(r"SET\s+LOCAL\s+lock_timeout\s*=\s*'([1-9][0-9]*)(?:ms|s)'", _FLAGS)
_STMT_TIMEOUT_RE = re.compile(r"SET\s+LOCAL\s+statement_timeout\s*=\s*'([1-9][0-9]*)(?:ms|s|min)'", _FLAGS)
_CREATE_TABLE_HEAD = re.compile(r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+' + _ID + r'\s*\(', _FLAGS)
_CREATE_INDEX_HEAD = re.compile(
    r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?IF\s+NOT\s+EXISTS\s+' + _ID + r'\s+ON\s+' + _ID + r'\s*\(', _FLAGS)
_WHERE_TAIL = re.compile(r'\s*WHERE\s+\S.*', _FLAGS)
# 열 타입도 허용 목록이다. NOT NULL·DEFAULT·REFERENCES·CHECK 등 어떤 제약도 붙을 수 없다.
_COL_TYPE = (r'(?:text|uuid|jsonb?|bool(?:ean)?|int(?:eger|[248])?|bigint|smallint|real|float[48]|double\s+precision'
             r'|date|bytea|inet|cidr|interval|timestamptz|timestamp(?:\s+with(?:out)?\s+time\s+zone)?'
             r'|time(?:\s+with(?:out)?\s+time\s+zone)?|(?:numeric|decimal)(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?'
             r'|(?:varchar|character\s+varying|char|character)(?:\s*\(\s*\d+\s*\))?)(?:\s*\[\s*\])?')
_ADD_COLUMN_RE = re.compile(
    r'ALTER\s+TABLE\s+' + _ID + r'\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(?-i:[a-z_][a-z0-9_]*)\s+' + _COL_TYPE, _FLAGS)
_COMMENT_RE = re.compile(
    r"COMMENT\s+ON\s+(?:(?:TABLE|INDEX)\s+" + _ID + r"|COLUMN\s+" + _ID_COL + r")\s+IS\s+'(?:[^']|'')*'", _FLAGS)


def _scan_statements(sql: str) -> tuple[list[str], list[str]]:
    """원문을 ; 기준 문장으로 나눈다. 문장 밖 줄 주석(--)만 지운다. -> (문장들, 구조 문제 코드들)

    정규화·토크나이저를 거치지 않고 원문 문자를 그대로 보존한다(숫자가 사라지는 일이 없도록).
    """
    stmts: list[str] = []
    problems: list[str] = []
    cur: list[str] = []
    stmts_on_line = 0
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == '-' and sql.startswith('--', i):
            if ''.join(cur).strip():
                problems.append('comment_inside_statement')  # 문장 안 주석은 가려 두는 것이 없는지 보장할 수 없다
            j = sql.find('\n', i)
            i = n if j < 0 else j
            continue
        if c == "'":
            if i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in '_&'):
                problems.append('prefixed_string')  # E'..\'..' 등 백슬래시·접두 문자열
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            else:
                problems.append('unterminated_string')
                j = n - 1
            literal = sql[i:j + 1]
            if '\n' in literal:
                stmts_on_line = 0
            cur.append(literal)
            i = j + 1
            continue
        if c == '"':
            problems.append('quoted_identifier')
        if c == '\n':
            stmts_on_line = 0
            cur.append(c)
        elif c == ';':
            stmt = ''.join(cur).strip()
            if stmt:
                stmts.append(stmt)
            stmts_on_line += 1
            if stmts_on_line >= 2 and 'multiple_statements_one_line' not in problems:
                problems.append('multiple_statements_one_line')
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = ''.join(cur).strip()
    if tail:
        stmts.append(tail)
    return [s for s in stmts if s], problems


def _balanced_close(text: str, open_idx: int) -> int:
    """text[open_idx] == '(' 의 짝 ')' 위치. 문자열 안 괄호는 건너뛴다. 없으면 -1."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            i += 1
            while i < n and not (text[i] == "'" and not text.startswith("''", i)):
                i += 2 if text.startswith("''", i) else 1
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _is_create_table(stmt: str) -> bool:
    m = _CREATE_TABLE_HEAD.match(stmt)
    return bool(m) and _balanced_close(stmt, m.end() - 1) == len(stmt) - 1


def _is_create_index(stmt: str) -> bool:
    m = _CREATE_INDEX_HEAD.match(stmt)
    if not m:
        return False
    close = _balanced_close(stmt, m.end() - 1)
    if close < 0 or not stmt[m.end():close].strip():
        return False
    rest = stmt[close + 1:]
    return not rest.strip() or bool(_WHERE_TAIL.fullmatch(rest))


def _is_ddl_allowed(stmt: str) -> bool:
    return (_is_create_table(stmt) or _is_create_index(stmt)
            or bool(_ADD_COLUMN_RE.fullmatch(stmt)) or bool(_COMMENT_RE.fullmatch(stmt)))


def static_problems(sql_text: str) -> list[str]:
    """허용 목록 정적 검사. 빈 목록이어야 위임 대상이다. 사유 코드는 모두 대표 승인으로 넘기는 사유다."""
    from scripts import check_migration_txn as txn

    if txn.check_migration_text(sql_text).skipped:  # 업무 DB 표기 파일 등은 이 게이트의 대상이 아니다
        return ['migration_check_skipped']
    statements, problems = _scan_statements(sql_text)
    if any('/*' in s for s in statements):  # 문장 안(문자열 포함)·문장 사이 어디든 블록 주석은 위임 불가
        problems.append('block_comment')
    body = '\n'.join(statements)
    for word in sorted({m.group(1).upper() for m in _FORBIDDEN_RE.finditer(body)}):
        problems.append(f'forbidden_word_{word.lower()}')
    if _DOLLAR_TAG_RE.search(body):
        problems.append('forbidden_dollar_quote')
    if not statements:
        problems.append('empty_migration')

    lock_idx: list[int] = []
    first_ddl: Optional[int] = None
    for idx, stmt in enumerate(statements):
        if _LOCK_RE.fullmatch(stmt):
            lock_idx.append(idx)
        elif _STMT_TIMEOUT_RE.fullmatch(stmt):
            continue
        elif _is_ddl_allowed(stmt):
            if first_ddl is None:
                first_ddl = idx
        else:
            problems.append(f'statement_not_allowed_{idx + 1}')
    if not lock_idx:
        problems.append('lock_timeout_missing')
    elif len(lock_idx) > 1:
        problems.append('lock_timeout_multiple')
    elif first_ddl is not None and lock_idx[0] > first_ddl:
        problems.append('lock_timeout_after_ddl')
    return list(dict.fromkeys(problems))


async def check_files(items: list[dict], read: Reader) -> list[str]:
    reasons: list[str] = []
    for x in items:
        name = x['file']
        data = await read(name)
        if data is None:
            reasons.append(f'origin_file_missing:{name}')
            continue
        if hashlib.sha256(data).hexdigest() != x['sha256']:
            reasons.append(f'sha_mismatch:{name}')
            continue
        if await read(rollback_path(name)) is None:
            reasons.append(f'rollback_missing:{name}')
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            reasons.append(f'not_utf8:{name}')
            continue
        reasons.extend(f'{p}:{name}' for p in static_problems(text))
    return reasons


async def migration_delegation_problems(content: str, reader: Optional[Reader] = None) -> dict[str, Any]:
    """-> {'ok': bool, 'reasons': [...], 'files': [{'file','sha256'}]}. 실패 사유는 모두 대표 승인으로 남기는 사유다."""
    items, bad = parse_manifest(content)
    if bad:
        return {'ok': False, 'reasons': [bad], 'files': []}
    reasons = await check_files(items, reader or origin_main_reader())
    return {'ok': not reasons, 'reasons': reasons, 'files': items}
