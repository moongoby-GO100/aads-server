"""세션 간 협업 — 담당끼리 묻고 답한다.

2026-09-14. 담당 다섯을 세워도 서로 물어볼 방법이 없었다. 파동엔진이
"진입을 당기자" 할 때 실매매엔진의 슬리피지 의견을 받을 길이 없으면
한 담당의 개선이 다른 담당의 지표를 깎는 것을 아무도 못 잡는다.

**세션에 메시지를 넣고 답을 받는 경로는 이미 돈다** — 파이프라인 러너가
쓰는 길이다(7일 501건 주입 → 483건 응답, 96%). 여기서는 그 경로를 도구로
노출하고, **답이 물어본 쪽으로 돌아오게** 한다.

## 왜 비동기인가

오늘 실측한 응답 시간이 2분~61분이다. 물어본 쪽이 기다리면 먼저 죽는다 —
`llm_first_response_timeout` 이 정확히 그 증상이었다. 영수증만 주고
답은 나중에 새 메시지로 넣는다.

## 루프를 막지 못하면 만들면 안 된다

A→B→A→B 가 끝없이 돌 수 있다. 사람이 안 보는 사이 수백 번이다. 오늘
resume 자동 재시도가 시간당 1,256회까지 간 것이 같은 종류의 사고다.

세 겹으로 막는다 — 홉 수, 같은 쌍 중복, 자기 자신.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger(__name__)

MAX_HOP = int(os.getenv("SESSION_RELAY_MAX_HOP", "3"))
CONTEXT_LIMIT = int(os.getenv("SESSION_RELAY_CONTEXT_CHARS", "4000"))
ANSWER_LIMIT = int(os.getenv("SESSION_RELAY_ANSWER_CHARS", "6000"))

# 물어본 쪽이 응답 중이면 기다린다. 진행 중인 응답에 새 user 메시지가 들어가면
# 그 응답이 통째로 버려진다.
_DELIVER_WAIT_TRIES = int(os.getenv("SESSION_RELAY_DELIVER_WAIT_TRIES", "20"))
_DELIVER_WAIT_SEC = float(os.getenv("SESSION_RELAY_DELIVER_WAIT_SEC", "30"))

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# 백그라운드 태스크 참조를 붙든다.
#
# `asyncio.create_task` 결과를 버리면 가비지 컬렉터가 중간에 거둬 간다.
# 2026-09-14 에 `chat_messages.embedding` 이 정확히 그 방식으로 소리 없이
# 실패하고 있었다 — assistant 메시지의 9.6% 만 임베딩돼 있었다.
_running: set[asyncio.Task] = set()


async def _relay_goal(origin_session_id: str, target_session_id: str, question: str) -> Optional[str]:
    """명시한 목표 또는 양쪽 세션의 유일한 공통 목표에만 릴레이를 묶는다."""
    from app.core.db_pool import get_pool

    rows = await get_pool().fetch(
        "SELECT DISTINCT a.goal_id::text AS goal_id FROM goal_task_links a "
        "JOIN goal_task_links b ON b.goal_id = a.goal_id "
        "WHERE a.task_type = 'chat_session' AND a.task_id = $1 "
        "AND b.task_type = 'chat_session' AND b.task_id = $2 "
        "AND COALESCE(a.link_state, 'active') = 'active' "
        "AND COALESCE(b.link_state, 'active') = 'active'",
        origin_session_id, target_session_id,
    )
    linked = {r["goal_id"] for r in rows}
    explicit = re.search(r"GOAL_ID:\s*([0-9a-f-]{36})", question, re.I)
    if explicit and explicit.group(1) in linked:
        return explicit.group(1)
    return next(iter(linked)) if len(linked) == 1 else None


async def _relay_paused(goal_id: Optional[str], relay_id: str) -> bool:
    if not goal_id:
        return False
    from app.services.orchestration_limits import goal_paused

    paused, why = await goal_paused(goal_id)
    if paused:
        logger.info("session_relay_goal_paused", goal=goal_id, milestone=None,
                    relay=relay_id, why=why)
    return paused


async def _resolve_target(target: str, origin_session_id: str) -> Optional[Dict[str, Any]]:
    """담당 이름 또는 세션 id 로 대상 세션을 찾는다.

    세션 id 를 외우게 하면 아무도 안 쓴다. 역할 키로 부를 수 있어야 한다.
    같은 워크스페이스 안에서만 찾는다 — 프로젝트를 넘어 부르면 맥락이 섞인다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    t = (target or "").strip()
    if not t:
        return None

    if _UUID_RE.match(t):
        row = await pool.fetchrow(
            "SELECT id::text, title, role_key, workspace_id::text FROM chat_sessions WHERE id = $1::uuid",
            t,
        )
        return dict(row) if row else None

    # 1) 역할 키 정확히 일치
    row = await pool.fetchrow(
        """
        SELECT s.id::text, s.title, s.role_key, s.workspace_id::text
        FROM chat_sessions s
        WHERE s.role_key = $1
          AND s.workspace_id = (SELECT workspace_id FROM chat_sessions WHERE id = $2::uuid)
        ORDER BY s.updated_at DESC LIMIT 1
        """,
        t, origin_session_id,
    )
    if row:
        return dict(row)

    # 2) 한글 별칭 — `prompt_assets.role_scope` 에 같이 등록돼 있다.
    #
    #    role_scope = {DataEngineOwner, 데이터엔진담당, DataEngineLead}
    #
    # 프롬프트는 담당을 한글로 부르는데("데이터엔진담당") 세션의 role_key 는
    # 영문이다. 2026-09-14 실측에서 `ask_session(target="데이터엔진담당")` 이
    # 그대로 실패했다 — 주도가 프롬프트에 적힌 이름으로 불렀는데 못 찾는다.
    row = await pool.fetchrow(
        """
        SELECT s.id::text, s.title, s.role_key, s.workspace_id::text
        FROM chat_sessions s
        WHERE s.workspace_id = (SELECT workspace_id FROM chat_sessions WHERE id = $2::uuid)
          AND s.role_key IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM prompt_assets a
              WHERE a.enabled AND a.role_scope @> ARRAY[$1]::text[]
                AND a.role_scope @> ARRAY[s.role_key]::text[]
          )
        ORDER BY s.updated_at DESC LIMIT 1
        """,
        t, origin_session_id,
    )
    if row:
        return dict(row)

    # 3) 세션 제목 — 사람은 "데이터관리자" 처럼 화면에 보이는 이름으로 부른다.
    row = await pool.fetchrow(
        """
        SELECT s.id::text, s.title, s.role_key, s.workspace_id::text
        FROM chat_sessions s
        WHERE s.workspace_id = (SELECT workspace_id FROM chat_sessions WHERE id = $2::uuid)
          AND s.role_key IS NOT NULL
          AND s.title ILIKE '%' || $1 || '%'
        ORDER BY s.updated_at DESC LIMIT 1
        """,
        t, origin_session_id,
    )
    return dict(row) if row else None


async def _target_is_busy(target_session_id: str) -> bool:
    """대상이 이미 응답을 만들고 있으면 끼어들지 않는다.

    진행 중인 작업에 끼어들면 그 작업이 깨진다. 오늘 세션 5090a247 에서
    `stale_superseded_by_newer_user_message` 로 54,301자짜리 진행 중
    응답이 버려지는 것을 봤다.
    """
    from app.core.db_pool import get_pool

    try:
        n = await get_pool().fetchval(
            "SELECT count(*) FROM chat_turn_executions "
            "WHERE session_id = $1::uuid AND status IN ('running','retrying')",
            target_session_id,
        )
        return bool(n)
    except Exception:
        return False


async def _pair_in_flight(origin: str, target: str) -> bool:
    from app.core.db_pool import get_pool

    try:
        n = await get_pool().fetchval(
            "SELECT count(*) FROM session_relay "
            "WHERE status IN ('pending', 'queued') AND created_at > now() - interval '2 hours' "
            "AND ((origin_session_id = $1::uuid AND target_session_id = $2::uuid) "
            "  OR (origin_session_id = $2::uuid AND target_session_id = $1::uuid))",
            origin, target,
        )
        return bool(n)
    except Exception:
        return False


async def _current_hop(origin_session_id: str) -> int:
    """이 세션이 받은 질문의 홉 수. 답하면서 또 물으면 +1 이 된다."""
    from app.core.db_pool import get_pool

    try:
        h = await get_pool().fetchval(
            "SELECT max(hop) FROM session_relay "
            "WHERE target_session_id = $1::uuid AND created_at > now() - interval '6 hours'",
            origin_session_id,
        )
        return int(h or 0)
    except Exception:
        return 0


def _build_question(origin_title: str, origin_role: str, origin_id: str,
                    target_role: str, question: str, context: str) -> str:
    who = origin_role or origin_title or origin_id[:8]
    head = f"[{target_role or '담당'}에게 — {who}({origin_id[:8]})의 질문]"
    body = [head, "", question.strip()]
    ctx = (context or "").strip()
    if ctx:
        body += ["", "── 공유된 내용 ──", ctx[:CONTEXT_LIMIT]]
    body += [
        "", "── 답할 때 ──",
        "이 답은 물어본 대화로 자동 전달됩니다. 결론을 먼저 쓰세요.",
        "자기 담당 범위에서 판단하고, 동의만 하지 말고 위험이 보이면 그렇게 쓰세요.",
    ]
    return "\n".join(body)


async def _deliver_answer(origin_session_id: str, content: str,
                          goal_id: Optional[str] = None, relay_id: str = "") -> bool:
    """회신을 물어본 세션에 넣고 **다음 행동을 하게 한다.**

    2026-09-14 첫 구현은 회신을 assistant 메시지로 넣었다. 루프를 막으려는
    의도였는데, 그러면 **물어본 담당이 그걸 읽고 움직이지 않는다.** 실측:
    회신이 18:08:56 에 도착했고 그 뒤 실행이 0건이었다. 답만 놓여 있었다.

    협업의 목적은 답을 받는 것이 아니라 **받은 답으로 다음을 하는 것**이다.
    그래서 user 역할 + `system_trigger` 로 넣는다 — 파이프라인 러너가 쓰는
    경로이고 응답률 96% 가 확인돼 있다.

    루프는 회신 방식이 아니라 **홉 수**로 막는다. 받은 답으로 또 물으면
    hop 이 오르고 3회에서 막힌다. 회신을 죽여서 막을 일이 아니었다.

    물어본 쪽이 지금 응답 중이면 기다린다. 진행 중인 응답에 새 user 메시지가
    들어가면 그 응답이 버려진다 — 오늘 세션 5090a247 에서
    `stale_superseded_by_newer_user_message` 로 54,301자짜리 진행 중 응답이
    사라지는 것을 봤다.
    """
    from app.services import chat_service as cs

    for attempt in range(_DELIVER_WAIT_TRIES):
        if not await _target_is_busy(origin_session_id):
            break
        logger.info(
            "session_relay_delivery_waiting origin=%s attempt=%d",
            origin_session_id[:8], attempt + 1,
        )
        await asyncio.sleep(_DELIVER_WAIT_SEC)
    else:
        # 끝까지 바쁘면 그래도 넣는다. 답을 영영 안 주는 것보다는 낫다 —
        # 다만 진행 중이던 응답이 대체될 수 있다는 것을 로그로 남긴다.
        logger.warning(
            "session_relay_delivered_while_busy origin=%s", origin_session_id[:8]
        )

    if await _relay_paused(goal_id, relay_id):
        return False

    # 회신을 기다리는 사이 컷오버가 오면 이 프로세스는 스탠바이다. 여기서 턴을
    # 열면 다음 릴리스의 drain 을 막는다 — active 슬롯이 세션이 빌 때 배달한다.
    if await cs.handoff_internal_turn_if_standby(
        origin_session_id, content, source="session_relay_reply",
    ):
        return True

    async for chunk in cs.send_message_stream(
        session_id=origin_session_id,
        content=content,
        intent_override="system_trigger",
        response_mode="quality",
    ):
        del chunk
    return True


async def _run_relay(relay_id: str, target_session_id: str, prompt: str,
                     origin_session_id: str, question: str,
                     goal_id: Optional[str] = None) -> None:
    """대상 세션에 질문을 넣고, 답이 나오면 물어본 세션에 회신한다."""
    from app.core.db_pool import get_pool
    from app.services import chat_service as cs

    pool = get_pool()
    answer = ""
    execution_id = ""
    try:
        if await _relay_paused(goal_id, relay_id):
            await pool.execute(
                "UPDATE session_relay SET status='queued', error='goal_paused' "
                "WHERE id=$1::uuid", relay_id,
            )
            return
        async for chunk in cs.send_message_stream(
            session_id=target_session_id,
            content=prompt,
            intent_override="system_trigger",
            response_mode="quality",
        ):
            # 이 실행의 id 를 잡아 둔다.
            #
            # 2026-09-14 첫 시험에서 "가장 최근 assistant 메시지" 를 답으로
            # 집었더니 **이 질문과 무관한 중단 안내문**이 회신으로 갔다.
            # 무관한 답을 전달하는 것은 답이 없는 것보다 나쁘다 — 물어본
            # 담당이 그걸 근거로 판단한다.
            if not execution_id and isinstance(chunk, str) and '"execution_id"' in chunk:
                try:
                    import json as _json

                    _d = _json.loads(chunk[chunk.index("{"): chunk.rstrip().rindex("}") + 1])
                    execution_id = str(_d.get("execution_id") or "")
                except Exception:
                    pass

        if not execution_id:
            raise RuntimeError("대상 세션의 실행 id 를 잡지 못했다 — 답을 특정할 수 없다")

        row = await pool.fetchrow(
            "SELECT id::text, content, coalesce(intent,'') AS intent FROM chat_messages "
            "WHERE execution_id = $1::uuid AND role = 'assistant' "
            "ORDER BY created_at DESC LIMIT 1",
            execution_id,
        )
        answer = (row["content"] if row else "") or ""
        answer_id = row["id"] if row else None
        intent = (row["intent"] if row else "") or ""

        # 중단·플레이스홀더는 답이 아니다.
        _NOT_ANSWERS = {
            "streaming_placeholder", "stale_empty_placeholder", "_archived_partial",
            "interrupted_partial", "interruption_notice", "resume_failure_notice",
            "rate_limited",
        }
        if intent in _NOT_ANSWERS or not answer.strip():
            raise RuntimeError(f"대상 세션이 답을 완성하지 못했다 (intent={intent or '없음'})")

        # 회신 — 답을 요구하지 않는다. 회신에 또 답하면 그게 루프의 시작이다.
        #
        # `send_message_stream` 으로 넣으면 물어본 세션이 **그 회신에 또
        # 답한다.** 그래서 assistant 메시지로 직접 넣는다 — 화면에는
        # 보이지만 새 응답을 유발하지 않는다.
        target_name = await pool.fetchval(
            "SELECT coalesce(role_key, title) FROM chat_sessions WHERE id = $1::uuid",
            target_session_id,
        ) or "담당"
        reply = (
            f"📨 **{target_name}의 답이 도착했습니다**\n"
            f"> 물어본 질문: {question.strip()[:120]}\n\n"
            f"{answer[:ANSWER_LIMIT]}\n\n"
            "── 이제 할 일 ──\n"
            "이 답을 반영해 다음을 진행하세요. 다른 담당의 의견이 더 필요하면 "
            "`ask_session` 으로 물으세요(한 줄기당 3회까지). 충분하면 결론을 내고 "
            "CEO 에게 보고하세요. 이 메시지에 인사만 하고 끝내지 마세요."
        )
        if not await _deliver_answer(origin_session_id, reply, goal_id, relay_id):
            await pool.execute(
                "UPDATE session_relay SET status='blocked', pending_reply=$2, "
                "answer_message_id=$3::uuid, error='goal_paused' WHERE id=$1::uuid",
                relay_id, reply, answer_id,
            )
            return

        await pool.execute(
            "UPDATE session_relay SET status='answered', answer_message_id=$2::uuid, "
            "answered_at=now() WHERE id=$1::uuid",
            relay_id, answer_id,
        )
        logger.info("session_relay_answered relay=%s target=%s chars=%d",
                    relay_id[:8], target_session_id[:8], len(answer))
    except Exception as exc:
        logger.warning("session_relay_failed relay=%s error=%s", relay_id[:8], str(exc)[:200])
        try:
            await pool.execute(
                "UPDATE session_relay SET status='failed', error=$2 WHERE id=$1::uuid",
                relay_id, str(exc)[:500],
            )
        except Exception:
            pass


async def ask(origin_session_id: str, target: str, question: str,
              context: str = "") -> Dict[str, Any]:
    """다른 담당에게 묻는다. 답은 나중에 이 대화로 돌아온다."""
    from app.core.db_pool import get_pool

    if not origin_session_id:
        # 코드만 돌려주면 담당은 "왜" 를 모른 채 같은 호출을 반복한다.
        # 실제로 2026-09-15 #310 주도 세션이 5번 연속 같은 벽에 부딪혔다.
        # 무엇이 비어 있었는지 로그에 남기고, 화면에도 다음 행동을 적는다.
        import os as _os

        from app.services.tool_executor import current_chat_session_id as _cv

        logger.warning(
            "session_relay_origin_missing: env=%s contextvar=%s pid=%d target=%s",
            "있음" if (_os.getenv("AADS_SESSION_ID") or "").strip() else "없음",
            "있음" if (_cv.get("") or "").strip() else "없음",
            _os.getpid(),
            str(target)[:40],
        )
        return {
            "sent": False, "error": "origin_session_missing",
            "message": "이 도구가 어느 대화에서 불렸는지 서버가 알지 못했습니다. "
                       "같은 호출을 반복하지 말고, `session_id` 에 이 대화의 세션 id 를 "
                       "직접 넣어 한 번 더 시도하세요. 그래도 실패하면 운영에 보고하세요.",
        }
    if not (question or "").strip():
        return {"sent": False, "error": "question_required", "message": "무엇을 물을지 적어야 합니다."}

    pool = get_pool()

    hop = await _current_hop(origin_session_id) + 1
    if hop > MAX_HOP:
        logger.warning("session_relay_hop_exceeded origin=%s hop=%d", origin_session_id[:8], hop)
        return {
            "sent": False, "error": "hop_limit",
            "message": f"이 대화 줄기는 이미 {MAX_HOP}회 오갔습니다. "
                       "더 묻지 말고 지금까지 받은 답으로 결론을 내고 CEO 에게 보고하세요.",
        }

    tgt = await _resolve_target(target, origin_session_id)
    if not tgt:
        return {"sent": False, "error": "target_not_found",
                "message": f"'{target}' 담당을 찾지 못했습니다. 역할 키나 세션 id 를 확인하세요."}
    if tgt["id"] == origin_session_id:
        return {"sent": False, "error": "self_target", "message": "자기 자신에게는 물을 수 없습니다."}

    if await _pair_in_flight(origin_session_id, tgt["id"]):
        return {"sent": False, "error": "already_in_flight",
                "message": f"{tgt.get('role_key') or tgt['title']} 와(과) 이미 주고받는 중입니다. 답을 기다리세요."}
    goal_id = await _relay_goal(origin_session_id, tgt["id"], question)
    # 바쁘면 **반려하지 않고 대기열에 넣는다.**
    #
    # 2026-09-17 대표님 "목표 마일스톤에 접근이 안된다고 세션들에서 보고가
    # 오는데". 실측: session_relay 123건 중 회신 8건(6.5%). 세션 d19a0e9e 는
    # 목표관리자에게 두 번 보내 두 번 다 target_busy 로 반려됐고 "마일스톤
    # 문제는 아직 전달되지 않았습니다" 로 끝났다.
    #
    # 반려가 오탐이어서가 아니다 — 그 순간 running 9건 전부 리스가 살아 있는
    # 진짜 작업 중이었다. 문제는 **오래 일하는 세션에는 영영 못 닿는다**는
    # 것이다. 목표관리자 36분, #119 전략관리자 52분째였고, 그 사이 도착한
    # 질문은 전부 버려졌다.
    #
    # 끼어들지 않는다는 원래 판단은 그대로 지킨다(진행 중 응답이 깨진다).
    # 지금 보내지 않을 뿐, 끝나면 보낸다.
    if await _target_is_busy(tgt["id"]) or await _relay_paused(goal_id, "new"):
        queued_id = str(uuid.uuid4())
        await pool.execute(
            "INSERT INTO session_relay (id, origin_session_id, target_session_id, hop, question, status, goal_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'queued', $6::uuid)",
            queued_id, origin_session_id, tgt["id"], hop, question[:2000], goal_id,
        )
        logger.info("session_relay_queued relay=%s origin=%s target=%s",
                    queued_id[:8], origin_session_id[:8], tgt["id"][:8])
        return {
            "sent": True,
            "queued": True,
            "relay_id": queued_id,
            "target_session": tgt["id"],
            "target_role": tgt.get("role_key") or tgt["title"],
            "hop": hop,
            "message": f"{tgt.get('role_key') or tgt['title']} 가 지금 다른 작업 중이라 "
                       "대기열에 넣었습니다. 그 작업이 끝나면 자동으로 전달되고 답은 이 "
                       "대화에 들어옵니다 — 기다리지 말고 다른 일을 계속하세요.",
        }

    origin = await pool.fetchrow(
        "SELECT title, coalesce(role_key,'') AS role_key FROM chat_sessions WHERE id = $1::uuid",
        origin_session_id,
    )
    prompt = _build_question(
        origin["title"] if origin else "", origin["role_key"] if origin else "",
        origin_session_id, tgt.get("role_key") or "", question, context,
    )

    relay_id = str(uuid.uuid4())
    await pool.execute(
        "INSERT INTO session_relay (id, origin_session_id, target_session_id, hop, question, status, goal_id) "
        "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'pending', $6::uuid)",
        relay_id, origin_session_id, tgt["id"], hop, question[:2000], goal_id,
    )

    task = asyncio.create_task(
        _run_relay(relay_id, tgt["id"], prompt, origin_session_id, question, goal_id)
    )
    _running.add(task)
    task.add_done_callback(_running.discard)

    logger.info("session_relay_sent relay=%s origin=%s target=%s hop=%d",
                relay_id[:8], origin_session_id[:8], tgt["id"][:8], hop)
    return {
        "sent": True,
        "relay_id": relay_id,
        "target_session": tgt["id"],
        "target_role": tgt.get("role_key") or tgt["title"],
        "hop": hop,
        "message": f"{tgt.get('role_key') or tgt['title']} 에게 보냈습니다. "
                   "답은 이 대화에 자동으로 들어옵니다 — 기다리지 말고 다른 일을 계속하세요.",
    }


# 대기열에 넣은 질문을 대상이 한가해지면 배달한다.
#
# 대기열만 만들고 배달하는 주체를 안 두면 반려가 침묵으로 바뀔 뿐이다 —
# 오늘 오전 추가지시 회수에서 같은 실수를 봤다(회수 시도가 그 세션의 다음
# 턴이 돌 때만 일어나, 세션이 멈추면 23시간을 그대로 남았다).
_RELAY_QUEUE_MAX_AGE_HOURS = max(1, int(os.getenv("SESSION_RELAY_QUEUE_MAX_AGE_HOURS", "6")))
_RELAY_QUEUE_BATCH = max(1, int(os.getenv("SESSION_RELAY_QUEUE_BATCH", "3")))
# 목표가 멈춘 동안에도 이보다 묵으면 버린다. 멈춘 채 버려진 목표에 대기
# 질문·붙잡힌 회신이 영원히 쌓이지 않게 하는 상한이다.
_RELAY_PAUSED_MAX_AGE_HOURS = max(
    _RELAY_QUEUE_MAX_AGE_HOURS,
    int(os.getenv("SESSION_RELAY_PAUSED_MAX_AGE_HOURS", "72")),
)


async def _resume_blocked_reply(relay_id: str, origin_session_id: str,
                                reply: str, goal_id: Optional[str]) -> None:
    """목표 멈춤으로 붙잡아 둔 회신을 넣는다. 사이클 밖에서 돈다."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    try:
        delivered = await _deliver_answer(origin_session_id, reply, goal_id, relay_id)
    except Exception as exc:
        # 스트림 도중 실패면 회신이 이미 일부 들어갔을 수 있다. 다시 넣으면
        # 중복이므로 재시도하지 않고 실패로 닫는다(`_run_relay` 와 같은 처리).
        logger.warning("session_relay_reply_resume_failed relay=%s error=%s",
                       relay_id[:8], str(exc)[:200])
        try:
            await pool.execute(
                "UPDATE session_relay SET status='failed', pending_reply=NULL, error=$2 "
                "WHERE id=$1::uuid AND status='pending'",
                relay_id, f"reply_resume_failed: {str(exc)[:450]}",
            )
        except Exception:
            pass
        return
    if not delivered:
        # 집은 뒤 배달 직전에 다시 멈췄다. 다음 해제를 기다린다.
        await pool.execute(
            "UPDATE session_relay SET status='blocked', error='goal_paused' "
            "WHERE id=$1::uuid AND status='pending'",
            relay_id,
        )
        return
    await pool.execute(
        "UPDATE session_relay SET status='answered', answered_at=now(), "
        "pending_reply=NULL, error=NULL WHERE id=$1::uuid",
        relay_id,
    )
    logger.info("session_relay_reply_resumed relay=%s origin=%s",
                relay_id[:8], origin_session_id[:8])


async def dispatch_queued_relays() -> Dict[str, int]:
    """대상이 한가해진 대기 질문을 배달한다.

    한 번에 여러 건을 같은 대상에 보내지 않는다 — 배달하는 순간 그 대상은
    바빠지고, 두 번째 건이 진행 중인 답변을 밀어낸다. 대상당 한 건만 집는다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    sent = 0
    expired = 0

    # 너무 묵은 것은 버린다. 여섯 시간 전 질문을 지금 보내면 맥락이 달라져
    # 엉뚱한 답이 온다 — 답이 없는 것보다 나쁘다.
    #
    # 목표 멈춤(2026-09-29)으로 멈춰 둔 것도 같은 기준이다. 멈춘 **동안만**
    # 만료를 미루고, 풀리는 순간 나이가 넘은 것은 배달하기 전에 여기서
    # 버린다(이 UPDATE 가 배달보다 먼저 돈다). 멈춘 채 버려진 목표에 쌓이지
    # 않도록 `_RELAY_PAUSED_MAX_AGE_HOURS` 를 넘으면 멈춤과 무관하게 버린다.
    # 멈춤으로 붙잡아 둔 회신(`blocked` + `pending_reply`)도 같은 규칙이다.
    exp = await pool.execute(
        "UPDATE session_relay r SET status='failed', pending_reply=NULL, "
        "error = CASE WHEN r.status='blocked' "
        "  THEN 'reply_expired: 목표 멈춤 동안 회신이 오래 묵음' "
        "  ELSE 'queue_expired: 대상이 오래 바빠 배달 못 함' END "
        "WHERE r.status IN ('queued', 'blocked') "
        "AND ((r.created_at <= now() - ($1::int * interval '1 hour') "
        "      AND NOT EXISTS (SELECT 1 FROM goals g WHERE g.id = r.goal_id "
        "                      AND g.paused_at IS NOT NULL)) "
        "     OR r.created_at <= now() - ($2::int * interval '1 hour'))",
        _RELAY_QUEUE_MAX_AGE_HOURS, _RELAY_PAUSED_MAX_AGE_HOURS,
    )
    try:
        expired = int(str(exp).split()[-1])
    except (ValueError, IndexError):
        expired = 0

    # 멈춤이 풀린 목표의 회신을 질문 재실행 없이 재개한다.
    #
    # 멈춘 목표는 SQL 에서 거른다 — 루프에서 거르면 같은 조회를 회신마다
    # 한 번 더 한다. 물어본 세션당 한 건씩만 집어, 멈춤이 풀린 순간 한
    # 세션에 회신이 몰려 들어가지 않게 한다. 배달은 스트림이 끝날 때까지
    # 걸리므로 사이클에서 기다리지 않고 따로 띄운다(`_resume_blocked_reply`).
    replies = await pool.fetch(
        """
        SELECT DISTINCT ON (r.origin_session_id)
               r.id::text AS id, r.origin_session_id::text AS origin,
               r.goal_id::text AS goal_id, r.pending_reply
          FROM session_relay r
         WHERE r.status = 'blocked' AND r.pending_reply IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM goals g WHERE g.id = r.goal_id
                           AND g.paused_at IS NOT NULL)
         ORDER BY r.origin_session_id, r.created_at ASC
         LIMIT $1
        """,
        _RELAY_QUEUE_BATCH,
    )
    for reply in replies:
        try:
            if await _target_is_busy(reply["origin"]):
                continue
            claimed = await pool.execute(
                "UPDATE session_relay SET status='pending' "
                "WHERE id=$1::uuid AND status='blocked'",
                reply["id"],
            )
            if str(claimed).split()[-1] != "1":
                continue
            task = asyncio.create_task(_resume_blocked_reply(
                reply["id"], reply["origin"], reply["pending_reply"], reply["goal_id"],
            ))
            _running.add(task)
            task.add_done_callback(_running.discard)
            sent += 1
        except Exception as exc:
            # 한 건의 실패로 사이클 전체(만료·대기열 배달)가 멈추면 안 된다.
            logger.warning("session_relay_reply_resume_error relay=%s error=%s",
                           reply["id"][:8], str(exc)[:200])

    # 멈춘 목표의 질문은 SQL 에서 거른다. 루프에서 `continue` 로 건너뛰면
    # DISTINCT ON 이 대상당 가장 오래된 한 건만 집으므로, 멈춘 목표의 질문
    # 한 건이 그 대상에게 가는 다른 목표·목표 없는 질문을 전부 막는다.
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (target_session_id)
               id::text AS id, origin_session_id::text AS origin, target_session_id::text AS target,
               question, goal_id::text AS goal_id
          FROM session_relay
         WHERE status = 'queued'
           AND NOT EXISTS (SELECT 1 FROM goals g WHERE g.id = session_relay.goal_id
                           AND g.paused_at IS NOT NULL)
         ORDER BY target_session_id, created_at ASC
         LIMIT $1
        """,
        _RELAY_QUEUE_BATCH,
    )
    for r in rows:
        if await _target_is_busy(r["target"]):
            continue
        # 'queued' 인 동안만 집는다. 두 프로세스가 같이 돌아도 한 번만 나간다.
        claimed = await pool.execute(
            "UPDATE session_relay SET status='pending' WHERE id=$1::uuid AND status='queued'",
            r["id"],
        )
        if str(claimed).split()[-1] != "1":
            continue

        origin = await pool.fetchrow(
            "SELECT title, coalesce(role_key,'') AS role_key FROM chat_sessions WHERE id = $1::uuid",
            r["origin"],
        )
        tgt_role = await pool.fetchval(
            "SELECT coalesce(role_key,'') FROM chat_sessions WHERE id = $1::uuid", r["target"]
        )
        prompt = _build_question(
            origin["title"] if origin else "", origin["role_key"] if origin else "",
            r["origin"], tgt_role or "", r["question"], "",
        )
        task = asyncio.create_task(
            _run_relay(r["id"], r["target"], prompt, r["origin"], r["question"], r["goal_id"])
        )
        _running.add(task)
        task.add_done_callback(_running.discard)
        sent += 1
        logger.info("session_relay_queue_dispatched relay=%s target=%s",
                    r["id"][:8], r["target"][:8])

    if sent or expired:
        logger.info("session_relay_queue_swept sent=%d expired=%d", sent, expired)
    return {"sent": sent, "expired": expired}


# ── 단방향 알림 ────────────────────────────────────────────────────────────
#
# 2026-09-29 CEO 지시: 텔레그램을 알림에서 빼고 담당 세션이 알림을 받아
# 조치하게 한다. 그런데 들어갈 길이 `ask()` 하나뿐이었다 — 왕복(질문→답변
# 완성) 모델이라 답이 완성되지 않으면 failed 로 남는다. 실측 530건 중
# failed 234 / pending 232 / answered 61(11.5%). 알림은 답이 필요 없는데
# 이 길에 태우면 영구히 failed/pending 으로 쌓인다.
#
# 그래서 길을 따로 낸다. `_run_relay`(답 대기·hop 증가)를 타지 않고,
# 넣는 방식만 `_deliver_answer` 를 그대로 쓴다(대상이 응답 중이면 기다림).
#
# 외부 cron 에는 발신 세션이 없다. `origin_session_id` NOT NULL 은 그대로
# 두고 워크스페이스마다 시스템 발신 세션 하나를 확보해 재사용한다.

NOTIFY_SENDER_ROLE_KEY = "SystemNotifier"
NOTIFY_SENDER_TITLE = "시스템 알림 발신"
NOTIFY_SEVERITIES = ("info", "warn", "critical")
NOTIFY_DEDUP_MIN = max(1, int(os.getenv("SESSION_NOTIFY_DEDUP_MIN", "30")))
NOTIFY_BODY_LIMIT = max(200, int(os.getenv("SESSION_NOTIFY_BODY_CHARS", "4000")))

# 지시서는 'delivered' 를 원했지만 DB 에 `session_relay_status_chk`
# (queued|pending|answered|failed|blocked) 가 걸려 있어 그 값은 INSERT 가
# 거부된다. 스키마 변경은 이번 범위에서 금지라 기존 종결 상태 'answered'
# 로 기록하고, 알림 행은 hop=0 · 발신자 SystemNotifier · 질문 머리
# "[알림:" 으로 구분한다. 제약을 넓히면 이 상수만 바꾸면 된다.
NOTIFY_STATUS_DELIVERED = os.getenv("SESSION_NOTIFY_STATUS_DELIVERED", "answered")


def _notify_question(severity: str, title: str, body: str, source: str,
                     dedup_key: Optional[str]) -> str:
    text = f"[알림:{severity}] {title.strip()}\n{(body or '').strip()[:NOTIFY_BODY_LIMIT]}\n(source={source.strip()})"
    if dedup_key:
        # 중복 판정 표식. 본문을 자른 뒤에 붙여서 잘려 나가지 않게 한다.
        text += f"\n(dedup={dedup_key.strip()})"
    return text


def _notify_content(question: str) -> str:
    return (
        f"{question}\n\n"
        "── 이 알림은 단방향입니다 ──\n"
        "발신자는 답을 기다리지 않습니다. 담당 범위에서 확인하고 필요한 조치를 하세요. "
        "조치가 필요 없다고 판단하면 그 이유를 한 줄로 남기세요."
    )


async def _notify_workspace(target: str, workspace_id: Optional[str],
                            tenant_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """알림을 넣을 워크스페이스를 정한다. (workspace_id, 실패 사유) 를 돌려준다.

    `_resolve_target` 은 발신 세션의 워크스페이스 안에서만 찾으므로 발신
    세션을 고르기 전에 워크스페이스가 정해져 있어야 한다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    tenant = (tenant_id or "").strip() or None
    t = (target or "").strip()

    if workspace_id:
        ws = await pool.fetchval(
            "SELECT id::text FROM chat_workspaces WHERE id = $1::uuid "
            "AND ($2::uuid IS NULL OR tenant_id = $2::uuid)",
            workspace_id, tenant,
        )
        return (ws, None) if ws else (None, "workspace_not_found")

    if _UUID_RE.match(t):
        ws = await pool.fetchval(
            "SELECT workspace_id::text FROM chat_sessions WHERE id = $1::uuid "
            "AND ($2::uuid IS NULL OR tenant_id = $2::uuid)",
            t, tenant,
        )
        return (ws, None) if ws else (None, "target_not_found")

    # 역할 키 또는 한글 별칭을 가진 세션이 있는 워크스페이스가 하나뿐이면 그것.
    # 여럿이면 고르지 않는다 — 다른 프로젝트 담당에게 알림이 가는 것이
    # 안 가는 것보다 나쁘다.
    rows = await pool.fetch(
        """
        SELECT DISTINCT s.workspace_id::text AS workspace_id
        FROM chat_sessions s
        WHERE s.role_key IS NOT NULL
          AND s.role_key <> $3
          AND ($2::uuid IS NULL OR s.tenant_id = $2::uuid)
          AND (
              s.role_key = $1
              OR EXISTS (
                  SELECT 1 FROM prompt_assets a
                  WHERE a.enabled AND a.role_scope @> ARRAY[$1]::text[]
                    AND a.role_scope @> ARRAY[s.role_key]::text[]
              )
          )
        """,
        t, tenant, NOTIFY_SENDER_ROLE_KEY,
    )
    ids = [r["workspace_id"] for r in rows]
    if len(ids) == 1:
        return ids[0], None
    if len(ids) > 1:
        return None, "workspace_ambiguous"
    return None, "target_not_found"


async def _system_sender_session(workspace_id: str) -> str:
    """워크스페이스의 시스템 발신 세션을 확보한다. 있으면 재사용한다."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 동시에 두 요청이 와도 한 행만 만든다.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"session_notify_sender:{workspace_id}",
            )
            sid = await conn.fetchval(
                "SELECT id::text FROM chat_sessions WHERE workspace_id = $1::uuid "
                "AND role_key = $2 ORDER BY created_at ASC LIMIT 1",
                workspace_id, NOTIFY_SENDER_ROLE_KEY,
            )
            if sid:
                return sid
            return await conn.fetchval(
                "INSERT INTO chat_sessions (workspace_id, title, role_key) "
                "VALUES ($1::uuid, $2, $3) RETURNING id::text",
                workspace_id, NOTIFY_SENDER_TITLE, NOTIFY_SENDER_ROLE_KEY,
            )


async def _run_notify(relay_id: str, target_session_id: str, content: str) -> None:
    """대상 인박스에 넣는다. 답은 기다리지 않는다 — 회신도, hop 증가도 없다."""
    from app.core.db_pool import get_pool

    try:
        await _deliver_answer(target_session_id, content)
    except Exception as exc:
        logger.warning("session_notify_failed relay=%s error=%s", relay_id[:8], str(exc)[:200])
        try:
            await get_pool().execute(
                "UPDATE session_relay SET status='failed', error=$2 WHERE id=$1::uuid",
                relay_id, str(exc)[:500],
            )
        except Exception:
            pass


async def notify(target: str, title: str, body: str, severity: str, source: str,
                 dedup_key: Optional[str] = None, workspace_id: Optional[str] = None,
                 tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """담당 세션에 단방향 알림을 넣는다.

    대상을 못 찾아도 예외를 던지지 않는다 — 호출자(cron)가 로그로 알 수
    있게 `delivered=False` 와 사유를 돌려준다.
    """
    from app.core.db_pool import get_pool

    result: Dict[str, Any] = {"delivered": False, "target_session_id": None,
                              "relay_id": None, "reason": None}
    if severity not in NOTIFY_SEVERITIES:
        result["reason"] = "invalid_severity"
        return result

    ws, why = await _notify_workspace(target, workspace_id, tenant_id)
    if not ws:
        result["reason"] = why or "target_not_found"
        return result

    sender = await _system_sender_session(ws)
    tgt = await _resolve_target(target, sender)
    if not tgt or tgt.get("workspace_id") != ws:
        result["reason"] = "target_not_found"
        return result
    if tgt["id"] == sender:
        # 발신 세션 자신이 걸리면 알림이 허공에 간다.
        result["reason"] = "target_not_found"
        return result
    result["target_session_id"] = tgt["id"]

    question = _notify_question(severity, title, body, source, dedup_key)
    marker = f"\n(dedup={dedup_key.strip()})" if dedup_key else None

    pool = get_pool()
    relay_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        async with conn.transaction():
            if marker:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"session_notify_dedup:{tgt['id']}:{dedup_key}",
                )
                dup = await conn.fetchval(
                    "SELECT id::text FROM session_relay "
                    "WHERE origin_session_id = $1::uuid AND target_session_id = $2::uuid "
                    "AND hop = 0 AND status <> 'failed' "
                    "AND created_at > now() - ($3::int * interval '1 minute') "
                    "AND right(question, length($4)) = $4 "
                    "ORDER BY created_at DESC LIMIT 1",
                    sender, tgt["id"], NOTIFY_DEDUP_MIN, marker,
                )
                if dup:
                    logger.info("session_notify_deduplicated relay=%s target=%s key=%s",
                                dup[:8], tgt["id"][:8], dedup_key[:60])
                    result["relay_id"] = dup
                    result["reason"] = "deduplicated"
                    return result
            await conn.execute(
                "INSERT INTO session_relay "
                "(id, origin_session_id, target_session_id, hop, question, status, answered_at) "
                "VALUES ($1::uuid, $2::uuid, $3::uuid, 0, $4, $5, now())",
                relay_id, sender, tgt["id"], question, NOTIFY_STATUS_DELIVERED,
            )

    task = asyncio.create_task(_run_notify(relay_id, tgt["id"], _notify_content(question)))
    _running.add(task)
    task.add_done_callback(_running.discard)

    logger.info("session_notify_sent relay=%s target=%s severity=%s source=%s",
                relay_id[:8], tgt["id"][:8], severity, source[:60])
    result.update(delivered=True, relay_id=relay_id)
    return result
