# SaaS User Access And Briefing Policy

Last updated: 2026-06-11 KST

## Objective

AADS must separate internal CEO/admin operations from customer tenant usage.
New and existing general users must not see the internal admin home, system operations, CEO decisions, global project status, or global user analytics.

## Access Model

| User class | Tenant start | Allowed first screen | Admin home | Briefing scope |
| --- | --- | --- | --- | --- |
| CEO/admin/system allowlist | `internal` | `/` or `/admin/*` | Allowed | CEO operations briefing |
| Customer owner/admin/member/viewer | `customer` | `/chat`, `/team`, `/onboarding` | Blocked | Organization briefing |

## P0 Rules

1. `internal` tenant access is restricted to CEO/admin/system allowlist users.
2. General users start from a `customer` tenant and are redirected away from internal admin routes.
3. Direct URL entry to admin-only dashboard routes must redirect general users to `/chat`.
4. Customer briefing must not include server health, CEO decisions, global project metrics, directive queue, error log, or global user analytics.

## P1 Rules

1. Team members are added through `tenant_memberships` with role-based permissions.
2. Team invitation and onboarding are customer-tenant workflows, not CEO workspace membership.
3. Signup onboarding must collect organization name and optional invitees with explicit roles.
4. Customer usage and plan status should be shown in tenant-scoped product screens.

## Current Implementation

- Backend `/api/v1/auth/me` returns `is_internal_admin`, current tenant, membership, tenant role, and user role.
- Backend `/api/v1/briefing` returns `scope=internal_admin` for internal admins and `scope=customer_tenant` for general users.
- Dashboard hides admin-only navigation for non-internal admins.
- Dashboard redirects non-internal admins from internal routes to `/chat`.
- Login redirects non-internal admins away from `/` and other internal routes.
