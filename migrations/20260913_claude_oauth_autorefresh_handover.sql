-- AADS-CLAUDE-OAUTH-AUTOREFRESH-P0-20260913
-- Canonical HANDOVER ledger entry. Idempotent; no credential material is stored.

BEGIN;

WITH upserted AS (
    INSERT INTO project_handover_entries (
        tenant_id,
        project_key,
        entry_key,
        entry_type,
        title,
        summary,
        body,
        status,
        priority,
        source_kind,
        source_task_id,
        source_path,
        metadata,
        created_by
    ) VALUES (
        public.aads_internal_tenant_id(),
        'AADS',
        'task:AADS-CLAUDE-OAUTH-AUTOREFRESH-P0-20260913',
        'status',
        'Claude account 1/2 OAuth auto-refresh path recovery',
        'Slot credentials take precedence over fixed env access tokens; refresh is serialized and persisted atomically.',
        E'## Implemented\n\n' ||
        E'- Claude CLI Docker and host paths use per-slot accessToken+refreshToken credentials.\n' ||
        E'- A per-account file lock prevents refresh-token reuse races; only validated credential JSON is atomically recovered.\n' ||
        E'- Fixed env access tokens are used only when the corresponding credential file is absent.\n' ||
        E'- A 401/revoked/expired response gets one same-slot refresh retry, then existing account and cross-provider fallback continues.\n' ||
        E'- AUTH-001 uses expiry, refresh-token presence, and recent redacted CLI validation state.\n' ||
        E'- Successful streams retain done; exhausted providers emit an explicit terminal interrupted event.\n\n' ||
        E'## Verification scope\n\n' ||
        E'Unit tests cover slot precedence, Docker/host refresh persistence, env fallback, concurrent locking, 401 retry, redaction, and revoked/expired AUTH-001 classification.\n\n' ||
        E'## Runtime state\n\n' ||
        E'Code-only stage. No service restart or live OAuth smoke was performed. Approved runtime smoke must verify both slots with masked expiry/mtime metadata and must stop for re-login if a refresh token is revoked or reused.',
        'active',
        'P0',
        'migration',
        'AADS-CLAUDE-OAUTH-AUTOREFRESH-P0-20260913',
        'migrations/20260913_claude_oauth_autorefresh_handover.sql',
        jsonb_build_object(
            'task_id', 'AADS-CLAUDE-OAUTH-AUTOREFRESH-P0-20260913',
            'contains_secrets', false,
            'runtime_smoke', 'pending_approval',
            'restart', 'not_performed'
        ),
        'pipeline_runner'
    )
    ON CONFLICT (tenant_id, project_key, entry_key) DO UPDATE SET
        entry_type = EXCLUDED.entry_type,
        title = EXCLUDED.title,
        summary = EXCLUDED.summary,
        body = EXCLUDED.body,
        status = EXCLUDED.status,
        priority = EXCLUDED.priority,
        source_kind = EXCLUDED.source_kind,
        source_task_id = EXCLUDED.source_task_id,
        source_path = EXCLUDED.source_path,
        metadata = EXCLUDED.metadata,
        revision = project_handover_entries.revision + 1,
        updated_at = NOW(),
        resolved_at = NULL
    WHERE project_handover_entries.summary IS DISTINCT FROM EXCLUDED.summary
       OR project_handover_entries.body IS DISTINCT FROM EXCLUDED.body
       OR project_handover_entries.metadata IS DISTINCT FROM EXCLUDED.metadata
    RETURNING *
)
INSERT INTO project_handover_events (
    entry_id,
    tenant_id,
    project_key,
    event_type,
    revision,
    snapshot,
    change_summary,
    changed_by
)
SELECT
    id,
    tenant_id,
    project_key,
    CASE WHEN revision = 1 THEN 'created' ELSE 'updated' END,
    revision,
    TO_JSONB(upserted) - 'search_vector',
    'OAuth auto-refresh recovery implementation handover',
    'pipeline_runner'
FROM upserted
ON CONFLICT (entry_id, revision) DO NOTHING;

COMMIT;
