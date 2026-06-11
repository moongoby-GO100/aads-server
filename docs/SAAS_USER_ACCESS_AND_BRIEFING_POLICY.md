# SaaS User Access And Briefing Policy

## Purpose

General AADS users must experience the product through their own customer tenant.
CEO/admin/system users keep access to the internal tenant and AADS operating views.

## P0 Rules

- The `internal` tenant is limited to CEO/admin/system allowlisted principals.
- New and existing general users start in a `customer` tenant.
- Chat prompts for customer tenants must describe the user's own workspace, not AADS internal projects.
- System briefing for customer tenants must show tenant-scoped members, invites, sessions, artifacts, usage, and plan.
- Agenda and artifact panels must load items by current tenant and, where applicable, current session.
- Dashboard home and `/admin/*` views are internal-admin only.

## P1 Rules

- Team invitations create `tenant_memberships` with explicit roles: `admin`, `member`, or `viewer`.
- Onboarding should collect organization name, optional team invites, and role intent before the user starts work.
- First-login chat should provide usage guidance directly in the empty state and through a "사용법" quick action.
- General-user quick actions must avoid internal project names such as AADS, KIS, GO100, SF, NTV2, and NAS unless those are explicitly part of the user's tenant data.

## Current Implementation

- Backend briefing API branches by `is_internal_admin`; customer users receive `scope=customer_tenant`.
- Agenda API now requires tenant auth and hides global agenda lists from non-internal users unless a session filter is present.
- Chat system prompts receive a `<customer_tenant_scope>` guard for customer tenants.
- Dashboard sidebar hides admin-only links for non-internal users.
- Chat first screen and welcome chips are written as customer-workspace guidance.

## Completion Criteria

- Non-internal users cannot open dashboard home or admin routes from the UI.
- Non-internal chat briefing does not mention AADS operating tasks, CEO decisions, or internal project status.
- Artifact agenda tab only shows agendas tied to the current session.
- Asking "사용법 알려줘" returns first-use guidance for the user's own workspace.
