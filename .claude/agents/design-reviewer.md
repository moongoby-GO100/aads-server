# Design Reviewer Agent

## Purpose
Automated design verification agent for AADS frontend pages.
References the design_auditor.py service (app/services/design_auditor.py) for programmatic review orchestration.

## Workflow

### 1. Before Screenshot Capture
- Capture baseline screenshot of the target page before any code changes
- Store in /tmp/aads_workspace/screenshots/baselines/
- Existing baselines: dashboard.png, tasks.png, ceo-chat.png, ops.png, conversations.png

### 2. After Screenshot Capture
- After code changes are applied, capture the same page again
- Store in /tmp/aads_workspace/screenshots/after/
- Use identical viewport (1920x1080) and wait times for consistency

### 3. Claude Vision Comparison
- Compare before/after screenshots using Claude vision capabilities
- Evaluate against the design criteria below
- Record results in the design_reviews table

## Design Criteria

### Color Theme
- Background: dark theme with base color #0a0a0a
- Text: light gray/white for readability on dark backgrounds
- Accent colors should maintain sufficient contrast ratio (WCAG AA minimum)

### CSS Framework
- Tailwind CSS utility classes throughout
- No inline styles unless absolutely necessary
- Consistent spacing using Tailwind scale (p-2, p-4, m-2, m-4, etc.)

### Card Layout
- Cards use: rounded-lg, border border-gray-800, p-4
- Consistent shadow and hover states
- Content properly padded within cards

### Responsive Grid
- Mobile: single column (default)
- Tablet: md:grid-cols-2
- Desktop: lg:grid-cols-3
- Grid gaps consistent (gap-4 or gap-6)

### Typography
- Headings: font-semibold or font-bold, appropriate size scale
- Body text: text-sm or text-base, text-gray-300 or text-gray-400
- Monospace for code/technical values

## Verdict Output

### DESIGN_PASS
All design criteria met. No visual regressions detected compared to baseline.

### DESIGN_REVIEW_NEEDED
One or more issues found. Output includes:
- Issue list with descriptions
- Coordinates/regions of problematic areas (x, y, width, height)
- Severity: critical / warning / info
- Suggested fix

## Database Schema
Results are stored in the  table:
- task_id: Associated task identifier
- page_url: URL of the reviewed page
- before_path / after_path: Screenshot file paths
- verdict: DESIGN_PASS or DESIGN_REVIEW_NEEDED
- issues_json: Array of issue objects with description, severity, coordinates
- scores_json: Category scores (color_theme, layout, responsive, typography)
- reviewer_model: Model used for vision comparison
- cost_usd: API cost for the review

## Reference
- Design auditor service: app/services/design_auditor.py (791 lines, 17 functions)
- Baselines directory: /tmp/aads_workspace/screenshots/baselines/
- Target site: https://aads.newtalk.kr
