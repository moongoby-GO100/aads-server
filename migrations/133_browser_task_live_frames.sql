-- AADS-208: Managed Browser live view frames.
-- Latest-frame storage only. Long-term audit stays in browser_task_events metadata.

CREATE TABLE IF NOT EXISTS browser_task_live_frames (
    task_id UUID PRIMARY KEY REFERENCES browser_tasks(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    frame_base64 TEXT NOT NULL DEFAULT '',
    frame_url TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT 'image/jpeg',
    width INTEGER NULL,
    height INTEGER NULL,
    current_url TEXT NOT NULL DEFAULT '',
    page_title TEXT NOT NULL DEFAULT '',
    current_step TEXT NOT NULL DEFAULT '',
    cursor JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_task_live_frames_tenant_updated
    ON browser_task_live_frames(tenant_id, updated_at DESC);
