-- Multiple explicit work items belong to the existing unique runner/goal link.
ALTER TABLE goal_task_links
    ADD COLUMN IF NOT EXISTS work_item_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
    ADD COLUMN IF NOT EXISTS work_items_synced_status TEXT;
