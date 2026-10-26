"""계획 모드 전용 조회: 셸과 사용자 SQL을 실행하지 않는다."""
from pathlib import Path
import os
import stat
import re

ROOT = Path(__file__).resolve().parents[1]
NAMES = frozenset({'plan_read_source', 'plan_database_status', 'plan_database_schema'})
# The bridge uses this audited subset for MCP metadata. The wider legacy
# plan guard is not sufficient evidence for a read-only annotation.
# Existing handlers: ToolExecutor._handover_search (tenant-scoped SELECT),
# _pipeline_runner_status (GET), canonical_document_lookup (lookup only).
VERIFIED_READ_ONLY_TOOLS = NAMES | frozenset({
    'handover_search', 'pipeline_runner_status', 'canonical_document_lookup',
})
DEFINITIONS = [
    {'name': 'plan_database_schema', 'description': '공개 스키마의 테이블 열 구조 조회. 데이터와 사용자 SQL은 읽거나 실행하지 않습니다.',
     'input_schema': {'type': 'object', 'properties': {'table': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,62}$'}}, 'required': ['table'], 'additionalProperties': False}},
    {'name': 'plan_read_source', 'description': 'AADS 서버 저장소의 소스 파일 읽기. 상대 경로만 허용하며 시크릿과 symlink는 거부합니다.',
     'input_schema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}},
    {'name': 'plan_database_status', 'description': '고정 SELECT 1 및 현재 트랜잭션 읽기 전용 여부 조회. SQL 입력을 받지 않습니다.',
     'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
]


def read_source(path):
    # dir_fd + O_NOFOLLOW on every component prevents both traversal and symlink races.
    if not isinstance(path, str) or len(path) > 512:
        raise ValueError('invalid source path')
    parts = path.split('/')
    if (parts[0] not in {'app', 'scripts', 'mcp_servers', 'tests'}
            or any(not p or p in {'.', '..'} or p.startswith('.') for p in parts)
            or Path(path).suffix not in {'.py', '.sh', '.ts', '.tsx'}
            or any(word in path.lower() for word in ('secret', 'credential', 'token', 'private_key', 'auth_keys'))):
        raise ValueError('source path denied')
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 131072:
                raise ValueError('source file denied')
            content = source.read(131073).decode('utf-8')
            # Fail closed for inline credentials as well as credential filenames.
            if re.search(r'(?i)(sk-ant-|-----BEGIN .*PRIVATE KEY|(?:password|secret|token|api_key)\s*[=:]\s*[\"\'][^\"\']{8,})', content):
                raise ValueError('potential secret content denied')
            return {'path': path, 'content': content}
    finally:
        os.close(fd)


async def execute(name, params):
    if name == 'plan_read_source':
        if set(params) != {'path'}:
            raise ValueError('only path is accepted')
        return read_source(params['path'])
    if name == 'plan_database_schema':
        table = params.get('table')
        if set(params) != {'table'} or not isinstance(table, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', table):
            raise ValueError('invalid table name')
        from app.core.db_pool import get_pool
        async with get_pool().acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL statement_timeout = '2s'")
                await conn.execute("SET LOCAL lock_timeout = '500ms'")
                rows = await conn.fetch(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = $1 "
                    "ORDER BY ordinal_position LIMIT 200", table,
                )
                return {'table': table, 'columns': [dict(row) for row in rows]}
    if name == 'plan_database_status':
        if params:
            raise ValueError('SQL and other arguments are forbidden')
        from app.core.db_pool import get_pool
        async with get_pool().acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL statement_timeout = '2s'")
                await conn.execute("SET LOCAL lock_timeout = '500ms'")
                row = await conn.fetchrow("SELECT 1 AS read_probe, current_setting('transaction_read_only') AS read_only")
                return dict(row)
    raise ValueError('unknown plan read tool')
