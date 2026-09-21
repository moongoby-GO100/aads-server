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

## auto 는 실행되지 않는다 (2026-09-21 대표님 지적)

`cards` 와 `auto` 는 수명이 전혀 다르다. 이걸 모르면 "자동 항목이 왜
자동으로 안 되냐" 는 질문을 반드시 받는다.

    cards → agent_permission_requests 에 행이 남는다
          → 대표님이 누르면 project_docs 승인 API 가
            "[시스템] 대표님이 다음 단계를 승인했습니다" 프롬프트를
            세션에 재주입한다 → 에이전트가 다시 불려 실행한다.

    auto  → 반환값 리스트에만 있다. DB 행 없음. 재주입 없음.
            턴이 끝나면 그대로 사라진다.

그래서 **승인이 필요한 항목이 자동 항목보다 더 확실히 실행된다** 는
역설이 생긴다. auto 를 실행하는 주체는 오직 에이전트 자신이고, 그것도
같은 턴 안에서여야 한다. 반환 `note` 가 그렇게 지시하는 이유다.

실행기를 붙일 생각이면 auto 를 `decision='approved'` 로 같은 테이블에
넣어 재주입 경로를 태우는 쪽이 새 큐를 만드는 것보다 안전하다 —
다만 승인 화면 표시를 먼저 확인해야 한다(미착수).
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


async def _session_project(conn, session_id: str) -> str:
    """이 세션이 속한 프로젝트 키(대문자). 모르면 빈 문자열."""
    if not session_id:
        return ""
    try:
        val = await conn.fetchval(
            "SELECT UPPER(COALESCE(w.project_key, '')) "
            "  FROM chat_sessions s "
            "  LEFT JOIN chat_workspaces w ON w.id = s.workspace_id "
            " WHERE s.id = $1::uuid",
            session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("next_step_project_lookup_failed error=%s", str(exc)[:160])
        return ""
    return str(val or "")


async def _covered_by_existing_grant(
    conn, session_id: str, tool: str, project: str = "",
) -> Optional[str]:
    """이미 받아 둔 승인이 이 제안을 덮고 있으면 그 승인 id 를 준다.

    2026-09-17 대표님 지적 — "승인 팝업이 목표 진행시에도 계속 뜬다".

    원인은 여기였다. 범위가 `mission`·`goal` 두 개일 때 쓴 조회가 그대로
    남아 있었는데, 그 사이 `session`·`project` 두 범위가 붙었다
    (`live_trading_guard.is_approved` 참고). 그래서 대표님이 "이 프로젝트
    전체 100회" 를 눌러 두셔도 다음 단계 카드는 계속 올라왔다 — 실측으로
    24시간에 next_step 카드 197장.

    **범위 목록은 `live_trading_guard.is_approved` 와 같이 움직여야 한다.**
    한쪽만 늘리면 그 범위는 실행 게이트만 통과하고 제안 카드는 계속 묻는다.
    """
    try:
        row = await conn.fetchrow(
            """
            SELECT r.id::text AS id
            FROM agent_permission_requests r
            WHERE r.decision = 'approved'
              AND r.expires_at > now()
              -- 그 도구를 덮는 승인이거나, 다음 단계 자체를 덮는 승인이거나.
              -- 대표님이 제안 카드에 "이 프로젝트 전체" 를 누르시면 그 승인의
              -- action_type 은 도구 이름이 아니라 'next_step' 이다. 그것만
              -- 보던 옛 조회는 그 승인을 영영 찾지 못했다(2026-09-17).
              AND r.action_type = ANY($2::text[])
              AND COALESCE((r.approval_scope->>'used')::int, 0)
                  < COALESCE(r.max_executions, 1)
              AND (
                    -- 골 승인은 목표를 따라가므로 세션을 묶지 않는다.
                    r.approval_scope->>'scope' = 'goal'
                    -- 미션·세션 승인은 그 대화 안에서만 유효하다.
                 OR (r.approval_scope->>'scope' IN ('mission', 'session')
                     AND r.requested_by = $1)
                    -- 프로젝트 승인은 세션을 넘는다. 프로젝트를 모르는
                    -- 호출($3 = '')은 여기에 걸리지 않는다.
                 OR ($3 <> '' AND r.approval_scope->>'scope' = 'project'
                     AND (
                          UPPER(COALESCE(r.approval_scope->>'project', '')) = $3
                          -- 옛 카드는 project 를 비워 두고 저장됐다. 그때는
                          -- 승인을 올린 세션의 프로젝트로 판정한다.
                       OR (COALESCE(r.approval_scope->>'project', '') = ''
                           AND r.requested_by ~ '^[0-9a-fA-F-]{36}$'
                           AND EXISTS (
                               SELECT 1 FROM chat_sessions s2
                               LEFT JOIN chat_workspaces w2
                                      ON w2.id = s2.workspace_id
                                WHERE s2.id = r.requested_by::uuid
                                  AND UPPER(COALESCE(w2.project_key, '')) = $3))))
              )
            -- 넓은 것부터 쓴다.
            ORDER BY CASE COALESCE(r.approval_scope->>'scope', 'single')
                         WHEN 'goal' THEN 0 WHEN 'project' THEN 1
                         WHEN 'session' THEN 2 ELSE 3 END,
                     r.decided_at DESC
            LIMIT 1
            """,
            session_id,
            [t for t in ((tool or "").strip(), ACTION_TYPE) if t],
            (project or "").upper(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("next_step_coverage_check_failed error=%s", str(exc)[:160])
        return None
    return row["id"] if row else None


async def _goal_policy_covers(conn, session_id: str, risk: str) -> Optional[str]:
    """목표에 걸어 둔 승인 설정이 이 제안을 덮는가. 덮으면 목표 id.

    2026-09-17 대표님 지적 — "목표 승인설정이 되면 자동승인 범위안에서는
    자동으로 진행되어야 하는거 아닌가".

    맞다. 그런데 그 설정(`goals.approval_policy`)은 실행 게이트
    (`live_trading_guard.goal_policy_allows`)만 보고 있었고, 제안 카드는
    보지 않았다. 그래서 목표를 진행하는 내내 "이걸 할까요" 가 계속 떴다.

    두 가지는 그대로 지킨다.

    1. **횟수 상한을 본다.** 설정에 남은 횟수가 없으면 다시 묻는다.
    2. **여기서 세지 않는다.** 이 함수는 "물어볼 필요가 있나" 만 보는
       읽기 전용이다. 실제 소비는 도구가 실행될 때 실행 게이트가 센다.
       여기서 같이 세면 한 번의 일에 두 번 깎인다.
    """
    if not session_id:
        return None
    field = "auto_approve_critical" if risk == "critical" else "auto_approve_high"
    try:
        row = await conn.fetchrow(
            """
            SELECT g.id::text AS id, g.title
            FROM goal_task_links l
            JOIN goals g ON g.id = l.goal_id
            WHERE l.task_type = 'chat_session' AND l.task_id = $1
              AND COALESCE(l.link_state, 'active') = 'active'
              AND g.status IN ('draft', 'active', 'blocked')
              AND COALESCE((g.approval_policy->>$2)::boolean, false)
              AND COALESCE((g.approval_policy->>'used')::int, 0)
                  < COALESCE((g.approval_policy->>'max_executions')::int, 0)
            ORDER BY g.updated_at DESC
            LIMIT 1
            """,
            session_id, field,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("next_step_goal_policy_failed error=%s", str(exc)[:160])
        return None
    return row["id"] if row else None


async def _current_bubble_id(conn, session_id: str) -> Optional[str]:
    """이 제안이 붙을 응답 버블(assistant 메시지)을 찾는다.

    호출 시점은 턴이 아직 흐르는 중이라 그 턴의 `streaming_placeholder` 가
    살아 있다. 그것이 지금 회장님 화면에서 자라고 있는 버블이다.

    없으면(이미 확정됐거나 placeholder 를 안 쓰는 경로) 가장 최근 assistant
    메시지로 떨어진다. 그것도 없으면 None 을 주고, 화면은 기존처럼 팝업으로만
    띄운다 — **붙일 자리를 모르면 안 붙이는 편이 낫다.** 아무 데나 붙이면
    회장님이 다른 답변의 제안을 승인하시게 된다.

    세 층을 프롬프트/도구 인자로 넘겨받지 않고 여기서 직접 찾는 이유는,
    넘겨받으려면 chat_service → model_selector → tool_executor 를 관통해야
    하고 그 경로 어디서든 빠지면 조용히 None 이 되기 때문이다.
    """
    try:
        row = await conn.fetchval(
            """
            SELECT id::text FROM chat_messages
            WHERE session_id = $1::uuid
              AND role = 'assistant'
              -- 화면에 버블로 그려지는 것에만 붙인다. 2026-09-18 실측으로
              -- 48시간 11장이 **아무 화면에도 없었다** — 7장은 숨김 메시지
              -- (`is_hidden`: 러너 진행·중단 조각)에, 4장은 이미 지워진
              -- 메시지에 붙어 있었다. 붙을 자리가 화면에 없으면 인라인으로
              -- 안 보이고, 화면은 source_message_id 가 있다는 이유로
              -- 팝업에서도 빼 버린다. 그래서 카드가 통째로 사라진다.
              -- 2026-09-18 재수정. 07:19 에 넣은 `is_hidden IS NOT TRUE` 가
              -- **지금 흐르는 placeholder 를 같이 잘랐다.** 스트리밍 중인
              -- placeholder 는 항상 is_hidden=true 이므로 아래 ORDER BY 의
              -- streaming_placeholder 우선 절이 죽은 코드가 됐고, 카드는
              -- 직전 확정 버블에 붙었다 — 07:24:46 실측으로 14시간 전
              -- (2026-09-17 17:43) 답변에 2장이 붙어 화면에서 사라졌다.
              -- 살아 있는 placeholder 는 예외로 받고, 확정되지 못하고 죽은
              -- placeholder(30분 초과)만 거른다.
              AND (is_hidden IS NOT TRUE
                   OR (COALESCE(intent, '') = 'streaming_placeholder'
                       AND created_at > now() - interval '30 minutes'))
              AND deleted_at IS NULL
              -- 러너 진행 메시지는 대화에서 한 줄로 접히므로 카드가 붙을
              -- 자리가 없다(대시보드 isRunnerChatMessage → runnerGroup).
              AND COALESCE(intent, '') NOT IN
                  ('pipeline_runner', 'runner_notification', 'ai_review_warning')
              AND COALESCE(content, '') NOT LIKE '%[Pipeline Runner]%'
              -- 흐르는 중인 버블이 없으면(또는 그것이 숨김이면) **이번 턴의
              -- 답변에만** 붙인다. 위 조건으로 숨김 placeholder 를 뺐으므로
              -- 이 절이 없으면 이전 턴의 확정 답변으로 떨어지고, 회장님은
              -- 다른 답변 아래에서 이번 제안을 승인하시게 된다(2026-09-17
              -- 에 카드 2장이 50분 전 버블에 붙은 것과 같은 사고).
              -- 모르면 붙이지 않는다 — 그 카드는 팝업으로 간다.
              AND (COALESCE(intent, '') = 'streaming_placeholder'
                   OR created_at > now() - interval '15 minutes')
            -- COALESCE 를 벗기지 마라. intent 가 NULL 이면 비교 결과가 NULL 이고,
            -- DESC 정렬에서 NULL 은 맨 앞에 온다(NULLS FIRST 가 기본). 그러면
            -- intent 없는 **옛 답변**이 지금 흐르는 placeholder 를 제치고 1등이
            -- 된다 — 2026-09-17 첫 실증에서 카드 2장이 50분 전 버블에 붙었다.
            ORDER BY COALESCE(intent = 'streaming_placeholder', false) DESC,
                     created_at DESC
            LIMIT 1
            """,
            session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("next_step_bubble_lookup_failed error=%s", str(exc)[:160])
        return None
    return row or None


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

        bubble_id = await _current_bubble_id(conn, session_id)
        project = await _session_project(conn, session_id)
        # 목표 승인 설정은 위험도마다 답이 다르다. 한 번 보고 재사용한다 —
        # 제안 다섯 개에 같은 조회를 다섯 번 돌릴 이유가 없다.
        goal_cache: Dict[str, Optional[str]] = {}

        for step in normalized:
            grant_id = await _covered_by_existing_grant(
                conn, session_id, step["tool"], project,
            )
            if grant_id:
                auto.append({"title": step["title"], "grant_id": grant_id[:8]})
                continue

            risk = step["risk"]
            if risk not in goal_cache:
                goal_cache[risk] = await _goal_policy_covers(conn, session_id, risk)
            goal_id = goal_cache[risk]
            if goal_id:
                auto.append({
                    "title": step["title"],
                    "grant_id": goal_id[:8],
                    "via": "goal_policy",
                })
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
                         max_executions, expires_at, created_at, gate_source, tier,
                         source_message_id)
                    VALUES ($1::uuid, $2, 'chat_session', $3, $4,
                            $5, 'pending', $6,
                            -- 프로젝트를 같이 남긴다. 승인 화면이 "이 프로젝트
                            -- 전체" 를 눌러도 여기가 비어 있으면 다음 카드가
                            -- 그 승인을 찾지 못한다(2026-09-17).
                            jsonb_build_object('scope', 'single_call',
                                               'project', $9::text),
                            1, now() + interval '24 hours', now(), $7, 'approve',
                            NULLIF($8, '')::uuid)
                    RETURNING id::text
                    """,
                    tid, work_key, ACTION_TYPE, _summary_of(step, context),
                    step["risk"], session_id, GATE_SOURCE, bubble_id or "",
                    project,
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
            "cards 는 대표님이 누르면 승인 프롬프트가 세션에 재주입되어 "
            "그때 수행하면 됩니다. "
            "auto 는 '승인을 물을 필요가 없다'는 뜻일 뿐, "
            "시스템이 대신 실행해 주지 않는다 — DB 행도 재주입 트리거도 없다. "
            "auto 항목은 이번 턴 안에 직접 실행하고 결과까지 보고하라. "
            "실행하지 않은 채 '자동 진행하겠습니다' 로 턴을 끝내면 그 항목은 "
            "그대로 사라진다."
        ),
    }
