"""smart_browser register_e2e / register_batch 의 초안 등록 — 단건과 일괄이 같은 경로를 쓴다.

등록(초안 생성)과 승인은 분리돼 있다. 여기서는 어떤 경우에도 승인하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.services.work_recipe import recorder as recorder_module
from app.services.work_recipe import registration as registration_module
from app.services.work_recipe.audit import mask_url
from app.services.work_recipe.dom_evidence import DomEvidenceError, verify_dom_evidence

MAX_BATCH_PIECES = 20
_SELF_REPORTED_KEYS = ("screenshot_url", "snapshot_ref", "url", "captured_at", "tool")


def piece_fingerprint(inp: Mapping[str, Any], verify: list[dict[str, Any]], extra: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {
            "name": str(inp.get("recipe_name") or "").strip(),
            "domain": str(inp.get("domain") or "").strip(),
            "steps": inp.get("steps"),
            "verify": verify,
            "meta": dict(extra),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _screen_evidence(
    evidence: Mapping[str, Any], *, tenant_id: str, session_id: str, domain: str, verify: Any,
) -> dict[str, Any]:
    if evidence.get("artifact_id"):
        trusted = await verify_dom_evidence(
            tenant_id=tenant_id, chat_session_id=session_id, claimed=evidence,
            domain=domain, verify=verify,
        )
        return {
            "screen_verified": True, "trust": "server_issued",
            **{key: trusted[key] for key in
               ("artifact_id", "screenshot_url", "url", "captured_at", "expires_at", "binding_hash")},
        }
    if evidence.get("screen_verified") is not True:
        raise ValueError("screen_e2e_evidence_required")
    if not str(evidence.get("screenshot_url") or evidence.get("snapshot_ref") or "").strip():
        raise ValueError("screenshot_or_snapshot_reference_required")
    # 참조만 남긴다. 원본 DOM/OCR·임의 필드에는 비밀값이 섞일 수 있고, 자기신고라 신뢰 등급은 낮다.
    safe = {key: mask_url(str(evidence[key])[:1000]) for key in _SELF_REPORTED_KEYS
            if evidence.get(key) is not None}
    return {**safe, "screen_verified": True, "trust": "self_reported"}


async def register_generic_draft(
    *, tenant_id: str, session_id: str, inp: Mapping[str, Any], idempotent: bool = False,
) -> dict[str, Any]:
    """검증된 초안 한 건을 만든다. 실패는 {"error": code}, 중복은 {"status": "duplicate"}."""
    name = str(inp.get("recipe_name") or "").strip()
    domain = str(inp.get("domain") or "").strip()
    steps = inp.get("steps")
    if not name or not domain or not isinstance(steps, list) or not steps:
        return {"error": "recipe_name_domain_and_steps_required"}
    if any(not isinstance(step, dict) for step in steps):
        return {"error": "invalid_recipe_step"}
    evidence = inp.get("e2e_evidence")
    if not isinstance(evidence, dict):
        return {"error": "screen_e2e_evidence_required"}

    try:
        top_verify, evidence_verify = inp.get("verify"), evidence.get("verify")
        if top_verify and evidence_verify and top_verify != evidence_verify:
            raise ValueError("verify_conflict")
        verify_steps = registration_module.normalize_verify_steps(top_verify or evidence_verify)
        flow_metadata = {
            **registration_module.normalize_flow_metadata(evidence),
            **registration_module.normalize_flow_metadata(inp),
        }
        piece_metadata = registration_module.normalize_piece_metadata(inp)
        screen = await _screen_evidence(
            evidence, tenant_id=tenant_id, session_id=session_id, domain=domain,
            verify=top_verify or evidence_verify,
        )
    except (ValueError, DomEvidenceError) as exc:
        return {"error": str(exc)}

    if idempotent:
        fingerprint = piece_fingerprint(inp, verify_steps, {**flow_metadata, **piece_metadata})
        key = piece_metadata.get("idempotency_key") or f"auto:{fingerprint[:40]}"
        piece_metadata = {**piece_metadata, "idempotency_key": key, "piece_fingerprint": fingerprint}
        existing = await registration_module.find_registration_by_idempotency_key(
            tenant_id=tenant_id, idempotency_key=key,
        )
        if existing is not None:
            stored = ((existing.get("spec") or {}).get("metadata") or {}).get("piece_fingerprint")
            if stored != fingerprint:
                return {"error": "idempotency_key_conflict", "idempotency_key": key}
            return {"status": "duplicate", "registration": existing, "idempotency_key": key}

    try:
        recording = recorder_module.start_recording(
            name, domain, tenant_id, session_id=session_id, e2e_evidence=screen,
            verify=verify_steps, flow_metadata={**flow_metadata, **piece_metadata},
        )
        for raw_step in steps:
            payload = dict(raw_step)
            succeeded = payload.pop("succeeded", True) is True
            recording.record_step(payload, succeeded=succeeded)
        row = await recorder_module.finish_recording(recording, created_by=f"chat:{session_id}")
    except (ValueError, registration_module.RegistrationError) as exc:
        return {"error": str(exc)}
    result: dict[str, Any] = {"status": "registered", "registration": row}
    if piece_metadata.get("idempotency_key"):
        result["idempotency_key"] = piece_metadata["idempotency_key"]
    return result


def _piece_result(index: int, piece: Mapping[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"index": index, "recipe_name": str(piece.get("recipe_name") or "")}
    if outcome.get("idempotency_key"):
        entry["idempotency_key"] = outcome["idempotency_key"]
    row = outcome.get("registration")
    if isinstance(row, Mapping):
        entry.update(registration_id=str(row.get("id") or ""), registration_status=str(row.get("status") or ""))
        spec = row.get("spec") if isinstance(row.get("spec"), Mapping) else {}
        entry["version"] = spec.get("version")
    entry["status"] = "error" if outcome.get("error") else str(outcome.get("status"))
    if outcome.get("error"):
        entry["error"] = str(outcome["error"])
    return entry


async def register_draft_batch(
    *, tenant_id: str, session_id: str, inp: Mapping[str, Any]
) -> dict[str, Any]:
    """단일 목적 조각 여러 개를 초안으로 등록한다. 한 조각의 실패가 다른 조각을 막지 않는다."""
    pieces = inp.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        return {"error": "pieces_required"}
    if len(pieces) > MAX_BATCH_PIECES:
        return {"error": f"too_many_pieces:max_{MAX_BATCH_PIECES}"}
    defaults = {key: inp[key] for key in ("domain", "lane") if inp.get(key) not in (None, "")}

    from app.services.work_recipe.approval_card import propose_card

    results: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces):
        if not isinstance(piece, Mapping):
            results.append({"index": index, "recipe_name": "", "status": "error", "error": "invalid_piece"})
            continue
        try:
            outcome = await register_generic_draft(
                tenant_id=tenant_id, session_id=session_id, inp={**defaults, **piece}, idempotent=True,
            )
            entry = _piece_result(index, piece, outcome)
            if outcome.get("status") == "registered":
                card = await propose_card(outcome["registration"], chat_session_id=session_id)
                entry["approval_card"] = card.get("status")
        except Exception as exc:  # noqa: BLE001 — 조각 하나의 예외가 나머지를 막지 않는다
            entry = {"index": index, "recipe_name": str(piece.get("recipe_name") or ""),
                     "status": "error", "error": f"registration_failed:{type(exc).__name__}"}
        results.append(entry)

    failed = sum(1 for item in results if item["status"] == "error")
    registered = sum(1 for item in results if item["status"] == "registered")
    status = "failed" if failed == len(results) else "partial" if failed else "ok"
    return {
        "status": status, "session_id": session_id, "approval_required": registered > 0,
        "counts": {"registered": registered, "duplicate": sum(1 for i in results if i["status"] == "duplicate"),
                   "error": failed},
        "results": results,
    }
