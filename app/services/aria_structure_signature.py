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


def _stable_data_attributes(node: Mapping[str, Any]) -> dict[str, str]:
    """Hash allowlisted stable data attributes without retaining their values."""
    attributes = node.get("attributes") if isinstance(node.get("attributes"), Mapping) else {}
    stable_attributes: dict[str, str] = {}
    for key, value in attributes.items():
        normalized_key = str(key).lower()
        normalized_value = unicodedata.normalize("NFKC", str(value)).strip() if value is not None else ""
        if normalized_key in _SAFE_DATA_ATTRIBUTES and normalized_value:
            stable_attributes[normalized_key] = _digest(normalized_value)
    return dict(sorted(stable_attributes.items()))


def _stable_states(
    node: Mapping[str, Any], *, template: Mapping[str, Any] | None = None,
) -> dict[str, bool | str]:
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
    if template is None:
        return result
    configured = _configured_state_keys(template)
    return {key: value for key, value in result.items() if key in configured}


def _observed_states(node: Mapping[str, Any]) -> dict[str, bool | str]:
    return _stable_states(node)


def _template_name_hashes(template: Mapping[str, Any]) -> frozenset[str]:
    """Return hashes for names the recipe explicitly approved for persistence."""
    approved: set[str] = set()
    names: list[Any] = []
    required = template.get("required_anchors")
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        names.extend(anchor for anchor in required if isinstance(anchor, Mapping))
    stable_names = template.get("stable_names")
    if isinstance(stable_names, Sequence) and not isinstance(stable_names, (str, bytes)):
        names.extend(stable_names)
    for item in names:
        value = (
            item.get("name") or item.get("accessible_name") or item.get("label")
            if isinstance(item, Mapping) else item
        )
        name_hash = _name_hash(value)
        if name_hash:
            approved.add(name_hash)
    return frozenset(approved)


def _state_keys_from(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        candidates = value.keys()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        candidates = value
    else:
        candidates = ()
    return {str(key).lower() for key in candidates if str(key).lower() in _STATE_KEYS}


def _required_state_rules(template: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Collect state assertions, including state-bearing required anchors."""
    rules: list[Mapping[str, Any]] = []
    anchors = template.get("required_anchors")
    if isinstance(anchors, Sequence) and not isinstance(anchors, (str, bytes)):
        rules.extend(
            anchor for anchor in anchors
            if isinstance(anchor, Mapping) and isinstance(anchor.get("states"), Mapping)
        )
    required = template.get("required_states")
    if isinstance(required, Mapping):
        if any(key in required for key in ("role", "name", "accessible_name", "label", "states")):
            rules.append(required)
        else:
            rules.append({"states": required})
    elif isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        rules.extend(rule for rule in required if isinstance(rule, Mapping))
    return rules


def _configured_state_keys(template: Mapping[str, Any]) -> frozenset[str]:
    keys = _state_keys_from(template.get("stable_states"))
    for rule in _required_state_rules(template):
        keys.update(_state_keys_from(rule.get("states")))
    return frozenset(keys)


def _relation_targets(
    value: Any, *, approved_name_hashes: frozenset[str],
) -> list[dict[str, str]]:
    """Keep unnamed targets and template-approved named targets, never raw IDs/names."""
    values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
    result: list[dict[str, str]] = []
    for target in values:
        if not isinstance(target, Mapping):
            continue
        role = str(target.get("role") or "").strip().lower()
        raw_name = target.get("accessible_name") or target.get("name") or target.get("label")
        has_name = bool(str(raw_name or "").strip())
        name = _name_hash(raw_name)
        # A named relationship target is semantic text, not a structural target,
        # unless that exact name was explicitly approved by the recipe.
        if has_name and name not in approved_name_hashes:
            continue
        if role:
            item = {"role": role}
            if name:
                item["name_hash"] = name
            result.append(item)
    return sorted(result, key=_canonical)


def _normalize_node(
    node: Mapping[str, Any], *, approved_name_hashes: frozenset[str], state_keys: frozenset[str],
) -> dict[str, Any] | None:
    role = str(node.get("role") or "").strip().lower()
    if not role:
        return None
    item: dict[str, Any] = {"role": role}
    raw_name = node.get("accessible_name") or node.get("name") or node.get("label")
    has_name = bool(str(raw_name or "").strip())
    name_hash = _name_hash(raw_name)
    stable_attributes = _stable_data_attributes(node)
    # Named nodes are included only when the recipe approved their name or an
    # allowlisted stable data attribute supplies a non-textual identity. This
    # drops personalized and ordinary unapproved labels instead of retaining a
    # role-only fingerprint for them.
    if has_name and name_hash not in approved_name_hashes and not stable_attributes:
        return None
    if name_hash in approved_name_hashes:
        item["name_hash"] = name_hash
    landmark = str(node.get("landmark") or node.get("parent_role") or "").strip().lower()
    if landmark:
        item["landmark"] = landmark
    states = _stable_states(node, template={"stable_states": list(state_keys)})
    if states:
        item["states"] = states
    relations = node.get("relationships") if isinstance(node.get("relationships"), Mapping) else {}
    normalized_relations: dict[str, list[dict[str, str]]] = {}
    for key in _RELATION_KEYS:
        targets = _relation_targets(relations.get(key), approved_name_hashes=approved_name_hashes)
        if targets:
            normalized_relations[key] = targets
    if normalized_relations:
        item["relationships"] = normalized_relations
    if stable_attributes:
        item["data_attributes"] = stable_attributes
    return item


def build_partial_signature(
    nodes: Sequence[Mapping[str, Any]], *, area_key: str, template: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize approved names, stable attributes, and unnamed ARIA structure only."""
    template = template or {}
    approved_name_hashes = _template_name_hashes(template)
    state_keys = _configured_state_keys(template)
    normalized = [
        item for node in nodes if isinstance(node, Mapping)
        if (item := _normalize_node(node, approved_name_hashes=approved_name_hashes, state_keys=state_keys))
    ]
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


def _required_state_present(rule: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]]) -> bool:
    role = str(rule.get("role") or "").strip().lower()
    name_hash = _name_hash(rule.get("name") or rule.get("accessible_name") or rule.get("label"))
    expected = rule.get("states") if isinstance(rule.get("states"), Mapping) else {}
    expected = {key: value for key, value in expected.items() if str(key).lower() in _STATE_KEYS}
    if not expected:
        return True
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        if role and str(node.get("role") or "").strip().lower() != role:
            continue
        raw_name = node.get("accessible_name") or node.get("name") or node.get("label")
        if name_hash and _name_hash(raw_name) != name_hash:
            continue
        observed = _observed_states(node)
        if all(observed.get(str(key).lower()) == value for key, value in expected.items()):
            return True
    return False


def assess_revisit(
    *, previous: Mapping[str, Any] | None, current_nodes: Sequence[Mapping[str, Any]] | None,
    area_key: str, template: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose reuse, rediscovery, or Human Gateway without treating ambiguity as success."""
    template = template or {}
    signature = build_partial_signature(current_nodes or [], area_key=area_key, template=template)
    current = signature["structure"]["nodes"]
    required = template.get("required_anchors") if isinstance(template.get("required_anchors"), Sequence) else []
    missing = [anchor for anchor in required if isinstance(anchor, Mapping) and not _required_anchor_present(anchor, current)]
    if missing:
        return {"decision": "human_gateway", "reason": "critical_required_anchor_missing", "similarity": 0.0,
                "human_gateway_required": True, "signature": signature}
    required_state_rules = _required_state_rules(template)
    if any(not _required_state_present(rule, current_nodes) for rule in required_state_rules):
        return {"decision": "human_gateway", "reason": "critical_required_state_changed", "similarity": 0.0,
                "human_gateway_required": True, "signature": signature}
    if not current_nodes:
        return {"decision": "rediscover", "reason": "aria_missing_dom_fallback_required", "similarity": 0.0,
                "human_gateway_required": False, "signature": None}
    tokens = [_node_token(node) for node in current]
    if not current or len(tokens) != len(set(tokens)):
        return {"decision": "rediscover", "reason": "ambiguous_aria_structure", "similarity": 0.0,
                "human_gateway_required": False, "signature": signature}
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
