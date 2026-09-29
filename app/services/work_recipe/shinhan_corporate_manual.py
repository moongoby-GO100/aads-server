"""Fail-closed three-stage Shinhan corporate manual recipe boundary.

Receipts contain only evidence fingerprints. They are generated from the browser
executor's observation, never accepted as execution authority from a caller.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from app.browser_bridge.models import BrowserEndpointKind
from app.browser_bridge.service import get_browser_bridge_service
from app.core.db_pool import get_pool
from app.services.bank_collection_lock import (
    default_bank_lock_path,
    release_bank_lock,
    try_acquire_bank_lock,
)
from app.services.pc_agent_manager import pc_agent_manager
from app.services.work_recipe import store
from app.services.work_recipe.schema import WorkRecipe

DOMAIN = "bank.shinhan.com"
MANIFEST_KEY = "shinhan_corporate_manual"
STAGES = ("open", "authenticate", "verify")
_SAFE_ACTIONS = frozenset({"navigate", "click", "fill", "snapshot"})
_SAFE_SELECTOR = re.compile(
    r"^[#.](?:login|logIn|user(?:name|Id)?|password|passwd|account|balance|"
    r"logout|inquiry|btnLogin|btnAccount|btnBalance|계좌조회|잔액조회|로그인|로그아웃)"
    r"(?:[-_][A-Za-z0-9]+)?$", re.IGNORECASE,
)
_MUTATION = re.compile(r"transfer|trnsfr|remit|payment|pay(?:ment)?ok|송금|이체|결제|출금", re.IGNORECASE)
_STAGE_MARKERS = {
    # These are conservative screen assertions. A hash alone proves only that
    # some page was rendered, not that login or account lookup succeeded.
    "open": ("기업", "로그인"),
    "authenticate": ("로그아웃", "계좌조회"),
    "verify": ("계좌번호", "잔액"),
}
_BALANCE_PATTERN = re.compile(r"잔액\s*[:：]?\s*(?:[0-9][0-9,]*|\*{3,})\s*원")


class FinancialManualBlocked(ValueError):
    """An unapproved or unverified financial replay was refused."""


def is_shinhan_recipe(recipe: WorkRecipe) -> bool:
    values = [recipe.domain, *(step.url for step in [*recipe.steps, *recipe.verify] if step.url)]
    for value in values:
        host = store.normalize_domain(value)
        if host == "shinhan.com" or host.endswith(".shinhan.com"):
            return True
        # A fixed non-bank host with a templated path is not a bank recipe.
        # An unresolved destination/authority must fail closed before secrets
        # are resolved; it could otherwise conceal a financial destination.
        parsed = urlsplit(value if "://" in value else "//" + value)
        if "{{" in parsed.netloc:
            return True
    return False


def validate_manifest(recipe: WorkRecipe) -> tuple[int, ...]:
    """Bind the three stage boundaries to immutable approved recipe steps."""
    if not is_shinhan_recipe(recipe):
        return ()
    if store.normalize_domain(recipe.domain) != DOMAIN:
        raise FinancialManualBlocked("shinhan_origin_invalid")
    manifest = recipe.metadata.get(MANIFEST_KEY)
    if not isinstance(manifest, Mapping):
        raise FinancialManualBlocked("shinhan_manifest_required")
    ends = manifest.get("stage_end_seq")
    if not isinstance(ends, list) or len(ends) != 3 or any(type(n) is not int for n in ends):
        raise FinancialManualBlocked("shinhan_stage_boundaries_invalid")
    if not (1 <= ends[0] < ends[1] < ends[2] == len(recipe.steps)):
        raise FinancialManualBlocked("shinhan_stage_boundaries_invalid")
    if manifest.get("stages") != list(STAGES):
        raise FinancialManualBlocked("shinhan_stage_manifest_invalid")
    if recipe.metadata.get("native_auth_required") is not True:
        raise FinancialManualBlocked("shinhan_pc_lane_required")
    for step in [*recipe.steps, *recipe.verify]:
        text = " ".join(str(getattr(step, key) or "") for key in
                        ("action", "url", "endpoint", "description", "selector", "value", "wait_for"))
        if _MUTATION.search(text):
            raise FinancialManualBlocked("financial_mutation_forbidden")
        if step.action not in _SAFE_ACTIONS or step.risk != "READ" or step.endpoint or step.extra:
            raise FinancialManualBlocked("shinhan_action_forbidden")
        if step.selector and not _SAFE_SELECTOR.fullmatch(step.selector):
            raise FinancialManualBlocked("shinhan_selector_forbidden")
        if step.action in {"click", "fill"} and not step.selector:
            raise FinancialManualBlocked("shinhan_selector_required")
        if step.url:
            try:
                parsed = urlsplit(step.url)
                port = parsed.port
            except ValueError:
                raise FinancialManualBlocked("shinhan_origin_invalid") from None
            if (parsed.scheme != "https" or parsed.hostname != DOMAIN
                    or parsed.username or parsed.password or port not in (None, 443)
                    or "\\" in step.url or any(ord(char) < 32 for char in step.url)):
                raise FinancialManualBlocked("shinhan_origin_invalid")
        if step.action == "fill" and (not isinstance(step.value, str) or not step.value.startswith("{{")):
            raise FinancialManualBlocked("shinhan_secret_reference_required")
    return tuple(ends)


def receipt_for(stage: str, evidence: Mapping[str, Any], predecessor: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Create a content-bound receipt from observed browser evidence."""
    if stage not in STAGES:
        raise FinancialManualBlocked("stage_invalid")
    if stage == STAGES[0]:
        if predecessor is not None:
            raise FinancialManualBlocked("unexpected_predecessor")
        prior_hash = ""
    else:
        expected = STAGES[STAGES.index(stage) - 1]
        if predecessor is None or predecessor.get("stage") != expected or not verify_receipt(predecessor):
            raise FinancialManualBlocked("predecessor_evidence_required")
        prior_hash = str(predecessor["receipt_sha256"])
    url = str(evidence.get("url") or "")
    dom = evidence.get("dom")
    dom_hash = str(dom.get("sha256") or "") if isinstance(dom, Mapping) else ""
    if store.normalize_domain(url) != DOMAIN or len(dom_hash) != 64 or any(c not in "0123456789abcdef" for c in dom_hash):
        raise FinancialManualBlocked("stage_evidence_required")
    screen = str(dom.get("text") or "") if isinstance(dom, Mapping) else ""
    if hashlib.sha256(screen.encode()).hexdigest() != dom_hash or not all(
        marker in screen for marker in _STAGE_MARKERS[stage]
    ):
        raise FinancialManualBlocked("stage_screen_assertion_failed")
    if stage == "verify" and not _BALANCE_PATTERN.search(screen):
        raise FinancialManualBlocked("account_balance_evidence_required")
    body = {"stage": stage, "origin": DOMAIN, "dom_sha256": dom_hash, "predecessor_sha256": prior_hash}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**body, "receipt_sha256": digest}


def verify_receipt(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("stage") not in STAGES or receipt.get("origin") != DOMAIN:
        return False
    dom_hash = receipt.get("dom_sha256")
    prior_hash = receipt.get("predecessor_sha256")
    if not isinstance(dom_hash, str) or len(dom_hash) != 64 or not isinstance(prior_hash, str):
        return False
    body = {key: receipt.get(key) for key in ("stage", "origin", "dom_sha256", "predecessor_sha256")}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest == receipt.get("receipt_sha256")


async def assert_approved_execution(recipe: WorkRecipe, tenant_id: Any, session_id: str | None,
                                    work_key: str | None, actor_id: str) -> None:
    """Read authority from registration DB and PC session registry, not callbacks."""
    validate_manifest(recipe)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT r.status, r.spec AS requested_spec, r.recipe_id, w.enabled,
                      w.spec AS approved_spec, w.version AS approved_version
                 FROM work_recipe_registration_requests r
                 JOIN work_recipes w ON w.id = r.recipe_id AND w.tenant_id = r.tenant_id
                WHERE r.tenant_id=$1::uuid AND w.name=$2 AND w.domain=$3 AND w.version=$4
                  AND r.status='approved' AND w.enabled=TRUE
                ORDER BY r.decided_at DESC LIMIT 1""",
            str(tenant_id), recipe.name, store.normalize_domain(recipe.domain), recipe.version,
        )
    if not row or row["status"] != "approved" or row["enabled"] is not True:
        raise FinancialManualBlocked("approved_registration_required")
    try:
        approved = row["approved_spec"]
        requested = row["requested_spec"]
        approved = json.loads(approved) if isinstance(approved, str) else approved
        requested = json.loads(requested) if isinstance(requested, str) else requested
        approved_recipe = WorkRecipe.from_dict(approved)
        requested_recipe = WorkRecipe.from_dict(requested)
        expected = recipe.to_dict()
        registered = approved_recipe.to_dict()
        proposed = requested_recipe.to_dict()
        registered_version = row["approved_version"]
        version_matches = int(registered_version) == recipe.version == approved_recipe.version
        for spec in (expected, registered, proposed):
            spec.pop("version", None)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise FinancialManualBlocked("approved_recipe_mismatch") from None
    if not version_matches or expected != registered or registered != proposed:
        raise FinancialManualBlocked("approved_recipe_mismatch")
    session = get_browser_bridge_service().sessions.get(str(session_id or ""))
    if (session is None or session.is_expired
            or session.endpoint.kind is not BrowserEndpointKind.LOCAL_AGENT
            or not str(session.work_key).startswith("yeoljeong-bank-shinhan-")
            or session.work_key != work_key):
        raise FinancialManualBlocked("authenticated_pc_session_required")
    agent_id = str(session.endpoint.metadata.get("agent_id") or "")
    agent_status = pc_agent_manager.get_agent_status(agent_id) if agent_id else None
    agent = pc_agent_manager.get_agent(agent_id) if agent_id else None
    if (not agent_status or agent_status.get("status") != "online" or agent is None
            or not actor_id or str(agent.user_id) != actor_id
            or str(agent.tenant_id) != str(tenant_id)):
        raise FinancialManualBlocked("authenticated_pc_session_required")


def _public_stage_result(result: Mapping[str, Any], receipt: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Persist fingerprints only, never account/credential-bearing snapshots."""
    ok = result.get("ok") is True
    evidence: dict[str, Any] = {}
    if receipt is not None:
        evidence["stage_receipt"] = dict(receipt)
    return {"ok": ok, "status": "success" if ok else "failed",
            "error": "" if ok else "financial_step_failed", "output": None,
            "evidence": evidence}


class StageEvidenceExecutor:
    """Wrap the real browser executor and chain observed stage evidence."""

    def __init__(self, executor: Any, boundaries: tuple[int, int, int]):
        self.executor = executor
        self.boundaries = boundaries
        self.receipts: list[dict[str, str]] = []
        self.last_step_seq = 0
        self.last_verify_seq = 0

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        seq = int(payload["seq"])
        if payload.get("phase") == "verify":
            if len(self.receipts) != len(STAGES):
                return {"status": "blocked", "ok": False, "error": "predecessor_evidence_required"}
            if seq <= self.last_verify_seq:
                return {"status": "blocked", "ok": False, "error": "stage_replay_forbidden"}
            self.last_verify_seq = seq
            return _public_stage_result(await self.executor(payload))
        if payload.get("phase", "step") != "step" or seq <= self.last_step_seq:
            return {"status": "blocked", "ok": False, "error": "stage_replay_forbidden"}
        stage_index = next((i for i, end in enumerate(self.boundaries) if seq <= end), None)
        if stage_index is None:
            return {"status": "blocked", "ok": False, "error": "stage_out_of_range"}
        if stage_index > len(self.receipts):
            return {"status": "blocked", "ok": False, "error": "predecessor_evidence_required"}
        if stage_index < len(self.receipts):
            return {"status": "blocked", "ok": False, "error": "stage_replay_forbidden"}
        self.last_step_seq = seq
        result = await self.executor(payload)
        if not result.get("ok") or seq != self.boundaries[stage_index]:
            return _public_stage_result(result)
        predecessor = self.receipts[-1] if self.receipts else None
        try:
            receipt = receipt_for(STAGES[stage_index], result.get("evidence") or {}, predecessor)
        except FinancialManualBlocked as exc:
            return {"status": "blocked", "ok": False, "error": str(exc)}
        self.receipts.append(receipt)
        return _public_stage_result(result, receipt)


class FinancialExecutionLock:
    """Hold the shared bank collector lock throughout the replay."""

    def __enter__(self):
        self.fd = try_acquire_bank_lock(default_bank_lock_path())
        if self.fd is None:
            raise FinancialManualBlocked("financial_exclusive_busy")
        return self

    def __exit__(self, *_: object) -> None:
        release_bank_lock(self.fd)
