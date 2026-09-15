"""다음 단계 제안을 승인 카드로 올린다.

2026-09-15 CEO 지시 — "대안 다음진행사항을 승인게이트에 올려 내가 승인한
권한 범위면 자동으로 다음단계를 실행될수 있게".

그 전까지 보고서 끝의 "→ 다음 단계 1·2·3" 은 그냥 글자였다. 어디에도
올라가지 않아서 회장님이 매번 "진행해" 를 치셔야 다음이 돌았다. 실측:
2026-09-15 하루에만 "다음단계 진행해" 계열 지시가 반복됐다.

제안을 카드로 올리면 체크 한 번으로 이어진다.

## 막힌 것을 푸는 카드와 다르다

`live_trading_guard` 카드는 **"하려다 막혔다"** 이고, 이 카드는
**"이걸 할까요"** 다. 그래서 `gate_source` 를 나눈다. 화면에서 섞이면
회장님이 둘을 같은 것으로 보고 습관적으로 누르게 되고, 그때 진짜
막아야 할 하나가 같이 통과한다.

## 이미 승인받은 범위는 묻지 않는다

제안에 `tool` 이 적혀 있고 그 도구를 덮는 미션·골 승인이 살아 있으면
카드를 만들지 않고 `auto` 로 돌려준다. 같은 것을 두 번 묻지 않는 것이
"끊김없이" 의 핵심이다.

**여기서 승인 여부를 판정하지 않는다.** 실제 실행 시점의 판정은
`live_trading_guard.is_approved()` 가 한다(횟수도 거기서 센다). 이 함수는
"물어볼 필요가 있나" 만 미리 본다 — 읽기 전용이다.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

GATE_SOURCE = "next_step"
ACTION_TYPE = "next_step"

# 한 번에 올릴 수 있는 제안 수. 열 개를 올리면 읽지 않고 누른다.
MAX_STEPS = 5

_RISK_LEVELS = ("low", "medium", "high")


def _work_key(session_id: str, title: str) -> str:
    """제목이 같으면 같은 키. 프로세스가 바뀌어도 같아야 한다.

    파이썬 내장 `hash()` 를 쓰면 프로세스마다 달라진다 — 그래서 승인이
    재사용되지 않던 버그가 있었다(2026-09-15, c65319ee 에서 수정).
    같은 실수를 반복하지 않는다.
    """
    digest = hashlib.sha1(title.encode("utf-8", "replace")).hexdigest()[:7]
    return f"{(session_id or '')[:8]}:{ACTION_TYPE}:{digest}"


async def _covered_by_existing_grant(conn, session_id: str, tool: str) -> Optional[str]:
    """이미 받아 둔 미션·골 승인이 이 도구를 덮고 있으면 그 승인 id 를 준다."""
    if not tool:
        return None
    try:
        row = await conn.fetchrow(
            """
            SELECT id::text AS id
            FROM agent_permission_requests
            WHERE decision = 'approved'
              AND expires_at > now()
              AND action_type = $2
              AND approval_scope->>'scope' IN ('mission', 'goal')
              AND COALESCE((approval_scope->>'used')::int, 0)
                  < COALESCE(max_executions, 1)
              -- 미션 승인은 그 대화 안에서만 유효하다. 골 승인은 목표를
              -- 따라가므로 세션을 묶지 않는다.
              AND (approval_scope->>'scope' <> 'mission' OR requested_by = $1)
            ORDER BY decided_at DESC
            LIMIT 1
            """,
            session_id, tool,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("next_step_coverage_check_failed error=%s", str(exc)[:160])
        return None
    return row["id"] if row else None


def _normalize(step: Any, index: int) -> Optional[Dict[str, str]]:
    """제안 하나를 정리한다. 제목이 없으면 버린다."""
    if not isinstance(step, dict):
        return None
    title = str(step.get("title") or "").strip()
    if not title:
        return None
    risk = str(step.get("risk") or "low").lower()
    if risk not in _RISK_LEVELS:
        risk = "low"
    return {
        "title": title[:200],
        "detail": str(step.get("detail") or "").strip()[:800],
        "tool": str(step.get("tool") or "").strip()[:64],
        "rollback": str(step.get("rollback") or "").strip()[:300],
        "risk": risk,
        "index": str(index + 1),
    }


def _summary_of(step: Dict[str, str], context: str) -> str:
    """카드에 보일 본문. 회장님이 이것만 읽고 판단하실 수 있어야 한다."""
    lines = [f"[다음단계 {step['index']}] {step['title']}"]
    if step["detail"]:
        lines.append(step["detail"])
    if step["tool"]:
        lines.append(f"· 실행 도구: {step['tool']}")
    lines.append(f"· 되돌리는 법: {step['rollback'] or '되돌릴 필요 없음(읽기/조사)'}")
    if context:
        lines.append(f"· 배경: {context[:200]}")
    return "\n".join(lines)[:1000]


async def propose(
    session_id: str,
    steps: List[Any],
    context: str = "",
    tenant_id: str = "",
) -> Dict[str, Any]:
    """제안을 카드로 올린다.

    돌려주는 것:
        proposed  올린 제안 수
        cards     승인이 필요한 것 (id, title)
        auto      이미 승인 범위 안이라 바로 해도 되는 것 (title, grant_id)
        skipped   제목이 없어 버린 것
    """
    from app.core.db_pool import get_pool

    if not session_id:
        return {"error": "세션을 알 수 없어 제안을 올리지 못했습니다"}

    normalized = [
        s for s in (_normalize(raw, i) for i, raw in enumerate(steps or []))
        if s is not None
    ]
    skipped = len(steps or []) - len(normalized)
    if not normalized:
        return {"error": "올릴 제안이 없습니다 (title 이 비어 있음)", "skipped": skipped}
    if len(normalized) > MAX_STEPS:
        normalized = normalized[:MAX_STEPS]
        skipped += 1

    pool = get_pool()
    cards: List[Dict[str, str]] = []
    auto: List[Dict[str, str]] = []

    async with pool.acquire() as conn:
        tid = tenant_id or ""
        if not tid:
            try:
                tid = str(await conn.fetchval(
                    "SELECT tenant_id::text FROM chat_sessions WHERE id = $1::uuid",
                    session_id,
                ) or "")
            except Exception:  # noqa: BLE001
                tid = ""
        if not tid:
            return {"error": "tenant 를 찾지 못했습니다"}

        for step in normalized:
            grant_id = await _covered_by_existing_grant(conn, session_id, step["tool"])
            if grant_id:
                auto.append({"title": step["title"], "grant_id": grant_id[:8]})
                continue

            work_key = _work_key(session_id, step["title"])
            try:
                # 같은 제안을 두 번 올리면 카드가 쌓인다. 대기 중인 같은
                # 제안이 있으면 그것을 쓴다.
                existing = await conn.fetchval(
                    "SELECT id::text FROM agent_permission_requests "
                    "WHERE work_key = $1 AND decision = 'pending' "
                    "  AND expires_at > now() "
                    "ORDER BY created_at DESC LIMIT 1",
                    work_key,
                )
                if existing:
                    cards.append({"id": existing, "title": step["title"], "reused": "1"})
                    continue

                new_id = await conn.fetchval(
                    """
                    INSERT INTO agent_permission_requests
                        (tenant_id, work_key, origin, action_type, action_summary,
                         risk_level, decision, requested_by, approval_scope,
                         max_executions, expires_at, created_at, gate_source, tier)
                    VALUES ($1::uuid, $2, 'chat_session', $3, $4,
                            $5, 'pending', $6, '{"scope": "single_call"}'::jsonb,
                            1, now() + interval '24 hours', now(), $7, 'approve')
                    RETURNING id::text
                    """,
                    tid, work_key, ACTION_TYPE, _summary_of(step, context),
                    step["risk"], session_id, GATE_SOURCE,
                )
                cards.append({"id": str(new_id), "title": step["title"]})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "next_step_proposal_failed title=%s error=%s",
                    step["title"][:40], str(exc)[:160],
                )

    logger.info(
        "next_step_proposed session=%s cards=%s auto=%s skipped=%s",
        session_id[:8], len(cards), len(auto), skipped,
    )
    return {
        "proposed": len(cards) + len(auto),
        "cards": cards,
        "auto": auto,
        "skipped": skipped,
        "note": (
            "승인하시면 그 제안을 제가 이어서 수행합니다. "
            "auto 로 표시된 것은 이미 승인 범위 안이라 묻지 않고 진행합니다."
        ),
    }
