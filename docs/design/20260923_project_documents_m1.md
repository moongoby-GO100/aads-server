# Project document canonical ledger M1

The canonical key is `(tenant_id, project_key, document_key)`. `project_document_heads` owns the latest and approved pointers; `project_document_revisions` holds immutable UTF-8 snapshots and SHA-256 hashes. A draft or review revision advances only `latest_revision_id`. Approval advances `approved_revision_id` inside the same transaction as its audit event. Archive clears the approved pointer and retains every revision. `generation` is the optimistic concurrency token and changes on revision, review, approval, and archive. A retry with identical content returns the existing revision. A reused version with different content, reused idempotency key with different content, or stale generation returns 409.

## API contract

All paths are under `/api/v1/projects/{project_key}/documents`. The project key is a normalized uppercase identifier, not a URL or filesystem path. Authenticated tenant admins and owners can access their tenant's projects. Other tenant users require a row in `project_document_grants` for the exact tenant, project, user, and access (`read`, `write`, or `approve`). Grant provisioning is an administrative database action; M1 does not expose a grant mutation API. A viewer role is required for reads and a member role for mutations in addition to the project grant.

| Method and suffix | Purpose |
| --- | --- |
| `POST /` | Create a draft revision. Requires `expected_generation` (0 for a new document), `document_key`, `kind`, `title`, semantic `version`, and `content` or a verified repository `source_path`. Optional `goal_id`, `source_session_id`, `source_task_id`, `idempotency_key`, `change_summary`. |
| `GET /?q=&kind=&approved_only=&limit=` | Tenant and project scoped latest revision list and search. `approved_only=true` searches approved revision titles and bodies only. |
| `GET /{document_key}?approved_only=` | Latest or approved detail; `authoritative` is true only when the returned revision is the current approved pointer. |
| `GET /{document_key}/history` | Immutable revision metadata and current derived status. |
| `POST /{document_key}/review` | Mark latest revision in review with `{revision_id, expected_generation}`. |
| `POST /{document_key}/approve` | Move approved pointer to latest revision with `{revision_id, expected_generation}`. Requires approve grant. |
| `POST /{document_key}/archive` | Clear approved pointer for the specified approved revision. No delete. |
| `POST /{document_key}/goals` | Connect a canonical document to an existing goal in the same tenant and project. |
| `GET /for-goal/{goal_id}?approved_only=true` | Read linked canonical documents; approved only by default. Legacy `goal_documents` remains independent. |
| `POST /{document_key}/legacy-links` | Register `{revision_id, goal_document_id}` after matching tenant, project, goal, kind, version, repository path, and SHA-256 content. The legacy row and file are untouched. |
| `GET /{document_key}/legacy-links` | Read bounded, verified legacy row references for the canonical document. |
| `GET /brief/approved` | Bounded, read-only, 8 document brief. Each excerpt is at most 4,000 characters. Empty result is not authoritative. |
| `GET /inventory/legacy` | Read-only, 100-row bounded `goal_documents` mapping candidates with checked repository paths and hashes, plus scoped artifact counts. No registration or legacy writes. |

Document body maximum is 262,144 UTF-8 bytes. A repository source path must start in `docs/` or `reports/`, exist beneath this checkout, be UTF-8 text, and resolve without escaping via symlink. The API snapshots the verified source bytes into the revision and rejects mismatched supplied content. Credential patterns in content and identifying metadata are rejected. Audits record action, actor, scope, and revision ID; they do not copy the body.

The migration is additive. It neither migrates nor overwrites `goal_documents`, `project_artifacts`, `chat_artifacts`, or their files. Inventory candidates require human mapping review, including identity, kind, version, and hash. The staged migration merges M1 status metadata into handover entry `aads-project-documents-canonical-20260923` while preserving its existing design body when approved for execution; no operational database was changed while preparing M1.

## M2 integration

After the DB-map review is resolved, session and runner code can call `approved_brief(conn, tenant_id, project_key, limit)` from `app.api.canonical_documents`. The caller must derive tenant and project from authenticated execution context and must never pass user-supplied scope without authorization. It returns only approved revisions and a bounded excerpt. Empty or unapproved state must be presented as missing, not as a canonical answer. M2 will modify the overlapping context builder and runner files; M1 leaves them untouched.
