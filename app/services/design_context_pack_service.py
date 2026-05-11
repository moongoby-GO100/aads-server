"""Bounded Design Context Pack builder for design modification requests."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Sequence


PACK_VERSION = "design-context-pack/v1"
MAX_STRING_CHARS = 1200
MAX_COLLECTION_ITEMS = 40
MAX_DICT_ITEMS = 80
MAX_DEPTH = 8
MAX_CONTEXT_CHARS = 32000

_SAFE_DESIGN_TOKEN_KEYS = {
    "designtoken",
    "designtokens",
    "styletoken",
    "styletokens",
    "colortoken",
    "colortokens",
    "spacingtoken",
    "spacingtokens",
    "typographytoken",
    "typographytokens",
    "tokenevidence",
}
_PATH_KEYS = {"path", "file", "filepath", "sourcepath", "relativepath", "componentpath"}
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "credential",
    "secret",
    "apikey",
    "privatekey",
    "clientsecret",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "authorization",
    "cookie",
    "sessioncookie",
)
_SECRET_VALUE_RE = re.compile(
    r"(?is)("
    r"-----BEGIN\s+(?:RSA\s+|OPENSSH\s+|EC\s+)?PRIVATE\s+KEY-----"
    r"|bearer\s+[a-z0-9._~+/=-]{20,}"
    r"|sk-[a-z0-9][a-z0-9._~+/=-]{18,}"
    r"|(?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|credential)"
    r"\s*[:=]\s*[\"']?[^\s\"',;]{6,}"
    r")"
)


@dataclass
class SafetyReport:
    redacted_paths: list[str] = field(default_factory=list)
    truncated_paths: list[str] = field(default_factory=list)
    omitted_paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "redacted_count": len(self.redacted_paths),
            "truncated_count": len(self.truncated_paths),
            "omitted_count": len(self.omitted_paths),
            "redacted_paths": self.redacted_paths[:40],
            "truncated_paths": self.truncated_paths[:40],
            "omitted_paths": self.omitted_paths[:40],
        }


@dataclass(frozen=True)
class DesignContextPackBuildResult:
    context: dict[str, Any]
    sources: list[dict[str, Any]]
    missing_context: list[dict[str, Any]]
    prompt_chars: int
    safety_report: dict[str, Any]


def _key_norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def _is_sensitive_key(key: Any) -> bool:
    normalized = _key_norm(key)
    if normalized in _SAFE_DESIGN_TOKEN_KEYS:
        return False
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _is_disallowed_path(value: str) -> bool:
    lowered = (value or "").strip().replace("\\", "/").lower()
    if not lowered:
        return False
    parts = [part for part in lowered.split("/") if part]
    basename = parts[-1] if parts else lowered
    if basename.startswith(".env") or basename in {"credentials", "secrets"}:
        return True
    return any(part in {"credentials", "secrets"} for part in parts)


def _looks_sensitive_string(value: str) -> bool:
    return bool(_SECRET_VALUE_RE.search(value or ""))


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default))


def _sanitize(value: Any, report: SafetyReport, path: str = "$", depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        report.omitted_paths.append(path)
        return "[omitted:depth_limit]"

    if isinstance(value, Mapping):
        for key, child in value.items():
            if _key_norm(key) in _PATH_KEYS and isinstance(child, str) and _is_disallowed_path(child):
                report.omitted_paths.append(path)
                return "[omitted:sensitive_source]"

        sanitized: dict[str, Any] = {}
        items = list(value.items())
        for key, child in items[:MAX_DICT_ITEMS]:
            child_path = f"{path}.{key}"
            if _is_sensitive_key(key):
                report.redacted_paths.append(child_path)
                sanitized[str(key)] = "[redacted:sensitive]"
                continue
            sanitized[str(key)] = _sanitize(child, report, child_path, depth + 1)
        if len(items) > MAX_DICT_ITEMS:
            report.omitted_paths.append(path)
            sanitized["_omitted_count"] = len(items) - MAX_DICT_ITEMS
        return sanitized

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized_list = [
            _sanitize(item, report, f"{path}[{index}]", depth + 1)
            for index, item in enumerate(items[:MAX_COLLECTION_ITEMS])
        ]
        if len(items) > MAX_COLLECTION_ITEMS:
            report.omitted_paths.append(path)
            sanitized_list.append({"_omitted_count": len(items) - MAX_COLLECTION_ITEMS})
        return sanitized_list

    if isinstance(value, str):
        if _is_disallowed_path(value) or _looks_sensitive_string(value):
            report.redacted_paths.append(path)
            return "[redacted:sensitive]"
        compact = " ".join(value.split()) if len(value) > MAX_STRING_CHARS else value
        if len(compact) > MAX_STRING_CHARS:
            report.truncated_paths.append(path)
            return compact[: MAX_STRING_CHARS - 15].rstrip() + "...[truncated]"
        return compact

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    return value


def _safe(value: Any, report: SafetyReport, path: str) -> Any:
    return _sanitize(value, report, path)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, TypeError):
        return default
    return default if value is None else value


def _screen_from_request_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    screen_id = _row_value(row, "screen_id")
    if not screen_id:
        return None
    return {
        "id": str(screen_id),
        "route": str(_row_value(row, "screen_route", "")),
        "name": str(_row_value(row, "screen_name", "")),
        "purpose": str(_row_value(row, "screen_purpose", "")),
        "primary_actions": _as_list(_row_value(row, "screen_primary_actions", [])),
        "component_paths": _as_list(_row_value(row, "screen_component_paths", [])),
        "metadata": _as_dict(_row_value(row, "screen_metadata", {})),
    }


def _snapshot_summary(snapshot: Mapping[str, Any], report: SafetyReport, index: int) -> dict[str, Any]:
    return {
        "id": str(_row_value(snapshot, "id", "")),
        "phase": str(_row_value(snapshot, "phase", "before")),
        "viewport": str(_row_value(snapshot, "viewport", "")),
        "image_url": _safe(str(_row_value(snapshot, "image_url", "")), report, f"$.snapshots[{index}].image_url"),
        "captured_at": _safe(_row_value(snapshot, "captured_at"), report, f"$.snapshots[{index}].captured_at"),
        "dom_summary": _safe(_row_value(snapshot, "dom_summary", {}), report, f"$.snapshots[{index}].dom_summary"),
    }


def _decision_summary(decision: Mapping[str, Any], report: SafetyReport, index: int) -> dict[str, Any]:
    return {
        "id": str(_row_value(decision, "id", "")),
        "subject": _safe(str(_row_value(decision, "subject", "")), report, f"$.decisions[{index}].subject"),
        "decision": _safe(str(_row_value(decision, "decision", "")), report, f"$.decisions[{index}].decision"),
        "rationale": _safe(_row_value(decision, "rationale"), report, f"$.decisions[{index}].rationale"),
        "applies_to": str(_row_value(decision, "applies_to", "project")),
        "confidence": float(_row_value(decision, "confidence", 0.0) or 0.0),
        "metadata": _safe(_row_value(decision, "metadata", {}), report, f"$.decisions[{index}].metadata"),
    }


def _collect_named_values(
    name_groups: Mapping[str, set[str]],
    candidates: Sequence[tuple[str, Mapping[str, Any]]],
    report: SafetyReport,
) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[dict[str, Any]]] = {key: [] for key in name_groups}
    for source_name, candidate in candidates:
        for raw_key, raw_value in candidate.items():
            normalized = _key_norm(raw_key)
            for group, keys in name_groups.items():
                if normalized in keys:
                    evidence[group].append(
                        {
                            "source": source_name,
                            "key": str(raw_key),
                            "value": _safe(raw_value, report, f"$.token_style_evidence.{source_name}.{raw_key}"),
                        }
                    )
    return {key: value for key, value in evidence.items() if value}


def _acceptance_checks(row: Mapping[str, Any], screen: dict[str, Any] | None, report: SafetyReport) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for index, item in enumerate(_as_list(_row_value(row, "acceptance_criteria", []))):
        checks.append(
            {
                "source": "request.acceptance_criteria",
                "check": _safe(item, report, f"$.acceptance_checks.request[{index}]"),
            }
        )

    normalized_card = _as_dict(_row_value(row, "normalized_card", {}))
    for key in ("acceptance_checks", "checks"):
        for index, item in enumerate(_as_list(normalized_card.get(key))):
            checks.append(
                {
                    "source": f"request.normalized_card.{key}",
                    "check": _safe(item, report, f"$.acceptance_checks.normalized_card.{key}[{index}]"),
                }
            )

    screen_metadata = _as_dict((screen or {}).get("metadata"))
    for index, item in enumerate(_as_list(screen_metadata.get("acceptance_checks"))):
        checks.append(
            {
                "source": "screen.metadata.acceptance_checks",
                "check": _safe(item, report, f"$.acceptance_checks.screen[{index}]"),
            }
        )
    return checks


def _source(kind: str, label: str, reference: Any = None, included: bool = True) -> dict[str, Any]:
    payload = {"kind": kind, "label": label, "included": included}
    if reference is not None:
        payload["reference"] = reference
    return payload


def _missing(kind: str, reason: str, severity: str = "medium") -> dict[str, str]:
    return {"kind": kind, "reason": reason, "severity": severity}


def _risk_notes(
    row: Mapping[str, Any],
    screen: dict[str, Any] | None,
    snapshots: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    missing_context: Sequence[Mapping[str, Any]],
) -> list[str]:
    notes: list[str] = []
    if not screen:
        notes.append("No linked screen metadata; route and component scope may be incomplete.")
    if not snapshots:
        notes.append("No visual snapshot metadata is available for before/after comparison.")
    elif not any(str(_row_value(item, "phase", "")).lower() == "before" for item in snapshots):
        notes.append("No before snapshot is available; current-state visual evidence is partial.")
    if not _as_list((screen or {}).get("component_paths")):
        notes.append("No component paths are registered for this screen.")
    if _as_dict(_row_value(row, "forbidden_scope", {})):
        notes.append("Forbidden scope is present and must stay locked during implementation.")
    low_confidence = [
        str(_row_value(item, "subject", ""))
        for item in decisions
        if float(_row_value(item, "confidence", 0.0) or 0.0) < 0.6
    ]
    if low_confidence:
        notes.append("Some related design decisions have low confidence and should be treated as advisory.")
    if missing_context:
        notes.append("Context pack has missing context entries; implementation should preserve current behavior.")
    return notes[:12]


def _enforce_context_size(context: dict[str, Any], report: SafetyReport) -> None:
    if _json_chars(context) <= MAX_CONTEXT_CHARS:
        return
    current_state = _as_dict(context.get("current_state"))
    screen_state = _as_dict(current_state.get("screen"))
    if screen_state.get("metadata"):
        screen_state["metadata"] = {"_omitted": "context_size_limit"}
        current_state["screen"] = screen_state
        context["current_state"] = current_state
        report.omitted_paths.append("$.current_state.screen.metadata")
    if _json_chars(context) <= MAX_CONTEXT_CHARS:
        return
    snapshots = _as_list(current_state.get("snapshots"))
    compact_snapshots = []
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        compact_snapshots.append(
            {
                "id": snapshot.get("id"),
                "phase": snapshot.get("phase"),
                "viewport": snapshot.get("viewport"),
                "captured_at": snapshot.get("captured_at"),
            }
        )
    current_state["snapshots"] = compact_snapshots
    context["current_state"] = current_state
    report.omitted_paths.append("$.current_state.snapshots.dom_summary")


def build_design_context_pack(
    request_row: Mapping[str, Any],
    snapshot_rows: Sequence[Mapping[str, Any]] | None = None,
    decision_rows: Sequence[Mapping[str, Any]] | None = None,
) -> DesignContextPackBuildResult:
    """Assemble a sanitized JSON pack from persisted Design Modification Studio rows."""
    snapshots = list(snapshot_rows or [])
    decisions = list(decision_rows or [])
    report = SafetyReport()

    screen = _screen_from_request_row(request_row)
    project_key = str(_row_value(request_row, "project_key", "")).upper()
    component_paths = _as_list((screen or {}).get("component_paths"))
    screen_metadata = _as_dict((screen or {}).get("metadata"))
    normalized_card = _as_dict(_row_value(request_row, "normalized_card", {}))
    allowed_scope = _as_dict(_row_value(request_row, "allowed_scope", {}))
    forbidden_scope = _as_dict(_row_value(request_row, "forbidden_scope", {}))

    snapshot_summaries = [
        _snapshot_summary(snapshot, report, index)
        for index, snapshot in enumerate(snapshots[:MAX_COLLECTION_ITEMS])
    ]
    decision_summaries = [
        _decision_summary(decision, report, index)
        for index, decision in enumerate(decisions[:MAX_COLLECTION_ITEMS])
    ]

    evidence_candidates: list[tuple[str, Mapping[str, Any]]] = [
        ("screen.metadata", screen_metadata),
        ("request.normalized_card", normalized_card),
    ]
    for index, snapshot in enumerate(snapshots[:MAX_COLLECTION_ITEMS]):
        evidence_candidates.append((f"snapshot[{index}].dom_summary", _as_dict(_row_value(snapshot, "dom_summary", {}))))
    for index, decision in enumerate(decisions[:MAX_COLLECTION_ITEMS]):
        evidence_candidates.append((f"decision[{index}].metadata", _as_dict(_row_value(decision, "metadata", {}))))

    token_style_evidence = _collect_named_values(
        {
            "tokens": {
                "tokens",
                "designtokens",
                "styletokens",
                "colortokens",
                "spacingtokens",
                "typographytokens",
                "tokenevidence",
            },
            "audit": {"audit", "designaudit", "auditfindings", "auditsummary", "designauditfindings"},
            "style": {"style", "styles", "styleevidence", "visuallanguage", "layoutdensity", "typography"},
        },
        evidence_candidates,
        report,
    )

    missing_context: list[dict[str, Any]] = []
    if not screen:
        missing_context.append(_missing("screen_metadata", "No linked screen metadata row was found.", "high"))
    if not component_paths:
        missing_context.append(_missing("component_paths", "No component paths are registered for the target screen."))
    if not snapshots:
        missing_context.append(_missing("visual_snapshots", "No visual snapshot metadata exists for this request.", "high"))
    elif not any(str(_row_value(item, "phase", "")).lower() == "before" for item in snapshots):
        missing_context.append(_missing("snapshot_before", "No before snapshot metadata exists for this request.", "high"))
    if not _as_list(_row_value(request_row, "acceptance_criteria", [])):
        missing_context.append(_missing("acceptance_criteria", "No explicit acceptance criteria are attached."))
    if not decisions:
        missing_context.append(_missing("design_decisions", "No related design decisions were found.", "low"))
    if not token_style_evidence:
        missing_context.append(_missing("token_style_evidence", "No token, style, or audit evidence was found.", "low"))

    sources: list[dict[str, Any]] = [
        _source("request", "design_modification_requests", str(_row_value(request_row, "id", ""))),
    ]
    if screen:
        sources.append(_source("screen", "design_screens", screen.get("id")))
    for snapshot in snapshot_summaries:
        sources.append(
            _source(
                "snapshot",
                f"{snapshot.get('phase') or 'snapshot'}:{snapshot.get('viewport') or 'unknown'}",
                snapshot.get("id"),
            )
        )
    for decision in decision_summaries:
        sources.append(_source("decision", str(decision.get("subject") or "design decision"), decision.get("id")))
    if token_style_evidence:
        sources.append(_source("token_style_evidence", "screen/request/snapshot metadata"))

    acceptance_checks = _acceptance_checks(request_row, screen, report)
    context: dict[str, Any] = {
        "pack_version": PACK_VERSION,
        "project": {"key": project_key},
        "request_id": str(_row_value(request_row, "id", "")),
        "route": _safe((screen or {}).get("route"), report, "$.route"),
        "component_paths": _safe(component_paths, report, "$.component_paths"),
        "current_state": {
            "screen": {
                "id": (screen or {}).get("id"),
                "name": _safe((screen or {}).get("name"), report, "$.current_state.screen.name"),
                "purpose": _safe((screen or {}).get("purpose"), report, "$.current_state.screen.purpose"),
                "primary_actions": _safe((screen or {}).get("primary_actions", []), report, "$.current_state.screen.primary_actions"),
                "metadata": _safe(screen_metadata, report, "$.current_state.screen.metadata"),
            },
            "snapshots": snapshot_summaries,
        },
        "target_state": {
            "request_type": str(_row_value(request_row, "request_type", "other")),
            "status": str(_row_value(request_row, "status", "draft")),
            "user_prompt": _safe(str(_row_value(request_row, "user_prompt", "")), report, "$.target_state.user_prompt"),
            "normalized_card": _safe(normalized_card, report, "$.target_state.normalized_card"),
            "allowed_scope": _safe(allowed_scope, report, "$.target_state.allowed_scope"),
            "acceptance_criteria": _safe(_as_list(_row_value(request_row, "acceptance_criteria", [])), report, "$.target_state.acceptance_criteria"),
        },
        "locked_constraints": {
            "forbidden_scope": _safe(forbidden_scope, report, "$.locked_constraints.forbidden_scope"),
            "allowed_scope": _safe(allowed_scope, report, "$.locked_constraints.allowed_scope"),
            "decisions": decision_summaries,
        },
        "acceptance_checks": acceptance_checks,
        "related_decisions": decision_summaries,
        "risk_notes": _risk_notes(request_row, screen, snapshots, decisions, missing_context),
        "token_style_evidence": token_style_evidence,
        "snapshot_metadata": snapshot_summaries,
    }

    _enforce_context_size(context, report)
    safety_report = report.as_dict()
    if any(safety_report.values()):
        context["safety_filters"] = safety_report

    return DesignContextPackBuildResult(
        context=context,
        sources=_safe(sources, report, "$.sources"),
        missing_context=_safe(missing_context, report, "$.missing_context"),
        prompt_chars=_json_chars(context),
        safety_report=safety_report,
    )
