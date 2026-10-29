"""Restricted inserts; existing audit/request tables derive tenancy from project."""
import json


class DesignAgentPermissionError(PermissionError):
    pass


COLUMNS = {
    'design_token_sets': {'tenant_id', 'project_key', 'version', 'mode', 'tokens',
                          'created_by', 'status', 'source'},
    'design_audit_runs': {'tenant_id', 'project_key', 'status', 'score', 'findings',
                          'scanned_files'},
    'design_modification_requests': {'tenant_id', 'project_key', 'screen_id',
                                    'user_prompt', 'normalized_card', 'request_type',
                                    'allowed_scope', 'forbidden_scope',
                                    'acceptance_criteria', 'status'},
}
JSON_COLUMNS = {'tokens', 'findings', 'normalized_card', 'allowed_scope',
                'forbidden_scope', 'acceptance_criteria'}


class DesignAgentWriter:
    def __init__(self, conn, tenant_id, project_key, run_id):
        self._conn = conn
        self.tenant_id = tenant_id
        self.project_key = project_key
        self.run_id = run_id
        self.writes = []

    async def _audit(self, record, error=None):
        self.writes.append(record)
        await self._conn.execute("""UPDATE design_agent_runs
            SET writes=$3::jsonb, error=COALESCE($4,error)
            WHERE id=$1 AND tenant_id=$2""", self.run_id, self.tenant_id,
            json.dumps(self.writes), error)

    async def write(self, table, values, operation='INSERT'):
        values = dict(values)
        reason = None
        if operation.upper() != 'INSERT' or table not in COLUMNS:
            reason = 'Only allowlisted INSERTs are permitted'
        elif not self.tenant_id or str(values.get('tenant_id', '')) != str(self.tenant_id):
            reason = 'Missing or different tenant'
        elif values.get('project_key') != self.project_key:
            reason = 'Different project'
        elif set(values) - COLUMNS[table]:
            reason = 'Unknown columns or approval operation'
        elif values.get('status', 'draft') != 'draft':
            reason = 'Only draft inserts are permitted'
        elif table == 'design_token_sets' and values.get('source') not in {
                'extracted', 'generated', 'imported_genspark'}:
            reason = 'Disallowed token source'
        if not reason:
            owned = await self._conn.fetchval("""SELECT 1 FROM design_projects
                WHERE tenant_id=$1 AND project_key=$2 FOR SHARE""",
                self.tenant_id, self.project_key)
            if not owned:
                reason = 'Project does not belong to tenant'
        if not reason and values.get('screen_id'):
            owned = await self._conn.fetchval("""SELECT 1 FROM design_screens
                WHERE id=$1 AND project_key=$2""", values['screen_id'], self.project_key)
            if not owned:
                reason = 'Screen does not belong to project'
        if reason:
            await self._audit({'table': table, 'operation': operation, 'denied': reason}, reason)
            raise DesignAgentPermissionError(reason)
        values['status'] = 'draft'
        if table != 'design_token_sets':
            values.pop('tenant_id')  # Legacy tables have no tenant column.
        columns = sorted(values)
        placeholders = [f'${i}' + ('::jsonb' if c in JSON_COLUMNS else '')
                        for i, c in enumerate(columns, 1)]
        args = [json.dumps(values[c]) if c in JSON_COLUMNS else values[c] for c in columns]
        row_id = await self._conn.fetchval(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) RETURNING id", *args)
        await self._audit({'table': table, 'operation': 'INSERT', 'id': str(row_id),
                           'status': 'draft'})
        return row_id
