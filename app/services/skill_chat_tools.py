"""채팅 도구 skill_find / skill_execute (PRD RUN-1~3).

새 실행기를 만들지 않는다 — 기존 하네스 서비스(find_skills / execute_skill)를 호출한다.

- read 등급은 즉시 실행한다.
- write/deploy/auth/financial 은 live_trading_guard 승인(TIER_APPROVE) 후에만 실행한다.
- destructive 와 active 가 아닌 버전(candidate 등)은 어떤 경우에도 실행하지 않는다.
- 결과는 execute_skill 이 기존 ops_skill_runs 에 기록한다.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.services import ohvis_harness as harness

_TIER_ORDER = ("read", "write", "deploy", "auth", "financial", "destructive")
_GUARD_RISK_LEVEL = {"write": "high", "deploy": "high", "auth": "critical", "financial": "critical"}
_RESULT_FIELDS = ("status", "result_hash", "latency_ms", "error", "approval_id")


def _error(code: str, message: str = "", **extra: Any) -> dict[str, Any]:
    return {"error": code, "message": message or code, **extra}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def stricter_tier(*tiers: str) -> str:
    """가장 엄격한 등급. 알 수 없는 값은 가장 엄격한 쪽(destructive)으로 본다."""
    rank = {tier: index for index, tier in enumerate(_TIER_ORDER)}
    return max((str(t) for t in tiers), key=lambda t: rank.get(t, len(_TIER_ORDER)))


async def skill_find(*, inp: Mapping[str, Any]) -> dict[str, Any]:
    query = str(inp.get("query") or "").strip()
    if not query:
        return _error("invalid_input", "query 가 필요합니다")
    try:
        limit = max(1, min(int(inp.get("limit") or 5), 20))
    except (TypeError, ValueError):
        return _error("invalid_input", "limit 은 정수여야 합니다")
    result = await harness.find_skills(
        query=query,
        project=str(inp["project"]) if inp.get("project") else None,
        intent=str(inp["intent"]) if inp.get("intent") else None,
        limit=limit,
    )
    return _json_safe(result)


async def _load_skill(pool: Any, tenant_id: str, ref: str) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT l.id::text AS skill_id,l.slug,l.risk_tier AS library_tier,l.enabled,
                      v.version,v.status,v.manifest::text AS manifest_text
               FROM ops_skill_library l LEFT JOIN ops_skill_versions v ON v.skill_id=l.id
               WHERE l.tenant_id=$1::uuid AND (l.slug=$2 OR l.id::text=$2)
               ORDER BY v.created_at DESC""",
            tenant_id, ref,
        )
    return [dict(row) for row in rows]


def _pick_version(rows: list[dict[str, Any]], requested: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    versions = [r for r in rows if r.get("version")]
    if requested:
        row = next((r for r in versions if r["version"] == requested), None)
        if row is None:
            return None, _error("skill_version_not_found", f"버전 {requested} 이 없습니다")
        if row["status"] != "active":
            return None, _error(
                "version_not_active",
                f"버전 {requested} 은 {row['status']} 상태입니다. active 로 승격된 버전만 실행할 수 있습니다",
                version_status=row["status"],
            )
        return row, None
    active = next((r for r in versions if r["status"] == "active"), None)
    if active is None:
        statuses = sorted({str(r["status"]) for r in versions})
        return None, _error(
            "no_active_version",
            "active 버전이 없습니다. candidate 는 실행하지 않으며 CEO 승격 후에만 실행할 수 있습니다",
            version_statuses=statuses,
        )
    return active, None


def _build_intent(tenant_id: str, session_id: str, payload: dict[str, Any], correlation_id: str):
    from app.services.channel_router import ChannelRouter, directive_from_authenticated_context

    context = {"tenant": {"id": tenant_id}, "membership": {"user_id": f"chat_session:{session_id}"}}
    envelope = directive_from_authenticated_context(
        context, session_id=session_id, correlation_id=correlation_id, payload=payload,
        capabilities=frozenset({"skill.execute"}), source="internal_control",
    )
    return ChannelRouter().route_directive(envelope, capability="skill.execute")


async def skill_execute(
    *, tenant_id: str, session_id: str, inp: Mapping[str, Any], pool: Any = None,
) -> dict[str, Any]:
    ref = str(inp.get("skill") or inp.get("slug") or inp.get("skill_id") or "").strip()
    if not ref:
        return _error("invalid_input", "skill(slug 또는 id) 가 필요합니다")
    input_data = inp.get("input") if inp.get("input") is not None else {}
    if not isinstance(input_data, dict):
        return _error("invalid_input", "input 은 객체여야 합니다")
    if not session_id:
        return _error("missing_session_id", "skill_execute 는 채팅 세션 안에서만 실행됩니다")
    if pool is None:
        from app.core.db_pool import get_pool
        pool = get_pool()

    rows = await _load_skill(pool, tenant_id, ref)
    if not rows:
        return _error("skill_not_found", f"스킬 {ref} 을 찾을 수 없습니다")
    if not rows[0]["enabled"]:
        return _error("skill_disabled", "비활성화된 스킬입니다")
    row, problem = _pick_version(rows, str(inp.get("version") or "").strip())
    if problem:
        return problem
    assert row is not None

    try:
        manifest = harness.validate_skill_manifest(json.loads(row["manifest_text"]))
        tier = stricter_tier(manifest["risk_tier"], row["library_tier"])
        if tier not in harness.RISK_POLICIES:
            return _error("unknown_risk_tier", f"알 수 없는 등급 {tier}")
        harness._enforce_executable_risk_policy({"risk_tier": tier})
        executable, reason_ko = harness.skill_executability(manifest["executor"])
        if not executable:
            return _error("skill_not_executable", reason_ko or "", status_code=409)
        if manifest["executor"] not in harness._SKILL_EXECUTORS:
            return _error("executor_not_registered", "코드에 등록된 실행기가 없습니다")
        harness._validate_instance(manifest["input_schema"], input_data, "input_schema_validation_failed")
    except harness.SkillRegistryError as exc:
        return _error(exc.code, exc.detail, status_code=exc.status_code)
    except (TypeError, ValueError) as exc:
        return _error("stored_skill_contract_mismatch", str(exc)[:200])

    skill_id, version = row["skill_id"], row["version"]
    input_hash = harness._sha256(input_data)
    high_risk = tier != "read"
    if high_risk:
        from app.services import live_trading_guard as guard

        gate_input = {
            "target": f"skill:{skill_id}:{version}:{input_hash}",
            "task": f"스킬 {row['slug']} v{version} 실행 (등급 {tier})",
        }
        risk_level = _GUARD_RISK_LEVEL[tier]
        if not await guard.is_approved("skill_execute", gate_input, session_id, risk_level):
            request_id = await guard.request_approval(
                "skill_execute", gate_input, f"{tier} 등급 스킬 실행 승인 필요",
                session_id=session_id, tenant_id=tenant_id, gate_source="skill_execute",
                tier=guard.TIER_APPROVE, risk_level=risk_level, label="스킬 실행",
            )
            if not request_id:
                return _error("approval_request_failed", "승인 요청을 만들지 못해 실행하지 않았습니다")
            return {
                "status": "approval_required", "approval_request_id": request_id,
                "skill": row["slug"], "version": version, "risk_tier": tier,
                "message": "대표님 승인 후 같은 호출을 다시 실행하세요",
            }

    idem = str(inp.get("idempotency_key") or "").strip() or None
    mode = manifest["idempotency"]["mode"]
    if mode == "required" and not idem:
        idem = "chat:" + hashlib.sha256(f"{session_id}:{skill_id}:{version}:{input_hash}".encode()).hexdigest()[:32]
    if mode == "forbidden":
        idem = None
    payload = {"skill_id": skill_id, "version": version, "input": input_data}
    try:
        intent = _build_intent(tenant_id, session_id, payload, idem or input_hash[:32])
        run = await harness.execute_skill(
            tenant_id=tenant_id, skill_id=skill_id, version=version, input_data=input_data,
            idempotency_key=idem, action_intent=intent, gateway_approved=high_risk,
        )
    except harness.SkillRegistryError as exc:
        return _error(exc.code, exc.detail, status_code=exc.status_code)
    except Exception as exc:  # noqa: BLE001 - ChannelRoutingError 등은 도구 오류로 돌려준다
        return _error("skill_execution_rejected", f"{type(exc).__name__}: {str(exc)[:200]}")

    output = run.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            pass
    return {
        "run_id": str(run.get("id")), "skill": row["slug"], "version": version, "risk_tier": tier,
        "output": _json_safe(output),
        **{key: _json_safe(run.get(key)) for key in _RESULT_FIELDS},
    }
