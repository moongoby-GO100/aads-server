-- AADS dirty worktree governance.
-- Additive only: record file-level owner/session/task_id attribution and git status snapshots.

ALTER TABLE chat_workspace_change_ledger
    ADD COLUMN IF NOT EXISTS owner TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS task_id TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS change_origin TEXT DEFAULT 'chat_direct',
    ADD COLUMN IF NOT EXISTS git_status TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS git_branch TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS git_head_sha TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_workspace_change_ledger_file_owner
    ON chat_workspace_change_ledger (project, repo, file_path, status, updated_at DESC);

COMMENT ON COLUMN chat_workspace_change_ledger.owner IS
    'Dirty file owner label such as chat:<session>, runner:<job>, or manual:<operator>.';
COMMENT ON COLUMN chat_workspace_change_ledger.task_id IS
    'Directive/job/task id associated with this file change when available.';
COMMENT ON COLUMN chat_workspace_change_ledger.change_origin IS
    'Origin of detection: chat_direct, run_remote_command, git_status_snapshot, runner, etc.';
COMMENT ON COLUMN chat_workspace_change_ledger.git_status IS
    'Two-character git porcelain status captured when the file was detected dirty.';
COMMENT ON COLUMN chat_workspace_change_ledger.git_head_sha IS
    'Repository HEAD SHA at dirty detection time.';
