"""Privacy-preserving ARIA partial-structure signatures for browser revisits.

The signature intentionally describes only an interaction area's stable shape.
It is not a DOM snapshot and must never become a store for page or account text.
Browser recipes remain the canonical site/page template; their optional
``capture_rules.aria_signature`` supplies required anchors and thresholds.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

SIGNATURE_VERSION = "aria-partial-v1"
DEFAULT_REUSE_THRESHOLD = 0.88
_NAME_LIMIT = 96
_SAFE_DATA_ATTRIBUTES = frozenset({"data-testid", "data-test", "data-qa", "data-cy", "data-component"})
_STATE_KEYS = frozenset({"expanded", "selected", "checked", "disabled", "required", "invalid", "pressed", "current", "haspopup"})
_RELATION_KEYS = frozenset({"controls", "labelledby", "describedby", "owns"})
_DYNAMIC_TEXT = re.compile(
    r"(?:\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|"
    r"[$€£¥₩]|\b(?:price|stock|inventory|sale|ad|advertisement|sponsored)\b|"
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|\+?\d[\d ()-]{7,}\b|\b\d{5,}\b)",
    re.IGNORECASE,
)
_PERSONAL_TEXT = re.compile(r"\b(?:welcome|hello|profile|account|customer|user|member)\b", re.IGNORECASE)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_accessible_name(value: Any) -> str | None:
    """Return a stable accessible name or omit text that can identify/change.

    Names are normalized for comparison only and are immediately hashed before
    persistence.  Dynamic price, stock, ad, date/time, identifier and likely
    account-personalized text therefore cannot enter a signature.
    """
    name = unicodedata.normalize("NFKC", str(value or "")).casefold()
    name = re.sub(r"\s+", " ", name).strip()
    if not name or len(name) > _NAME_LIMIT or _DYNAMIC_TEXT.search(name) or _PERSONAL_TEXT.search(name):
        return None
    # A numeric-only label cannot be a stable accessible name.
    return name if any(char.isalpha() for char in name) else None


def _name_hash(value: Any) -> str | None:
    name = normalize_accessible_name(value)
    return _digest(name) if name else None


def _stable_states(node: Mapping[str, Any]) -> dict[str, bool | str]:
    states = node.get("states") if isinstance(node.get("states"), Mapping) else node
    result: dict[str, bool | str] = {}
    for key in _STATE_KEYS:
        value = states.get(key)
        if isinstance(value, bool) or (
            key == "current"
            and isinstance(value, str)
            and value in {"page", "step", "location", "date", "time"}
        ):
            result[key] = value
    return result


def _relation_targets(value: Any) -> list[dict[str, str]]:
    """Keep only semantic targets; raw relationship IDs are deliberately dropped."""
    values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
    result: list[dict[str, str]] = []
    for target in values:
        if not isinstance(target, Mapping):
            continue
        role = str(target.get("role") or "").strip().lower()
        name = _name_hash(target.get("name") or target.get("accessible_name") or target.get("label"))
        if role:
            item = {"role": role}
            if name:
                item["name_hash"] = name
            result.append(item)
    return sorted(result, key=_canonical)


def _normalize_node(node: Mapping[str, Any]) -> dict[str, Any] | None:
    role = str(node.get("role") or "").strip().lower()
    if not role:
        return None
    item: dict[str, Any] = {"role": role}
    raw_name = node.get("accessible_name") or node.get("name") or node.get("label")
    name_hash = _name_hash(raw_name)
    # A supplied but non-stable name denotes exactly the volatile/personalized
    # content this signature must exclude; retaining its role would still make
    # price or ad insertion look like a structural change.
    if str(raw_name or "").strip() and not name_hash:
        return None
    if name_hash:
        item["name_hash"] = name_hash
    landmark = str(node.get("landmark") or node.get("parent_role") or "").strip().lower()
    if landmark:
        item["landmark"] = landmark
    states = _stable_states(node)
    if states:
        item["states"] = states
    relations = node.get("relationships") if isinstance(node.get("relationships"), Mapping) else {}
    normalized_relations: dict[str, list[dict[str, str]]] = {}
    for key in _RELATION_KEYS:
        targets = _relation_targets(relations.get(key))
        if targets:
            normalized_relations[key] = targets
    if normalized_relations:
        item["relationships"] = normalized_relations
    attributes = node.get("attributes") if isinstance(node.get("attributes"), Mapping) else {}
    stable_attributes: dict[str, str] = {}
    for key, value in attributes.items():
        normalized_key = str(key).lower()
        value_hash = _name_hash(value)
        if normalized_key in _SAFE_DATA_ATTRIBUTES and value_hash:
            stable_attributes[normalized_key] = value_hash
    if stable_attributes:
        item["data_attributes"] = dict(sorted(stable_attributes.items()))
    return item


def build_partial_signature(nodes: Sequence[Mapping[str, Any]], *, area_key: str) -> dict[str, Any]:
    """Normalize an ARIA interaction subtree without retaining sibling order/text."""
    normalized = [item for node in nodes if isinstance(node, Mapping) if (item := _normalize_node(node))]
    normalized.sort(key=_canonical)  # advertisements and unstable sibling ordering do not affect identity.
    structure = {"area_key": str(area_key), "nodes": normalized}
    return {
        "signature_version": SIGNATURE_VERSION,
        "signature_hash": _digest(_canonical(structure)),
        "structure": structure,
        "node_count": len(normalized),
    }


def _node_token(node: Mapping[str, Any]) -> str:
    return _canonical(node)


def _required_anchor_present(anchor: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]]) -> bool:
    role = str(anchor.get("role") or "").lower()
    name_hash = _name_hash(anchor.get("name") or anchor.get("accessible_name") or anchor.get("label"))
    return any(node.get("role") == role and (not name_hash or node.get("name_hash") == name_hash) for node in nodes)


def assess_revisit(
    *, previous: Mapping[str, Any] | None, current_nodes: Sequence[Mapping[str, Any]] | None,
    area_key: str, template: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose reuse, rediscovery, or Human Gateway without treating ambiguity as success."""
    template = template or {}
    if not current_nodes:
        return {"decision": "rediscover", "reason": "aria_missing_dom_fallback_required", "similarity": 0.0,
                "human_gateway_required": False, "signature": None}
    signature = build_partial_signature(current_nodes, area_key=area_key)
    current = signature["structure"]["nodes"]
    tokens = [_node_token(node) for node in current]
    if not current or len(tokens) != len(set(tokens)):
        return {"decision": "rediscover", "reason": "ambiguous_aria_structure", "similarity": 0.0,
                "human_gateway_required": False, "signature": signature}
    required = template.get("required_anchors") if isinstance(template.get("required_anchors"), Sequence) else []
    missing = [anchor for anchor in required if isinstance(anchor, Mapping) and not _required_anchor_present(anchor, current)]
    if missing:
        return {"decision": "human_gateway", "reason": "critical_required_anchor_missing", "similarity": 0.0,
                "human_gateway_required": True, "signature": signature}
    if not previous:
        return {"decision": "rediscover", "reason": "no_prior_signature", "similarity": 0.0,
                "human_gateway_required": False, "signature": signature}
    old_nodes = ((previous.get("structure") or {}).get("nodes") or []) if isinstance(previous, Mapping) else []
    old_tokens = {_node_token(node) for node in old_nodes if isinstance(node, Mapping)}
    new_tokens = set(tokens)
    similarity = len(old_tokens & new_tokens) / len(old_tokens | new_tokens) if old_tokens or new_tokens else 1.0
    threshold = float(template.get("reuse_threshold", DEFAULT_REUSE_THRESHOLD))
    threshold = min(1.0, max(0.0, threshold))
    if similarity >= threshold:
        return {"decision": "reuse", "reason": "similarity_threshold_met", "similarity": similarity,
                "human_gateway_required": False, "signature": signature}
    return {"decision": "rediscover", "reason": "similarity_below_threshold", "similarity": similarity,
            "human_gateway_required": False, "signature": signature}


async def load_recipe_aria_template(*, tenant_id: str, recipe_id: str, recipe_version: str) -> dict[str, Any] | None:
    """Load only the ARIA section of the existing tenant-scoped recipe canon."""
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT capture_rules FROM browser_recipes
               WHERE tenant_id=$1::uuid AND recipe_id=$2 AND version=$3 AND enabled IS TRUE""",
            tenant_id, recipe_id, recipe_version,
        )
    if not row:
        return None
    capture_rules = row["capture_rules"] if isinstance(row["capture_rules"], Mapping) else json.loads(row["capture_rules"] or "{}")
    template = capture_rules.get("aria_signature") if isinstance(capture_rules, Mapping) else None
    return dict(template) if isinstance(template, Mapping) else {}


async def record_revisit_signature(
    *, tenant_id: str, recipe_id: str, recipe_version: str, page_key: str, area_key: str,
    current_nodes: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Assess and persist a tenant-scoped, non-textual observation for a revisit."""
    from app.core.db_pool import get_pool
    template = await load_recipe_aria_template(tenant_id=tenant_id, recipe_id=recipe_id, recipe_version=recipe_version)
    if template is None:
        raise ValueError("recipe_template_not_found")
    async with get_pool().acquire() as conn:
        previous = await conn.fetchrow(
            """SELECT normalized_structure AS structure FROM browser_page_structure_signatures
               WHERE tenant_id=$1::uuid AND recipe_id=$2 AND recipe_version=$3
                 AND page_key=$4 AND area_key=$5 ORDER BY observed_at DESC LIMIT 1""",
            tenant_id, recipe_id, recipe_version, page_key, area_key,
        )
        result = assess_revisit(previous=dict(previous) if previous else None, current_nodes=current_nodes,
                                area_key=area_key, template=template)
        signature = result["signature"]
        await conn.execute(
            """INSERT INTO browser_page_structure_signatures
               (tenant_id,recipe_id,recipe_version,page_key,area_key,signature_version,signature_hash,
                normalized_structure,similarity,decision,reason,human_gateway_required)
               VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12)""",
            tenant_id, recipe_id, recipe_version, page_key, area_key,
            SIGNATURE_VERSION, signature["signature_hash"] if signature else None,
            json.dumps(signature["structure"] if signature else {}), result["similarity"], result["decision"],
            result["reason"], result["human_gateway_required"],
        )
    return result
