-- One review draft per auto-advanced milestone, with an indexed lookup.
CREATE UNIQUE INDEX IF NOT EXISTS ux_directive_drafts_milestone_auto_advance
    ON directive_drafts (tenant_id, (classification->>'milestone_id'))
    WHERE classification->>'source_mode' = 'milestone_auto_advance';
