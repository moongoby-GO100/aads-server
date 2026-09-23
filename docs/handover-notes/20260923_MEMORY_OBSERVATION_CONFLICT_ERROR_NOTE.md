# Memory observation UPSERT/index mismatch — 2026-09-23

## Symptom

Release 5171 failed its five-minute P0/P1 monitor because
`memory_manager observe` logged `there is no unique or exclusion constraint
matching the ON CONFLICT specification`. The service catches the DB exception,
so individual chat requests can appear successful while AI observations are
not stored. The release monitor correctly treats the recurring error as a
deployment failure.

## Root cause

`MemoryManager.observe` used `ON CONFLICT (category, key)`. The live
`ai_observations` table instead has a unique expression index on
`(category, key, COALESCE(project, ''))`; `project` is nullable and defaults
to NULL. Other observation writers already target the project-aware index.
The error is a query/index contract mismatch, not a missing table migration.

## Correction and acceptance

Make this writer use the same three-expression conflict target. Do not add a
second two-column unique index: it would prevent distinct project-scoped
observations from coexisting. Add a query regression test and verify the
conflict target against the live DB with read-only `EXPLAIN` before rollout.
The corrected image must pass an observation UPSERT canary, AAG reads, the
five-minute P0/P1 monitor, and same-digest standby synchronization.

Read-only production `EXPLAIN` now resolves the corrected statement to
`Conflict Arbiter Indexes: idx_ai_observations_cat_key_proj`; the targeted
memory/SDK/model suite passed 82 tests before deployment.
