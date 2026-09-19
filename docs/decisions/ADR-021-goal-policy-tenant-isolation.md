# ADR-021: Goal policy stores use default-deny PostgreSQL RLS

- Status: accepted for W-12c
- Date: 2026-09-19
- Canonical inputs: PRD v1.2 §14, Goal Policy Foundation Interface v1

## Decision

The ten PRD 14.8 stores use PostgreSQL row-level security. Every policy compares
`tenant_id` with the transaction-local `app.current_tenant_id` UUID. Missing,
empty, or malformed context resolves to NULL and therefore reads return no rows
and mutations fail. All ten tables use both `ENABLE ROW LEVEL SECURITY` and
`FORCE ROW LEVEL SECURITY`.

The migration identity and runtime identity must be separate. The runtime role
must be `NOSUPERUSER NOBYPASSRLS`, must not own these tables, and receives only
the statement privileges needed by its repository. Startup/release verification
must reject `rolsuper` or `rolbypassrls` for the runtime identity. The migration
role is not used by the API or workers.

Repositories must open a transaction and execute the parameterized equivalent
of `SELECT set_config('app.current_tenant_id', tenant_uuid, true)` before the
first governed-store statement. Context must never be session-scoped. Background
workers derive the tenant from the claimed envelope and set it inside the same
transaction; a cross-tenant claim is not supported.

`app.current_tenant_id` is request scope, not authentication. PostgreSQL custom
settings are caller-settable, so the repository must derive this UUID only from
the trusted server-side authentication/claim envelope. It must never accept a
tenant UUID from a query, request body, tool argument, or model output. RLS
protects the trusted repository path from omitted predicates; it is not a
sandbox for a compromised runtime identity. Least-privilege statement grants,
separate service identities, and prevention of arbitrary SQL remain mandatory.

## Measured current state

At this decision point, `app/core/db_pool.py` creates a shared asyncpg pool and
the goal routers acquire raw pooled connections. The two existing services use
explicit tenant predicates, but no shared pool hook sets `app.current_tenant_id`.
The M12/M14 tables also had no RLS policies. Consequently, direct SQL under the
runtime role could omit the predicate, and pooled session context would be unsafe.
W-13/W-14F repository work is therefore gated on transaction-local context; the
new stores fail closed until that contract is used.

## Why RLS

Composite foreign keys prevent cross-tenant relationships but cannot prevent a
valid SQL role from reading another tenant's independent rows. Repository-only
enforcement likewise cannot cover a missed predicate. RLS supplies a
database-enforced row boundary after trusted tenant binding, while composite
keys continue to protect object relationships. It does not make ad-hoc SQL safe
because ad-hoc SQL can change a custom setting.

## Compatibility and rollback

The migration is additive. Existing tables and legacy rows are retained. The
accepted M14 outbox writer remains compatible because its payload hash, sequence,
and publish state are derived for new legacy writes. Legacy evidence and
dependency rows remain readable; new writes must provide the hardened fields.
The overlapping M14 kill-switch and decision-log tables remain untouched for
historical readers. Security-invoker compatibility views expose canonical data
in legacy read shapes; new writes target only `goal_kill_switches` and
`goal_policy_decisions`. There is no dual-write or silent claim that the
differently shaped legacy tables satisfy the canonical contract.

Rollback is application-first and non-destructive: revert readers/writers while
retaining tables, policies, ledgers, and audit data. RLS is never disabled by the
rollback file. A future retirement requires separate approval and archival proof.

## Verification

The disposable PostgreSQL suite applies M12, M14, and this migration twice; uses
a `NOSUPERUSER NOBYPASSRLS` role; verifies missing-context and cross-tenant reads
and mutations; races active-scope inserts; checks append-only ledgers; and executes
the non-destructive rollback metadata transaction. Superuser-only results are not
accepted as tenant-isolation evidence because PostgreSQL superusers bypass RLS.

## Residual risks and gates

- A misconfigured superuser or `BYPASSRLS` runtime identity defeats RLS; release
  role verification is mandatory.
- A compromised runtime identity or arbitrary-SQL capability can change the
  custom tenant setting. Service authentication and SQL capability controls are
  therefore part of the boundary.
- Tables outside the ten-store boundary retain their existing isolation model.
- Existing M14 writers need transaction-local tenant context before this migration
  is activated for runtime traffic.
- Foreign-key checks do not replace RLS and may reveal integrity failure classes
  internally; external APIs must continue mapping cross-tenant existence to 404.
