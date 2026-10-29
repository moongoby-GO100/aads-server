"""Queue operations on the caller's asyncpg connection/transaction."""
import json

EVENTS = {'project_created', 'deploy_succeeded', 'manual',
          'screen_change_requested', 'new_screen_requested'}


async def enqueue_trigger(conn, tenant_id, project_key, event_type, dedupe_key, payload=None):
    if not tenant_id or not project_key or not dedupe_key or event_type not in EVENTS:
        raise ValueError('Invalid trigger')
    # The no-op conflict update returns the winning id even during concurrent enqueue.
    return await conn.fetchval("""
        INSERT INTO design_agent_triggers
            (tenant_id, project_key, event_type, dedupe_key, payload)
        VALUES ($1,$2,$3,$4,$5::jsonb)
        ON CONFLICT (tenant_id, dedupe_key) DO UPDATE
        SET dedupe_key = EXCLUDED.dedupe_key RETURNING id
    """, tenant_id, project_key, event_type, dedupe_key, json.dumps(payload or {}))


async def claim_next(conn, tenant_id):
    return await conn.fetchrow("""
        UPDATE design_agent_triggers SET status='claimed', claimed_at=now(),
            attempts=attempts+1
        WHERE id = (SELECT id FROM design_agent_triggers
            WHERE tenant_id=$1 AND status='queued' ORDER BY created_at, id
            FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING *
    """, tenant_id)


async def mark_done(conn, tenant_id, trigger_id, skipped=False):
    await conn.execute("""UPDATE design_agent_triggers
        SET status=$3, finished_at=now() WHERE id=$1 AND tenant_id=$2""",
        trigger_id, tenant_id, 'skipped' if skipped else 'done')


async def mark_failed(conn, tenant_id, trigger_id):
    await conn.execute("""UPDATE design_agent_triggers
        SET status='failed', finished_at=now() WHERE id=$1 AND tenant_id=$2""",
        trigger_id, tenant_id)
