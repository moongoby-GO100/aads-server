"""담당 세션이 없을 때 — **승인 카드를 올리고, 승인된 뒤에만 만든다.**

## 왜 이 모듈이 있나

두 원칙이 서로 부딪히고 있었다.

1. `add_goal_owner` 는 **세션을 만들지 않는다**(`app/routers/goals.py`).
   "채팅창이 늘어나는 것을 대표님이 모르시는 상태가 되면 안 된다."
2. `goal_dispatch` 는 담당 역할(`owner_role_key`)에 맞는 세션이 없으면
   `skipped += 1` 로 **조용히 건너뛰었다.**

①은 옳다. 그런데 ②가 기록조차 남기지 않아서, 담당이 없는 마일스톤은
영원히 방치됐다 — 착수 상태로 열려 있고, 아무도 말을 걸지 않고, 어디에도
"담당이 없어서 못 보냈다" 가 적히지 않는다. 조용한 방치는 이 시스템에서
제일 나쁜 실패다.

2026-09-17 CEO 지시로 셋째 길을 낸다. **주도 세션이 생성 승인을 요청하고,
CEO 가 승인한 뒤에만 만든다.** ①의 원칙(모르는 채로 늘어나지 않는다)은
그대로 지키면서 ②의 침묵을 없앤다.

## 승인 없이 만드는 경로를 만들지 마라

`provision_owner_session` 은 **승인된 카드에서만** 호출되어야 한다. 편의를
위해 다른 곳에서 부르기 시작하면 ①이 무너진다 — 대표님이 모르는 채로
채팅창이 늘어난다.

## 둘 다 멱등이다

- 요청(`request_owner_session`): `work_key` 가 `owner-session:<project>:<role>`
  이므로, 같은 역할에 대해 대기 중인 카드가 있으면 새로 만들지 않는다.
  스케줄러는 몇 분마다 같은 마일스톤을 다시 본다 — 멱등이 아니면 카드가 쌓인다.
- 생성(`provision_owner_session`): 같은 워크스페이스에 같은 `role_key` 세션이
  이미 있으면 만들지 않고 **그것을 연결한다.** 승인 버튼을 두 번 누르거나
  일괄 승인에 같은 건이 섞여도 채팅창이 둘로 늘지 않는다.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger(__name__)

ACTION_TYPE = "create_owner_session"
GATE_SOURCE = "goal_owner"

# 카드 유효 기간. 실매매 게이트(2~24시간)보다 길다 — 이건 "지금 막혀 있다"
# 가 아니라 "담당을 세울까요" 라서, 대표님이 하루 뒤에 보셔도 뜻이 남는다.
_EXPIRES_INTERVAL = "48 hours"


def work_key_for(project: str, role_key: str) -> str:
    """같은 프로젝트의 같은 역할이면 같은 키.

    `next_step` 처럼 해시를 쓰지 않는다. 담당은 (프로젝트, 역할) 하나뿐이라
    본문이 달라져도 같은 요청이어야 한다 — 목표가 둘이어도 만들 세션은 하나다.
    """
    return f"owner-session:{(project or '').strip()}:{(role_key or '').strip()}"


def session_title_for(role_key: str) -> str:
    return f"[{(role_key or '').strip()}] 담당"


async def _has_role_prompt(conn, role_key: str) -> bool:
    """그 역할의 프롬프트가 있나.

    없으면 만들어도 **자기가 무엇을 하는 사람인지 모르는 채로 시작한다.**
    카드에 그대로 적어서 대표님이 알고 누르시게 한다.
    """
    if not role_key:
        return False
    try:
        return bool(await conn.fetchval(
            "SELECT 1 FROM prompt_assets WHERE enabled "
            "  AND role_scope @> ARRAY[$1]::text[] LIMIT 1",
            role_key,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner_session_prompt_check_failed error=%s", str(exc)[:160])
        return False


def _summary(
    *,
    goal_title: str,
    role_key: str,
    stuck: int,
    title: str,
    has_prompt: bool,
) -> str:
    """카드 본문. **대표님이 이것만 읽고 판단하실 수 있어야 한다.**

    다섯을 반드시 담는다 — 어느 목표인지, 몇 건이 멈춰 있는지, 무엇을 만드는지,
    역할 프롬프트가 있는지, 그리고 되돌리는 법.
    """
    lines = [
        f"[담당 세션 생성] {goal_title}",
        f"· 멈춘 마일스톤: {stuck}건 (담당 역할 `{role_key}` 에 맞는 채팅창이 없음)",
        f"· 만들 채팅창: \"{title}\" (role_key=`{role_key}`)",
    ]
    if has_prompt:
        lines.append(f"· 역할 프롬프트: 있음 — 생성 즉시 `{role_key}` 프롬프트가 결합됩니다")
    else:
        lines.append(
            f"· ⚠️ '{role_key}' 역할 프롬프트가 없어 이 담당은 자기가 무엇을 하는 "
            "사람인지 모르는 채로 시작합니다"
        )
    lines.append(
        "· 되돌리는 법: 생성된 세션을 삭제하고 goal_task_links 의 해당 링크를 "
        "link_state='revoked' 로 내립니다"
    )
    return "\n".join(lines)[:1000]


async def request_owner_session(
    *,
    goal_id: str,
    role_key: str,
    project: str,
    requester_session_id: str,
) -> Dict[str, Any]:
    """주도 세션 이름으로 생성 승인 카드를 올린다.

    돌려주는 것:
        request_id  카드 id
        reused      이미 대기 중이던 카드를 쓴 것인가
        work_key    멱등 키
        has_prompt  역할 프롬프트 유무
        stuck       담당 없음으로 멈춰 있는 마일스톤 수
    """
    from app.core.db_pool import get_pool

    role_key = (role_key or "").strip()
    project = (project or "").strip()
    requester_session_id = (requester_session_id or "").strip()
    if not role_key:
        return {"error": "role_key 가 비어 있어 담당 세션을 요청할 수 없습니다"}
    if not requester_session_id:
        return {"error": "요청을 올릴 주도 세션이 없습니다"}

    key = work_key_for(project, role_key)

    async with get_pool().acquire() as conn:
        # ① 같은 역할로 대기 중인 카드가 있으면 그것을 쓴다. 스케줄러가
        #    몇 분마다 같은 마일스톤을 다시 보기 때문에, 여기서 막지 않으면
        #    대기 목록이 같은 카드로 뒤덮인다.
        existing = await conn.fetchval(
            "SELECT id::text FROM agent_permission_requests "
            " WHERE work_key = $1 AND decision = 'pending' AND expires_at > now() "
            " ORDER BY created_at DESC LIMIT 1",
            key,
        )
        if existing:
            return {"request_id": existing, "reused": True, "work_key": key}

        # ② 요청자(주도) 세션에서 tenant/workspace 를 가져온다. 새 담당은
        #    주도와 같은 워크스페이스에 서야 목표 현황에 함께 보인다.
        requester = await conn.fetchrow(
            "SELECT tenant_id::text AS tenant_id, workspace_id::text AS workspace_id "
            "  FROM chat_sessions WHERE id = $1::uuid",
            requester_session_id,
        )
        if not requester or not requester["tenant_id"]:
            return {"error": "주도 세션을 찾지 못했습니다"}

        goal = await conn.fetchrow(
            """
            SELECT g.title AS goal_title, COALESCE(g.project, '') AS project,
                   (SELECT count(*) FROM milestones m
                     WHERE m.goal_id = g.id
                       AND m.owner_role_key = $2
                       AND m.owner_session_id IS NULL
                       AND m.status NOT IN ('completed', 'cancelled'))::int AS stuck
              FROM goals g WHERE g.id = $1::uuid
            """,
            goal_id, role_key,
        )
        if not goal:
            return {"error": "목표를 찾지 못했습니다"}

        has_prompt = await _has_role_prompt(conn, role_key)
        title = session_title_for(role_key)
        # 카드가 살아 있는 동안 승인 처리기가 읽는 유일한 사실 묶음이다.
        # 여기서 빠뜨린 값은 승인 시점에 되살릴 방법이 없다.
        scope = {
            "scope": "single_call",
            "goal_id": str(goal_id),
            "role_key": role_key,
            "project": project or str(goal["project"] or ""),
            "workspace_id": str(requester["workspace_id"] or ""),
            "requester_session_id": requester_session_id,
            "has_prompt": bool(has_prompt),
        }

        import json as _json

        new_id = await conn.fetchval(
            """
            INSERT INTO agent_permission_requests
                (tenant_id, work_key, origin, action_type, action_summary,
                 risk_level, decision, requested_by, approval_scope,
                 max_executions, expires_at, created_at, gate_source, tier)
            VALUES ($1::uuid, $2, 'chat_session', $3, $4,
                    'low', 'pending', $5, $6::jsonb,
                    1, now() + interval '""" + _EXPIRES_INTERVAL + """', now(), $7, 'approve')
            RETURNING id::text
            """,
            requester["tenant_id"], key, ACTION_TYPE,
            _summary(
                goal_title=str(goal["goal_title"] or "(제목 없음)"),
                role_key=role_key,
                stuck=int(goal["stuck"] or 0),
                title=title,
                has_prompt=has_prompt,
            ),
            requester_session_id, _json.dumps(scope, ensure_ascii=False), GATE_SOURCE,
        )

    logger.info(
        "owner_session_requested request=%s role=%s project=%s stuck=%s prompt=%s",
        str(new_id)[:8], role_key, project, int(goal["stuck"] or 0), has_prompt,
    )
    return {
        "request_id": new_id,
        "reused": False,
        "work_key": key,
        "has_prompt": has_prompt,
        "stuck": int(goal["stuck"] or 0),
        "session_title": title,
    }


async def provision_owner_session(approval_scope: Dict[str, Any]) -> Dict[str, Any]:
    """승인된 카드로 담당 세션을 세운다. **승인 경로에서만 부른다.**

    돌려주는 것:
        created           새로 만들었나 (False = 이미 있던 세션을 연결)
        session_id        담당 세션 id
        session_title     담당 세션 제목
        linked_milestones 이 세션이 맡게 된 마일스톤 수
        has_prompt        역할 프롬프트 유무
    """
    from app.core.db_pool import get_pool

    scope = approval_scope or {}
    role_key = str(scope.get("role_key") or "").strip()
    goal_id = str(scope.get("goal_id") or "").strip()
    workspace_id = str(scope.get("workspace_id") or "").strip()
    requester_session_id = str(scope.get("requester_session_id") or "").strip()
    if not role_key or not workspace_id:
        raise ValueError("owner_session_scope_incomplete")

    pool = get_pool()
    title = session_title_for(role_key)

    created = False
    session_id = ""
    tenant_id = ""
    user_id: Optional[str] = None

    async with pool.acquire() as conn:
        has_prompt = await _has_role_prompt(conn, role_key)

        # ① 이미 있으면 만들지 않는다. 일괄 승인에 같은 건이 섞이거나
        #    버튼을 두 번 눌러도 채팅창이 둘로 늘면 안 된다.
        existing = await conn.fetchrow(
            "SELECT id::text AS id, title FROM chat_sessions "
            " WHERE workspace_id = $1::uuid AND role_key = $2 "
            " ORDER BY updated_at DESC LIMIT 1",
            workspace_id, role_key,
        )
        if existing:
            session_id = str(existing["id"])
            title = str(existing["title"] or title)
        else:
            ws = await conn.fetchrow(
                "SELECT tenant_id::text AS tenant_id FROM chat_workspaces "
                " WHERE id = $1::uuid",
                workspace_id,
            )
            if not ws or not ws["tenant_id"]:
                raise ValueError("owner_session_workspace_not_found")
            tenant_id = str(ws["tenant_id"])
            user_id = await _requester_user_id(conn, requester_session_id)

    # 생성은 커넥션 밖에서 한다 — `create_session` 이 자기 커넥션을 잡는다.
    if not session_id:
        from app.services import chat_service as cs

        row = await cs.create_session(
            {"workspace_id": workspace_id, "title": title, "role_key": role_key},
            tenant_id=tenant_id,
            user_id=user_id,
        )
        session_id = str(row.get("id") or "")
        title = str(row.get("title") or title)
        created = True

    if not session_id:
        raise ValueError("owner_session_create_failed")

    async with pool.acquire() as conn:
        # ② 목표에 붙인다. 붙이지 않으면 목표 현황에 나오지 않는다.
        if goal_id:
            await conn.execute(
                "INSERT INTO goal_task_links (goal_id, task_type, task_id, status, "
                "       bind_source, bound_by, link_state) "
                "VALUES ($1::uuid, 'chat_session', $2, 'active', "
                "        'goal_owner_approval', 'ceo', 'active') "
                "ON CONFLICT DO NOTHING",
                goal_id, session_id,
            )
        # ③ 담당 없이 멈춰 있던 마일스톤을 이 세션에 넘긴다. `dispatch_note`
        #    는 NULL 로 되돌린다 — "담당 세션 없음" 은 이제 사실이 아니다.
        linked = 0
        if goal_id:
            linked = int(await conn.fetchval(
                """
                WITH upd AS (
                    UPDATE milestones
                       SET owner_session_id = $3::uuid, dispatch_note = NULL,
                           dispatch_blocked_at = NULL,
                           updated_at = NOW()
                     WHERE goal_id = $1::uuid
                       AND owner_role_key = $2
                       AND owner_session_id IS NULL
                    RETURNING 1
                )
                SELECT count(*)::int FROM upd
                """,
                goal_id, role_key, session_id,
            ) or 0)

    logger.warning(
        "owner_session_provisioned session=%s role=%s created=%s linked=%s prompt=%s",
        session_id[:8], role_key, created, linked, has_prompt,
    )
    return {
        "created": created,
        "session_id": session_id,
        "session_title": title,
        "linked_milestones": linked,
        "has_prompt": has_prompt,
    }


async def _requester_user_id(conn, requester_session_id: str) -> Optional[str]:
    """새 세션의 소유자. 컬럼이 없는 배포본도 있으므로 먼저 확인한다."""
    if not requester_session_id:
        return None
    try:
        has_user_id = bool(await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            " WHERE table_schema = 'public' AND table_name = 'chat_sessions' "
            "   AND column_name = 'user_id' LIMIT 1"
        ))
        if not has_user_id:
            return None
        return await conn.fetchval(
            "SELECT user_id FROM chat_sessions WHERE id = $1::uuid",
            requester_session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner_session_user_lookup_failed error=%s", str(exc)[:160])
        return None


async def note_owner_session_rejected(approval_scope: Dict[str, Any]) -> int:
    """거절도 기록으로 남는다.

    남기지 않으면 그 마일스톤은 "담당 세션 없음 — 생성 승인 요청함" 에 멈춘
    채로 보이고, 다음 사람은 승인 대기 중인 줄 안다. **거절은 결론이다.**
    """
    from app.core.db_pool import get_pool

    scope = approval_scope or {}
    role_key = str(scope.get("role_key") or "").strip()
    goal_id = str(scope.get("goal_id") or "").strip()
    if not role_key or not goal_id:
        return 0
    try:
        async with get_pool().acquire() as conn:
            return int(await conn.fetchval(
                """
                WITH upd AS (
                    UPDATE milestones
                       SET dispatch_note = '담당 세션 생성 거절됨',
                           dispatch_blocked_at = COALESCE(dispatch_blocked_at, NOW()),
                           updated_at = NOW()
                     WHERE goal_id = $1::uuid
                       AND owner_role_key = $2
                       AND owner_session_id IS NULL
                       AND (dispatch_note IS DISTINCT FROM '담당 세션 생성 거절됨'
                            OR dispatch_blocked_at IS NULL)
                    RETURNING 1
                )
                SELECT count(*)::int FROM upd
                """,
                goal_id, role_key,
            ) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner_session_reject_note_failed error=%s", str(exc)[:160])
        return 0
