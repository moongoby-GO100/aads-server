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


async def _deliver_answer(origin_session_id: str, content: str) -> None:
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

    async for chunk in cs.send_message_stream(
        session_id=origin_session_id,
        content=content,
        intent_override="system_trigger",
        response_mode="quality",
    ):
        del chunk


async def _run_relay(relay_id: str, target_session_id: str, prompt: str,
                     origin_session_id: str, question: str) -> None:
    """대상 세션에 질문을 넣고, 답이 나오면 물어본 세션에 회신한다."""
    from app.core.db_pool import get_pool
    from app.services import chat_service as cs

    pool = get_pool()
    answer = ""
    execution_id = ""
    try:
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
        await _deliver_answer(origin_session_id, reply)

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
    if await _target_is_busy(tgt["id"]):
        queued_id = str(uuid.uuid4())
        await pool.execute(
            "INSERT INTO session_relay (id, origin_session_id, target_session_id, hop, question, status) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'queued')",
            queued_id, origin_session_id, tgt["id"], hop, question[:2000],
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
        "INSERT INTO session_relay (id, origin_session_id, target_session_id, hop, question, status) "
        "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'pending')",
        relay_id, origin_session_id, tgt["id"], hop, question[:2000],
    )

    task = asyncio.create_task(
        _run_relay(relay_id, tgt["id"], prompt, origin_session_id, question)
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
    exp = await pool.execute(
        "UPDATE session_relay SET status='failed', "
        "error='queue_expired: 대상이 오래 바빠 배달 못 함' "
        "WHERE status='queued' AND created_at <= now() - ($1::int * interval '1 hour')",
        _RELAY_QUEUE_MAX_AGE_HOURS,
    )
    try:
        expired = int(str(exp).split()[-1])
    except (ValueError, IndexError):
        expired = 0

    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (target_session_id)
               id::text AS id, origin_session_id::text AS origin, target_session_id::text AS target,
               question
          FROM session_relay
         WHERE status = 'queued'
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
            _run_relay(r["id"], r["target"], prompt, r["origin"], r["question"])
        )
        _running.add(task)
        task.add_done_callback(_running.discard)
        sent += 1
        logger.info("session_relay_queue_dispatched relay=%s target=%s",
                    r["id"][:8], r["target"][:8])

    if sent or expired:
        logger.info("session_relay_queue_swept sent=%d expired=%d", sent, expired)
    return {"sent": sent, "expired": expired}
