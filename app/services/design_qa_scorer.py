"""Static QA scoring for design modification requests."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.db_pool import get_pool
from app.services import design_audit_service

SCORING_VERSION = "static-v1"

_TOKEN_SCAN_SUFFIXES = design_audit_service.DEFAULT_INCLUDE_SUFFIXES | {
    ".css",
    ".less",
    ".py",
    ".scss",
}
_FILE_COLLECTION_KEYS = {
    "changed_files",
    "component_path",
    "component_paths",
    "file_path",
    "file_paths",
    "source_files",
    "target_files",
}
_VIEWPORT_FONT_SCALING_RE = re.compile(
    r"(?:font-size\s*:\s*[^;}{\n]*(?:vw|vh|vmin|vmax)\b|fontSize\s*:\s*[\"'][^\"']*(?:vw|vh|vmin|vmax)\b[^\"']*[\"']|text-\[[^\]]*(?:vw|vh|vmin|vmax)[^\]]*\])",
    re.IGNORECASE,
)
_ARIA_HINT_RE = re.compile(r"\baria-[\w-]+\s*=", re.IGNORECASE)
_ALT_HINT_RE = re.compile(r"\balt\s*=", re.IGNORECASE)
_FOCUS_HINT_RE = re.compile(r"\bfocus(?:-visible)?[:=]", re.IGNORECASE)


class DesignModificationRequestNotFoundError(LookupError):
    """Raised when a design modification request does not exist."""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _line_column(content: str, index: int) -> tuple[int, int]:
    line = content.count("\n", 0, index) + 1
    last_newline = content.rfind("\n", 0, index)
    column = index + 1 if last_newline < 0 else index - last_newline
    return line, column


def _line_context(content: str, index: int, limit: int = 160) -> str:
    start = content.rfind("\n", 0, index) + 1
    end = content.find("\n", index)
    if end < 0:
        end = len(content)
    return content[start:end].strip()[:limit]


def _axis(score: int, max_score: int, notes: list[str]) -> dict[str, Any]:
    bounded = max(0, min(int(score), max_score))
    return {
        "score": bounded,
        "max_score": max_score,
        "notes": notes,
    }


def _score_rating(total_score: int) -> str:
    if total_score >= 90:
        return "approval_candidate"
    if total_score >= 80:
        return "conditional_approval"
    if total_score >= 70:
        return "needs_revision"
    return "rejected"


def _is_under_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    for root in design_audit_service.ALLOWED_PROJECT_ROOTS.values():
        resolved_root = root.resolve()
        if _is_under_root(resolved, resolved_root):
            return resolved.relative_to(resolved_root).as_posix()
    return resolved.as_posix()


def _looks_like_source_file(value: str) -> bool:
    text = (value or "").strip()
    if not text or text.startswith(("http://", "https://")):
        return False
    return Path(text).suffix.lower() in _TOKEN_SCAN_SUFFIXES


def _collect_candidate_paths(value: Any, collected: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FILE_COLLECTION_KEYS:
                _collect_candidate_paths(item, collected)
                continue
            if isinstance(item, (dict, list)):
                _collect_candidate_paths(item, collected)
            elif isinstance(item, str) and key.endswith("_file") and _looks_like_source_file(item):
                collected.append(item)
        return
    if isinstance(value, list):
        for item in value:
            _collect_candidate_paths(item, collected)
        return
    if isinstance(value, str) and _looks_like_source_file(value):
        collected.append(value)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = (value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _project_roots(project_key: str) -> list[Path]:
    normalized = (project_key or "").strip().upper()
    roots: list[Path] = []
    for key in (normalized, f"{normalized}_SERVER"):
        root = design_audit_service.ALLOWED_PROJECT_ROOTS.get(key)
        if root is not None:
            roots.append(root.resolve())
    if roots:
        return roots
    return [root.resolve() for root in design_audit_service.ALLOWED_PROJECT_ROOTS.values()]


def _resolve_scan_path(raw_path: str) -> Path | None:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        absolute_path = candidate.resolve()
        allowed_roots = [root.resolve() for root in design_audit_service.ALLOWED_PROJECT_ROOTS.values()]
        if any(_is_under_root(absolute_path, root) for root in allowed_roots) and absolute_path.is_file():
            return absolute_path
        if absolute_path.is_file():
            return absolute_path
        return None

    cwd_candidate = (Path.cwd() / candidate).resolve()
    if cwd_candidate.is_file():
        return cwd_candidate

    for root in design_audit_service.ALLOWED_PROJECT_ROOTS.values():
        absolute_path = (root.resolve() / candidate).resolve()
        if _is_under_root(absolute_path, root.resolve()) and absolute_path.is_file():
            return absolute_path
    return None


def _resolve_project_file_paths(project_key: str, file_paths: list[str]) -> tuple[list[str], list[str]]:
    resolved_paths: list[str] = []
    missing_files: list[str] = []
    roots = _project_roots(project_key)

    for raw_path in _dedupe_strings(file_paths):
        candidate = Path(raw_path)
        if candidate.is_absolute():
            absolute_path = candidate.resolve()
            if any(_is_under_root(absolute_path, root) for root in roots) and absolute_path.is_file():
                resolved_paths.append(absolute_path.as_posix())
            else:
                missing_files.append(raw_path)
            continue

        matched = False
        for root in roots:
            absolute_path = (root / candidate).resolve()
            if not _is_under_root(absolute_path, root):
                continue
            if absolute_path.is_file():
                resolved_paths.append(absolute_path.as_posix())
                matched = True
                break
        if not matched:
            missing_files.append(raw_path)

    return _dedupe_strings(resolved_paths), missing_files


def _build_violation(
    *,
    kind: str,
    file_path: str,
    value: str,
    line: int | None,
    column: int | None,
    context: str | None,
    message: str,
    count: int | None = None,
    files: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "file_path": file_path,
        "value": value,
        "line": line,
        "column": column,
        "context": context,
        "message": message,
        "count": count,
        "files": files or [],
    }


def check_token_compliance(file_paths: list[str]) -> dict[str, Any]:
    """Scan source files for token and design-system compliance issues."""
    violations: list[dict[str, Any]] = []
    repeated_button_counts: Counter[str] = Counter()
    repeated_button_files: dict[str, set[str]] = {}
    scanned_file_paths: list[str] = []
    missing_files: list[str] = []

    for raw_path in _dedupe_strings(file_paths):
        source_path = _resolve_scan_path(raw_path)
        if source_path is None:
            missing_files.append(raw_path)
            continue

        try:
            content = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            missing_files.append(raw_path)
            continue

        display_path = _display_path(source_path)
        scanned_file_paths.append(display_path)

        for finding in design_audit_service.audit_source_text(content, display_path):
            if finding.kind == "raw_hex_color":
                violations.append(
                    _build_violation(
                        kind="raw_hex_color",
                        file_path=finding.file_path,
                        value=finding.value,
                        line=finding.line,
                        column=finding.column,
                        context=finding.context,
                        message="Raw hex color detected. Prefer existing design tokens or CSS variables.",
                    )
                )
            elif finding.kind == "emoji_icon":
                violations.append(
                    _build_violation(
                        kind="emoji_icon",
                        file_path=finding.file_path,
                        value=finding.value,
                        line=finding.line,
                        column=finding.column,
                        context=finding.context,
                        message="Emoji icon detected. Prefer a shared icon component instead.",
                    )
                )

        for match in _VIEWPORT_FONT_SCALING_RE.finditer(content):
            line, column = _line_column(content, match.start())
            violations.append(
                _build_violation(
                    kind="viewport_font_scaling",
                    file_path=display_path,
                    value=match.group(0),
                    line=line,
                    column=column,
                    context=_line_context(content, match.start()),
                    message="Viewport-based font scaling detected. Prefer tokenized typography scales.",
                )
            )

        for pattern in design_audit_service.extract_button_class_patterns(content, display_path):
            repeated_button_counts[pattern.classes] += pattern.count
            repeated_button_files.setdefault(pattern.classes, set()).update(pattern.files)

    for classes, count in repeated_button_counts.items():
        if count < 3:
            continue
        files = sorted(repeated_button_files.get(classes, set()))
        violations.append(
            _build_violation(
                kind="repeated_button_pattern",
                file_path=files[0] if len(files) == 1 else "<multiple>",
                value=classes,
                line=None,
                column=None,
                context=None,
                message="Repeated button class pattern detected. Consider a shared button component.",
                count=count,
                files=files,
            )
        )

    by_kind = Counter(item["kind"] for item in violations)
    return {
        "compliant": not violations and not missing_files,
        "files_scanned": len(scanned_file_paths),
        "scanned_file_paths": scanned_file_paths,
        "missing_files": missing_files,
        "summary": {
            "total_violations": len(violations),
            "by_kind": dict(sorted(by_kind.items())),
        },
        "violations": violations,
    }


def _scan_accessibility_hints(file_paths: list[str]) -> dict[str, int]:
    metrics = {
        "aria_attributes": 0,
        "alt_text": 0,
        "focus_styles": 0,
    }
    for raw_path in _dedupe_strings(file_paths):
        source_path = _resolve_scan_path(raw_path)
        if source_path is None:
            continue
        try:
            content = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        metrics["aria_attributes"] += len(_ARIA_HINT_RE.findall(content))
        metrics["alt_text"] += len(_ALT_HINT_RE.findall(content))
        metrics["focus_styles"] += len(_FOCUS_HINT_RE.findall(content))
    return metrics


def _extract_candidate_file_paths(
    request_row: dict[str, Any],
    latest_context_pack: dict[str, Any],
) -> list[str]:
    candidates: list[str] = []
    candidates.extend(str(item) for item in _as_list(request_row.get("screen_component_paths")) if isinstance(item, str))
    _collect_candidate_paths(_as_dict(request_row.get("allowed_scope")), candidates)
    _collect_candidate_paths(_as_dict(latest_context_pack.get("context")), candidates)
    return _dedupe_strings(candidates)


def _parse_viewport_bucket(viewport: str) -> str | None:
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", viewport or "", re.IGNORECASE)
    if not match:
        return None
    width = int(match.group(1))
    if width <= 480:
        return "mobile"
    if width <= 1024:
        return "tablet"
    return "desktop"


def _dom_richness(dom_summary: Any) -> int:
    if isinstance(dom_summary, dict):
        return len([key for key, value in dom_summary.items() if value not in (None, "", [], {})])
    if isinstance(dom_summary, list):
        return len([item for item in dom_summary if item not in (None, "", [], {})])
    return 1 if dom_summary else 0


def _score_request_match(
    request_row: dict[str, Any],
    snapshots: list[dict[str, Any]],
    resolved_paths: list[str],
) -> dict[str, Any]:
    notes: list[str] = []
    score = 0

    criteria_count = len(_as_list(request_row.get("acceptance_criteria")))
    if criteria_count:
        gained = min(10, 4 + criteria_count * 3)
        score += gained
        notes.append(f"{criteria_count} acceptance criteria are attached to the request.")
    elif _as_dict(request_row.get("normalized_card")):
        score += 4
        notes.append("Normalized request card metadata is available.")
    else:
        notes.append("No acceptance criteria or normalized request card metadata is attached.")

    after_snapshots = [item for item in snapshots if str(item.get("phase") or "") == "after"]
    if after_snapshots:
        score += 8
        notes.append(f"{len(after_snapshots)} after snapshot(s) are available for review.")
    else:
        notes.append("No after snapshot is attached yet.")

    if resolved_paths:
        gained = min(7, 3 + len(resolved_paths) * 2)
        score += gained
        notes.append(f"{len(resolved_paths)} implementation file(s) were mapped from request context.")
    else:
        notes.append("No implementation files could be mapped from the request context.")

    return _axis(score, 25, notes)


def _score_context_retention(
    request_row: dict[str, Any],
    latest_context_pack: dict[str, Any],
    snapshots: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    notes: list[str] = []
    score = 0

    if request_row.get("screen_id"):
        score += 4
        notes.append("The request is linked to a concrete screen.")
    else:
        notes.append("The request is not linked to a concrete screen record.")

    source_count = len(_as_list(latest_context_pack.get("sources")))
    if source_count:
        score += min(8, source_count * 2)
        notes.append(f"The latest context pack carries {source_count} explicit source(s).")
    else:
        notes.append("No explicit context-pack sources were attached.")

    decision_count = len(decisions)
    if decision_count:
        score += min(4, decision_count * 2)
        notes.append(f"{decision_count} design decision reference(s) are available.")
    else:
        notes.append("No related design decision reference was found.")

    before_snapshots = [item for item in snapshots if str(item.get("phase") or "") == "before"]
    if before_snapshots:
        score += 4
        notes.append("Before-state snapshots are available to preserve baseline context.")
    else:
        notes.append("No before-state snapshot is attached.")

    missing_context_count = len(_as_list(latest_context_pack.get("missing_context")))
    if missing_context_count:
        score = max(0, score - min(6, missing_context_count * 2))
        notes.append(f"The latest context pack still reports {missing_context_count} missing context item(s).")
    else:
        notes.append("The latest context pack reports no missing context items.")

    return _axis(score, 20, notes)


def _score_visual_completeness(
    snapshots: list[dict[str, Any]],
    resolved_paths: list[str],
    compliance_report: dict[str, Any],
) -> dict[str, Any]:
    notes: list[str] = []
    score = 0

    after_snapshots = [item for item in snapshots if str(item.get("phase") or "") == "after"]
    if after_snapshots:
        score += 10
        notes.append("After-state screenshots are available for visual review.")
    elif snapshots:
        score += 4
        notes.append("Only non-after screenshots are attached, so completion evidence is partial.")
    else:
        notes.append("No screenshots are attached to the request.")

    richest_dom_summary = max((_dom_richness(item.get("dom_summary")) for item in snapshots), default=0)
    if richest_dom_summary:
        score += min(6, 2 + richest_dom_summary)
        notes.append("Screenshot metadata includes structured DOM summary evidence.")
    else:
        notes.append("No structured DOM summary metadata is attached to screenshots.")

    if resolved_paths:
        score += 4
        notes.append("Source files are available for static completeness review.")
    else:
        notes.append("No source files were available for static completeness review.")

    compliance_counts = Counter(_as_dict(compliance_report.get("summary")).get("by_kind", {}))
    penalty = min(
        4,
        int(compliance_counts.get("raw_hex_color", 0)) + int(compliance_counts.get("emoji_icon", 0)),
    )
    if penalty:
        score = max(0, score - penalty)
        notes.append("Token and icon violations lowered the visual completeness score.")

    return _axis(score, 20, notes)


def _score_responsive_stability(
    snapshots: list[dict[str, Any]],
    compliance_report: dict[str, Any],
) -> dict[str, Any]:
    notes: list[str] = []
    score = 0

    viewport_buckets = {
        bucket
        for bucket in (_parse_viewport_bucket(str(item.get("viewport") or "")) for item in snapshots)
        if bucket is not None
    }
    if "mobile" in viewport_buckets:
        score += 5
    if "tablet" in viewport_buckets:
        score += 4
    if "desktop" in viewport_buckets:
        score += 4

    if viewport_buckets:
        notes.append(f"Viewport coverage includes {', '.join(sorted(viewport_buckets))}.")
    else:
        notes.append("No parseable viewport snapshot metadata is attached.")

    compliance_counts = Counter(_as_dict(compliance_report.get("summary")).get("by_kind", {}))
    viewport_font_scaling = int(compliance_counts.get("viewport_font_scaling", 0))
    if viewport_font_scaling:
        score = max(0, score - min(4, viewport_font_scaling * 2))
        notes.append(f"Viewport-based font scaling was detected {viewport_font_scaling} time(s).")
    elif viewport_buckets:
        score = min(15, score + 2)
        notes.append("No viewport-based font scaling pattern was detected.")

    return _axis(score, 15, notes)


def _score_accessibility(
    accessibility_metrics: dict[str, int],
    resolved_paths: list[str],
    compliance_report: dict[str, Any],
) -> dict[str, Any]:
    notes: list[str] = []
    score = 0

    focus_styles = int(accessibility_metrics.get("focus_styles", 0))
    if focus_styles:
        score += 4
        notes.append(f"{focus_styles} focus or focus-visible style hint(s) were detected.")
    elif resolved_paths:
        score += 1
        notes.append("No explicit focus style hint was detected in scanned files.")
    else:
        notes.append("No source files were available to inspect focus states.")

    aria_attributes = int(accessibility_metrics.get("aria_attributes", 0))
    alt_text = int(accessibility_metrics.get("alt_text", 0))
    if aria_attributes:
        score += 2
        notes.append(f"{aria_attributes} aria attribute hint(s) were detected.")
    else:
        notes.append("No aria attribute hint was detected.")

    if alt_text:
        score += 2
        notes.append(f"{alt_text} alt text hint(s) were detected.")
    else:
        notes.append("No alt text hint was detected.")

    compliance_counts = Counter(_as_dict(compliance_report.get("summary")).get("by_kind", {}))
    emoji_count = int(compliance_counts.get("emoji_icon", 0))
    if emoji_count:
        score = max(0, score - min(4, emoji_count * 2))
        notes.append(f"Emoji-based icons were detected {emoji_count} time(s).")
    else:
        score = min(10, score + 2)
        notes.append("No emoji-based icon usage was detected.")

    return _axis(score, 10, notes)


def _score_technical_stability(
    compliance_report: dict[str, Any],
    resolved_paths: list[str],
) -> dict[str, Any]:
    notes: list[str] = ["Static-only technical review; runtime lint, build, and browser checks are out of scope here."]
    score = 10

    compliance_counts = Counter(_as_dict(compliance_report.get("summary")).get("by_kind", {}))
    raw_hex_count = int(compliance_counts.get("raw_hex_color", 0))
    repeated_button_count = int(compliance_counts.get("repeated_button_pattern", 0))
    missing_files = len(_as_list(compliance_report.get("missing_files")))

    if raw_hex_count:
        score -= min(3, raw_hex_count)
        notes.append(f"Raw hex colors were detected {raw_hex_count} time(s).")
    if repeated_button_count:
        score -= min(3, repeated_button_count * 2)
        notes.append(f"Repeated button patterns were detected {repeated_button_count} time(s).")
    if missing_files:
        score -= min(2, missing_files)
        notes.append(f"{missing_files} candidate file(s) could not be resolved for scanning.")
    if not resolved_paths:
        score -= 2
        notes.append("No source files were scanned for technical review.")
    if score == 10:
        notes.append("The static token-compliance pass found no technical hygiene regressions.")

    return _axis(score, 10, notes)


async def score_modification(request_id: UUID) -> dict[str, Any]:
    """Compute and persist a static QA score for a design modification request."""
    pool = get_pool()
    async with pool.acquire() as conn:
        request_row = await conn.fetchrow(
            """
            SELECT r.id, r.project_key, r.screen_id, r.user_prompt, r.normalized_card,
                   r.request_type, r.allowed_scope, r.forbidden_scope, r.acceptance_criteria,
                   r.status, r.created_at, r.updated_at,
                   s.route AS screen_route, s.name AS screen_name, s.purpose AS screen_purpose,
                   s.component_paths AS screen_component_paths, s.metadata AS screen_metadata
            FROM design_modification_requests r
            LEFT JOIN design_screens s
                ON s.id = r.screen_id
            WHERE r.id = $1::uuid
            """,
            request_id,
        )
        if request_row is None:
            raise DesignModificationRequestNotFoundError("design modification request not found")

        latest_context_pack = await conn.fetchrow(
            """
            SELECT id, context, sources, missing_context, prompt_chars, created_at
            FROM design_context_packs
            WHERE request_id = $1::uuid
            ORDER BY created_at DESC
            LIMIT 1
            """,
            request_id,
        )
        snapshot_rows = await conn.fetch(
            """
            SELECT id, request_id, phase, viewport, image_url, dom_summary, captured_at
            FROM design_visual_snapshots
            WHERE request_id = $1::uuid
            ORDER BY captured_at DESC
            LIMIT 32
            """,
            request_id,
        )
        decision_rows = await conn.fetch(
            """
            SELECT id, applies_to, metadata
            FROM design_decisions
            WHERE project_key = $1
              AND (screen_id = $2::uuid OR screen_id IS NULL)
            ORDER BY created_at DESC
            LIMIT 24
            """,
            request_row["project_key"],
            request_row["screen_id"],
        )

        request_payload = dict(request_row)
        context_payload = dict(latest_context_pack) if latest_context_pack is not None else {}
        snapshots = [dict(item) for item in snapshot_rows]
        decisions = [dict(item) for item in decision_rows]

        candidate_file_paths = _extract_candidate_file_paths(request_payload, context_payload)
        resolved_file_paths, unresolved_file_paths = _resolve_project_file_paths(
            str(request_payload.get("project_key") or ""),
            candidate_file_paths,
        )
        token_compliance = check_token_compliance(resolved_file_paths)
        if unresolved_file_paths:
            token_compliance["missing_files"] = _dedupe_strings(
                _as_list(token_compliance.get("missing_files")) + unresolved_file_paths
            )
            token_compliance["compliant"] = False

        accessibility_metrics = _scan_accessibility_hints(resolved_file_paths)
        axes = {
            "request_match": _score_request_match(request_payload, snapshots, resolved_file_paths),
            "context_retention": _score_context_retention(request_payload, context_payload, snapshots, decisions),
            "visual_completeness": _score_visual_completeness(snapshots, resolved_file_paths, token_compliance),
            "responsive_stability": _score_responsive_stability(snapshots, token_compliance),
            "accessibility": _score_accessibility(accessibility_metrics, resolved_file_paths, token_compliance),
            "technical_stability": _score_technical_stability(token_compliance, resolved_file_paths),
        }
        total_score = sum(int(axis["score"]) for axis in axes.values())
        rating = _score_rating(total_score)
        scored_at = datetime.now(timezone.utc)

        evidence = {
            "static_only": True,
            "request_type": str(request_payload.get("request_type") or "other"),
            "status": str(request_payload.get("status") or "draft"),
            "screen_route": request_payload.get("screen_route"),
            "candidate_file_paths": candidate_file_paths,
            "resolved_file_paths": [_display_path(Path(item)) for item in resolved_file_paths],
            "missing_file_paths": token_compliance.get("missing_files") or [],
            "snapshot_viewports": _dedupe_strings(
                [str(item.get("viewport") or "") for item in snapshots if str(item.get("viewport") or "").strip()]
            ),
            "snapshot_phases": _dedupe_strings([str(item.get("phase") or "") for item in snapshots]),
            "acceptance_criteria_count": len(_as_list(request_payload.get("acceptance_criteria"))),
            "context_source_count": len(_as_list(context_payload.get("sources"))),
            "missing_context_count": len(_as_list(context_payload.get("missing_context"))),
            "decision_count": len(decisions),
            "accessibility_hints": accessibility_metrics,
            "latest_context_pack_id": str(context_payload["id"]) if context_payload.get("id") else None,
            "latest_context_pack_created_at": (
                context_payload["created_at"].isoformat() if context_payload.get("created_at") else None
            ),
        }
        score_details = {
            "scoring_version": SCORING_VERSION,
            "rating": rating,
            "axes": axes,
        }

        await conn.execute(
            """
            INSERT INTO design_qa_scores (
                request_id,
                scoring_version,
                total_score,
                request_match_score,
                context_retention_score,
                visual_completeness_score,
                responsive_stability_score,
                accessibility_score,
                technical_stability_score,
                score_details,
                token_compliance,
                evidence,
                created_at,
                updated_at
            )
            VALUES (
                $1::uuid,
                $2,
                $3,
                $4,
                $5,
                $6,
                $7,
                $8,
                $9,
                $10::jsonb,
                $11::jsonb,
                $12::jsonb,
                NOW(),
                NOW()
            )
            ON CONFLICT (request_id) DO UPDATE SET
                scoring_version = EXCLUDED.scoring_version,
                total_score = EXCLUDED.total_score,
                request_match_score = EXCLUDED.request_match_score,
                context_retention_score = EXCLUDED.context_retention_score,
                visual_completeness_score = EXCLUDED.visual_completeness_score,
                responsive_stability_score = EXCLUDED.responsive_stability_score,
                accessibility_score = EXCLUDED.accessibility_score,
                technical_stability_score = EXCLUDED.technical_stability_score,
                score_details = EXCLUDED.score_details,
                token_compliance = EXCLUDED.token_compliance,
                evidence = EXCLUDED.evidence,
                updated_at = NOW()
            """,
            request_id,
            SCORING_VERSION,
            total_score,
            axes["request_match"]["score"],
            axes["context_retention"]["score"],
            axes["visual_completeness"]["score"],
            axes["responsive_stability"]["score"],
            axes["accessibility"]["score"],
            axes["technical_stability"]["score"],
            json.dumps(score_details, ensure_ascii=False),
            json.dumps(token_compliance, ensure_ascii=False),
            json.dumps(evidence, ensure_ascii=False),
        )

    return {
        "request_id": str(request_id),
        "project_key": str(request_payload.get("project_key") or ""),
        "scoring_version": SCORING_VERSION,
        "total_score": total_score,
        "rating": rating,
        "axes": axes,
        "token_compliance": token_compliance,
        "evidence": evidence,
        "scored_at": scored_at,
    }
