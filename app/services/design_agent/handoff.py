"""Bind explicitly marked runner directives to tenant-owned approved tokens."""
import hashlib
import json
import re


class DesignHandoffRejected(Exception):
    def __init__(self, code: str):
        self.code = code
        self.message = {
            'design_approval_missing': '프로젝트의 디자인 승인본이 없습니다.',
            'design_draft_injection': '초안 디자인 토큰은 작업에 주입할 수 없습니다.',
        }[code]
        super().__init__(self.message)


def _project_header(instruction: str) -> str | None:
    # A header is the leading metadata paragraph, not prose or fenced examples.
    for line in instruction.splitlines():
        if not line.strip():
            continue
        # Skip flag lines without colons (e.g., ALLOW_DUP_JOB, NO_AUTO_REWORK)
        if re.fullmatch(r'[ \t]*[A-Z][A-Z0-9_]*[ \t]*', line):
            continue
        field = re.fullmatch(r'[ \t]*([A-Z][A-Z0-9_]*)[ \t]*:[ \t]*(.*?)[ \t]*', line)
        if not field:
            break
        if field[1] == 'DESIGN_PROJECT' and field[2]:
            return field[2]
    return None


async def resolve_approved(conn, tenant_id, project_key):
    # tenant_id/status are defined by 20261029_design_token_tenant_isolation.sql.
    row = await conn.fetchrow(
        """SELECT version, tokens FROM design_token_sets
           WHERE tenant_id = $1::uuid AND project_key = $2 AND status = 'approved'
           ORDER BY created_at DESC, version DESC, id DESC LIMIT 1""",
        tenant_id, project_key,
    )
    if row is None:
        return None
    tokens = row['tokens']
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    canonical = json.dumps(tokens, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return {'version': row['version'], 'sha256': hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
            'tokens': tokens}


def _remove_approval_blocks(instruction: str) -> str:
    # Own the whole reserved H2 section, including blank lines and forged fields.
    return re.sub(r'^##[ \t]+디자인 승인본[ \t]*\r?$.*?(?=^#{1,2}[ \t]+|\Z)', '',
                  instruction, flags=re.MULTILINE | re.DOTALL).rstrip()


async def apply_design_gate(conn, tenant_id, instruction):
    project_key = _project_header(instruction)
    if project_key is None:
        return instruction, None
    if re.search(r'^[ \t]*DESIGN_TOKEN_STATUS[ \t]*:[ \t]*draft\b', instruction,
                 flags=re.MULTILINE | re.IGNORECASE):
        raise DesignHandoffRejected('design_draft_injection')
    approved = await resolve_approved(conn, tenant_id, project_key)
    if approved is None:
        raise DesignHandoffRejected('design_approval_missing')
    meta = {key: approved[key] for key in ('version', 'sha256')}
    keys = sorted(approved['tokens']) if isinstance(approved['tokens'], dict) else []
    block = ('\n\n## 디자인 승인본\n'
             f'- project_key: {project_key}\n'
             f'- version: {meta["version"]}\n'
             f'- sha256: {meta["sha256"]}\n'
             f'- token_keys: {json.dumps(keys, ensure_ascii=False)}\n')
    return _remove_approval_blocks(instruction) + block, meta
