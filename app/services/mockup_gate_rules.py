"""Stdlib-only rules for the mockup approval execution gate.

Shared by the API (submit gate, worker gate endpoint) and by scripts/verify_mockup_approval.py, which runs on the
runner host without the app's dependencies. Nothing here touches the database.
"""
from __future__ import annotations

import os
import re
from typing import Any

MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
PHASES = ("submit", "pre_execution", "checkpoint")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MARKER = re.compile(r"^[ \t]*([A-Z_]{4,32})[ \t]*[:=][ \t]*(.*?)[ \t]*$")
_BUNDLE_KEYS = {
    "MOCKUP_REVIEW_ID": "review_id",
    "MOCKUP_REVISION_ID": "revision_id",
    "MOCKUP_MANIFEST_HASH": "manifest_hash",
}
_UI_SUFFIXES = (".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".sass", ".less", ".html")
_UI_REPOS = ("aads-dashboard",)
_NON_UI_PREFIXES = ("docs/", "tests/", "test/", "scripts/", "reports/", "migrations/")


def gate_mode(environ: dict[str, str] | None = None) -> str:
    """off = no checks, shadow = record only unless a bundle is declared, enforce = UI tasks need a bundle."""
    value = ((environ if environ is not None else os.environ).get("MOCKUP_GATE_MODE") or DEFAULT_MODE).strip().lower()
    return value if value in MODES else "enforce"  # an unreadable setting must not silently disable the gate


def _meta_lines(instruction: str):
    """Yield (key, value) for KEY: value lines outside fenced code blocks (no regex over the whole text)."""
    fenced = False
    for line in instruction.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _MARKER.match(line)
        if match:
            yield match.group(1), match.group(2)


def parse_bundle(instruction: str) -> dict[str, Any]:
    """Read the approved-bundle declaration from instruction metadata lines.

    {"declared": False} when no MOCKUP_* line exists. Once any line exists the declaration must be complete and
    unambiguous; otherwise errors explain why, and the caller treats the task as not verified.
    """
    found: dict[str, list[str]] = {}
    for key, value in _meta_lines(instruction):
        if key in _BUNDLE_KEYS:
            found.setdefault(_BUNDLE_KEYS[key], []).append(value)
    if not found:
        return {"declared": False, "errors": []}
    errors: list[str] = []
    bundle: dict[str, Any] = {"declared": True}
    for field in ("review_id", "revision_id", "manifest_hash"):
        values = found.get(field, [])
        if not values:
            errors.append(f"{field}_missing")
        elif len(set(v.lower() for v in values)) > 1:
            errors.append(f"{field}_ambiguous")
        else:
            value = values[0].lower()
            valid = _SHA256.fullmatch(value) if field == "manifest_hash" else _UUID.fullmatch(value)
            if valid:
                bundle[field] = value
            else:
                errors.append(f"{field}_invalid")
    bundle["errors"] = errors
    return bundle


def declared_goal_id(instruction: str) -> str | None:
    for key, value in _meta_lines(instruction):
        if key == "GOAL_ID" and _UUID.fullmatch(value):
            return value.lower()
    return None


def _split_paths(value: str) -> list[str]:
    return [p.strip().strip("`'\"") for p in re.split(r"[,\s]+", value) if p.strip().strip("`'\"")]


def is_ui_path(path: str) -> bool:
    lowered = path.lower().lstrip("./")
    if lowered.startswith(_NON_UI_PREFIXES) or "/reports/" in lowered or lowered.endswith((".md", ".json", ".py", ".sh")):
        return False
    return lowered.endswith(_UI_SUFFIXES) or lowered.startswith(("src/app/", "src/components/", "src/features/"))


def ui_task_reasons(instruction: str) -> list[str]:
    """Why this instruction looks like a UI task. Only TARGET / TARGET_FILES / UI_TASK lines count.

    READ_ONLY_FILES and prose are ignored, so a backend task that merely mentions a .tsx file is not a UI task.
    UI_TASK: true can add the UI classification; no value can remove it.
    """
    reasons: list[str] = []
    for key, value in _meta_lines(instruction):
        if key == "UI_TASK" and value.strip().lower() in ("true", "yes", "1"):
            reasons.append("ui_task_declared")
        elif key == "TARGET" and any(repo in value.lower() for repo in _UI_REPOS):
            reasons.append("dashboard_target")
        elif key == "TARGET_FILES":
            reasons.extend(f"ui_file:{p}" for p in _split_paths(value) if is_ui_path(p))
    return reasons[:10]


def classify(instruction: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """What the gate must do for this instruction, decided without any I/O."""
    mode = gate_mode(environ)
    bundle = parse_bundle(instruction)
    reasons = ui_task_reasons(instruction)
    ui = bool(reasons)
    applicable = mode != "off" and (bundle["declared"] or ui)
    return {"mode": mode, "declared": bundle["declared"], "bundle": bundle, "ui_task": ui, "ui_reasons": reasons,
            "applicable": applicable}


def no_bundle_decision(mode: str) -> dict[str, Any]:
    """A UI task that declares no approved bundle: blocked when enforcing, only recorded while in shadow."""
    if mode == "enforce":
        return {"allowed": False, "reasons": ["mockup_bundle_required"]}
    return {"allowed": True, "shadow": True, "reasons": ["mockup_bundle_required"]}


def unavailable_decision(info: dict[str, Any]) -> dict[str, Any]:
    """The gate could not reach its database. A declared bundle or an enforced UI task is denied (fail closed)."""
    if info.get("declared") or info.get("mode") == "enforce":
        return {"allowed": False, "reasons": ["gate_unavailable"]}
    return {"allowed": True, "shadow": True, "reasons": ["gate_unavailable"]}
