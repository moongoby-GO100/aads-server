# AADS Open Design Hub Phase 0 Implementation Plan

**TASK_ID**: AADS-204
**Status**: Phase 0 baseline
**Source plan**: `docs/plans/AADS-OPEN-DESIGN-HUB.md`
**Reference**: `docs/plans/AADS-SMART-DESIGN-SYSTEM.md`

## 1. Phase 0 Scope

Phase 0 is a read-only foundation for managing design quality across AADS-managed projects. It does not change dashboard styles, does not apply database migrations, and does not modify external project files.

| Area | Deliverable | Runtime Risk | Deploy Required |
|------|-------------|--------------|-----------------|
| Planning | Phase 1-4 execution breakdown | Low | No |
| Scanner PoC | `app/services/design_audit_service.py` read-only source scanner | Low | API reload only when exposed |
| Admin API | `GET /api/v1/admin/design/projects`, `GET /api/v1/admin/design/audit/preview` | Low | API reload only |
| Tests | Unit tests for scanner behavior and path guard | Low | No |

## 2. Current Phase 0 Contract

### Project Registry Preview

Endpoint:

```http
GET /api/v1/admin/design/projects
```

Response contract:

```json
{
  "projects": [
    {
      "project_key": "AADS",
      "display_name": "AADS",
      "repo_path": "/root/aads/aads-dashboard",
      "status": "draft",
      "read_only": true
    }
  ],
  "count": 6,
  "phase": "0"
}
```

### Audit Preview

Endpoint:

```http
GET /api/v1/admin/design/audit/preview?project_key=AADS&path=src&max_files=80
```

Scanner behavior:

- Detect raw hex colors.
- Detect `rgb()` and `rgba()` colors.
- Detect Tailwind arbitrary color classes.
- Detect emoji-like icon usage in source text.
- Detect repeated `<button className="...">` / `<button class="...">` patterns.
- Restrict scans to allowlisted project roots.
- Return bounded results without writing to the target project.

## 3. Phase 1: Registry And Token Schema

Goal: make project design state persistent without applying automatic design changes.

Files:

| File | Action |
|------|--------|
| `migrations/0xx_design_hub.sql` | Add draft schema only: `design_projects`, `design_tokens`, `design_audit_runs` |
| `app/services/design_registry_service.py` | Read/write service for registry metadata |
| `tests/unit/test_design_registry_service.py` | Schema-free service tests with mocked repository |

Acceptance checks:

```bash
python3 -m py_compile app/services/design_registry_service.py
pytest -q tests/unit/test_design_registry_service.py
git diff --check
```

Risk controls:

- Migration file may be committed, but the runner must not apply it to production DB.
- Registry writes must be explicit admin actions, not automatic scanner side effects.

## 4. Phase 2: Token Export Adapters

Goal: export a single token source into project-specific formats.

Adapters:

| Adapter | Output |
|---------|--------|
| `css-vars` | CSS custom properties |
| `tailwind-v4` | Tailwind v4 theme token snippet |
| `json` | Tool-neutral design token JSON |

Files:

| File | Action |
|------|--------|
| `app/services/design_token_exporter.py` | Pure token export functions |
| `tests/unit/test_design_token_exporter.py` | CSS, Tailwind, JSON export tests |

Acceptance checks:

```bash
python3 -m py_compile app/services/design_token_exporter.py
pytest -q tests/unit/test_design_token_exporter.py
git diff --check
```

Risk controls:

- Export only. Do not write into `aads-dashboard/`, GO100, KIS, SF, or NTV2 during this phase.
- Keep token names stable: `primary`, `accent`, `success`, `warning`, `danger`, `surface`, `text`.

## 5. Phase 3: Admin Design Hub UI

Goal: show registry, scanner summary, and token export previews in AADS dashboard.

Files:

| File | Action |
|------|--------|
| `/root/aads/aads-dashboard/src/...` | Add a contained admin view after API contract is stable |
| `/root/aads/aads-dashboard/src/lib/api.ts` | Add typed API client methods |

Acceptance checks:

```bash
npm run lint
git diff --check
```

Risk controls:

- Do not redesign the whole dashboard.
- Add a contained route/view only.
- Preserve existing theme and layout conventions until the shared design system is adopted deliberately.

## 6. Phase 4: Project Starter

Goal: generate starter design assets for new projects from a structured brief.

Inputs:

| Field | Example |
|-------|---------|
| Project type | internal ops dashboard, ecommerce, social app |
| Audience | CEO, operator, customer, admin |
| Brand tone | quiet/professional, high-contrast/data-heavy |
| Platform | Next.js/Tailwind, HTML/CSS, React SPA |

Outputs:

| Output | Description |
|--------|-------------|
| Token JSON | Canonical design tokens |
| CSS variables | Runtime theme |
| Component checklist | Required component set |
| Audit baseline | Initial scanner expectations |

Risk controls:

- Generated assets should be downloadable or preview-only first.
- Applying generated assets to a real project must be a separate approved runner task.

## 7. Operational Rules

- All scanner and preview endpoints are read-only.
- All filesystem scans must pass through allowlisted roots.
- No secret files are read.
- No production DB migration is applied by Phase 0.
- Any dashboard UI work must be separate from backend scanner work.
- Runner tasks should be split by phase and must include explicit file ownership.
