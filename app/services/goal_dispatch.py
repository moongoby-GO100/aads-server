"""마일스톤 착수 지시 — 골 오케스트레이션이 **담당에게 말을 걸게** 한다.

## 왜 필요한가

골 스케줄러는 상태만 옮기고 있었다. 마일스톤이 `in_progress` 로 바뀌어도
그 일을 할 세션은 아무것도 모른다. 대표님이나 주도가 손으로 말을 걸어야
움직였다 — 그건 오케스트레이션이 아니라 수작업이다.

## 끊기거나 실패하면 어떻게 되는가

이게 이 모듈의 핵심이다. 보낸 것과 **답이 온 것**은 다르다.

2026-09-14 실측. 운영인프라담당에게 지시를 넣은 직후 배포로 슬롯이 바뀌며
스트림이 끊겼고 세션에는 이것만 남았다:

    ⚠️ 응답 생성이 중단되어 여기까지 보존된 내용이 없습니다.

보낸 기록만 남겼다면 그 마일스톤은 **영원히 멈춘 채로 조용히 방치된다.**
그래서 네 겹을 둔다.

1. **답이 왔는지 본다.** 보낸 시각 이후에 그 세션에 assistant 메시지가
   실제로 쌓였는지 확인한다. 진행중 표시(`⏳`)나 중단 표시(`⚠️`)는
   답으로 세지 않는다 — 그게 바로 끊긴 경우다.
2. **답이 없으면 다시 보낸다.** `_RETRY_AFTER_MIN` 이 지나야 재시도한다.
   너무 빨리 다시 보내면 아직 생각 중인 담당을 방해한다.
3. **한도가 있다.** `_MAX_DISPATCH` 회를 넘으면 더 보내지 않고
   `dispatch_blocked_at` 을 세운다. `dispatch_note` 는 사람이 읽는 사유일
   뿐 막힘 판정 근거가 아니다. 무한 재시도는 폭주다.
4. **조용히 포기하지 않는다.** 한도를 넘긴 건은 기록에 남아 사람이 볼 수
   있다. 이것이 없으면 "보냈는데 아무 일도 안 일어남" 이 침묵으로 끝난다.

한 사이클에 보내는 건수도 제한한다(`_MAX_PER_CYCLE`). 마일스톤이 한꺼번에
열려도 세션 열 개에 동시에 말을 걸지 않는다.

## 같은 지시를 두 번 보내지 않는 법

위 네 겹은 전부 **혼자 도는 사이클**을 전제로 한다. 조회가 "아직 안 보냈다"
를 읽고, 보내고, 기록한다 — 그 사이에 아무도 없다는 전제다.

2026-09-17 22:28~22:33Z, 그 전제가 깨졌다. GO100 #119 의 마일스톤 둘이
세 사이클 연속 `attempt=1` 로 나갔고 담당 세션은 같은 착수 지시를 2회씩
받았다. 발송 간격 3분, 재시도 간격은 30분이다. 조회 조건은 지켜졌다 —
발송 시점마다 `dispatched_at` 이 실제로 NULL 이었다. 즉 앞 사이클이 남긴
기록이 조회와 발송 사이에 지워졌고, **무조건 UPDATE 는 그것을 못 알아챈다.**

그래서 겹을 둘 더 둔다.

5. **기록이 아니라 소유권을 잡는다.** 발송 직전의 UPDATE 에 "재시도 창
   안에 기록이 없을 때만" 조건을 달고 `RETURNING` 으로 확인한다. 비면 이미
   누가 가져간 것이므로 **보내지 않고** `goal_dispatch_claim_lost` 를 남긴다.
6. **사이클이 겹치지 않게 한다.** 도는 사이클이 있으면 즉시 돌아가고
   `goal_dispatch_cycle_skipped_overlap` 를 남긴다.
7. **기록을 지우는 쪽도 자취를 남긴다.** 5번은 증상을 막을 뿐 누가 지웠는지
   알려주지 않는다. 발송 기록을 되돌리는 모든 경로가 `note_record_reset` 을
   불러 `goal_dispatch_record_reset` 를 남긴다 — 지우기 전 값과 호출 위치를
   같이 적는다.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("GOAL_DISPATCH_ENABLED", "true").lower() in ("1", "true", "yes")
_MAX_PER_CYCLE = int(os.getenv("GOAL_DISPATCH_MAX_PER_CYCLE", "2"))
_MAX_DISPATCH = int(os.getenv("GOAL_DISPATCH_MAX_RETRY", "3"))
_RETRY_AFTER_MIN = int(os.getenv("GOAL_DISPATCH_RETRY_AFTER_MIN", "30"))
# 부하 때문에 미루는 데에도 **상한**을 둔다.
#
# 2026-09-17 실측. contabo14 는 장중에 매매 엔진 둘과 postgres·러너가 같이
# 돌아 부하가 3.6~4.2배로 유지된다. 부하 게이트 기준은 2.0배다. 그래서
# GO100 은 09:09 KST 이후 사이클마다 19건이 통째로 `goal_dispatch_load_gated`
# 로 밀렸다 — 장이 열려 있는 동안, 즉 목표가 가장 움직여야 하는 시간에
# 오케스트레이션이 통째로 멎었다.
#
# 미루기는 기다림이어야지 정지가 아니다. 한 마일스톤이 이만큼 연속으로
# 밀리면 부하와 무관하게 내보낸다. 비용 게이트가 누적 상한에 닿아 영구히
# 잠겼던 것과 같은 종류의 결함이다 — 상한 있는 게이트에는 빠져나갈 문이
# 있어야 한다.
_LOAD_DEFER_MAX_MIN = int(os.getenv("GOAL_DISPATCH_LOAD_DEFER_MAX_MIN", "30"))
# 담당 세션 생성 승인 요청은 한 사이클에 이만큼까지. 카드가 한꺼번에 열 장
# 올라오면 대표님은 읽지 않고 누르신다.
_OWNER_REQUEST_PER_CYCLE = int(os.getenv("GOAL_OWNER_REQUEST_PER_CYCLE", "3"))

# 발송 태스크의 **자체** 시간 상한. 사이클 상한(main.py 의 120초)과 별개다.
#
# 스트림 소비를 사이클에서 떼어내면 사이클 상한이 더는 이 태스크를 끊지
# 않는다. 그렇다고 상한이 없어도 되는 것은 아니다 — 상한 없는 백그라운드는
# 만들지 않는다(R-BG). 2026-09-14 에 `python3 -` 하나가 10시간 12분 동안
# CPU 를 태우고 출력 0바이트였던 것이 상한 없는 백그라운드였다.
_SEND_TIMEOUT = float(os.getenv("AADS_GOAL_DISPATCH_SEND_TIMEOUT_SECONDS", "600"))
# 동시에 몇 건까지 LLM 을 때릴지. 한 사이클 조회 상한이 20건이므로 상한이
# 없으면 20개 스트림이 한꺼번에 열린다.
_SEND_CONCURRENCY = int(os.getenv("AADS_GOAL_DISPATCH_CONCURRENCY", "3"))

# 띄운 태스크의 **강한 참조**. asyncio 는 태스크를 약하게만 들고 있어서
# 여기에 담아 두지 않으면 GC 가 실행 중인 태스크를 거둬간다.
_send_tasks: set[asyncio.Task] = set()
# 세마포어는 루프마다 하나다. import 시점에 만들면 루프가 바뀐 뒤(재기동·
# 테스트) 쓸 수 없는 객체가 남는다.
_send_gate: tuple[Any, asyncio.Semaphore] | None = None
# 사이클 재진입 방지. 세마포어와 같은 이유로 루프마다 하나다.
_cycle_gate: tuple[Any, asyncio.Lock] | None = None


def _send_semaphore() -> asyncio.Semaphore:
    global _send_gate
    loop = asyncio.get_running_loop()
    if _send_gate is None or _send_gate[0] is not loop:
        _send_gate = (loop, asyncio.Semaphore(_SEND_CONCURRENCY))
    return _send_gate[1]


def _cycle_lock() -> asyncio.Lock:
    """사이클 하나가 도는 동안 다음 사이클을 들여보내지 않는다.

    스케줄러(`max_instances=1`)와 advisory lock 이 이미 겹침을 막고 있지만,
    그 둘은 **`_run_goal_control_cycle` 을 지키는 울타리**다. 이 함수는
    거기서만 불리는 것이 아니고(수동 호출·테스트·다른 경로), 울타리가
    하나라도 열리면 같은 행을 두 사이클이 집는다.

    울타리는 지켜야 할 것 바로 옆에 하나 더 두는 편이 싸다.
    """
    global _cycle_gate
    loop = asyncio.get_running_loop()
    if _cycle_gate is None or _cycle_gate[0] is not loop:
        _cycle_gate = (loop, asyncio.Lock())
    return _cycle_gate[1]


# 답으로 세지 않는 표시. 진행중·중단 안내는 담당이 쓴 것이 아니다.
_NOT_AN_ANSWER = ("⏳", "⚠️ _응답 생성이", "_AI가 응답을 생성 중")


def _build_message(row: Any, *, sent_before: int | None = None) -> str:
    """지시문. `sent_before` 는 **소유권을 잡고 읽은** 발송 횟수다.

    조회 시점의 `row["dispatch_count"]` 를 그대로 믿으면, 조회와 발송
    사이에 횟수가 바뀐 경우 "착수" 와 "재알림" 이 뒤바뀐다. 잡은 뒤의
    실측값이 있으면 그것을 쓴다.
    """
    goal = row["goal_title"]
    title = row["milestone_title"]
    desc = (row["description"] or "").strip()
    criteria = (row["completion_criteria"] or "").strip()
    if sent_before is None:
        sent_before = int(row["dispatch_count"] or 0)
    again = sent_before > 0

    head = "[목표 진행 — 재알림]" if again else "[목표 진행 — 착수]"
    body = [
        f"{head} {goal}",
        "",
        f"## 맡은 마일스톤\n**{title}**",
    ]
    if desc:
        body.append(f"\n{desc}")
    if criteria:
        body.append(f"\n## 완료 기준\n{criteria}")
    if again:
        body.append(
            "\n앞서 같은 내용을 보냈는데 답이 확인되지 않았다. 응답이 중간에 "
            "끊겼을 수 있다. 이미 진행한 것이 있으면 그것부터 정리해서 답해라."
        )
    body.append(
        "\n## 답할 때\n"
        "무엇을 **실측했는지**부터 써라. 추정으로 시작하지 마라.\n"
        "자기 범위를 벗어나면 `ask_session` 으로 해당 담당에게 넘겨라.\n"
        "막히면 막힌 이유를 적어라 — 조용히 멈추는 것이 제일 나쁘다."
    )
    return "\n".join(body)


async def _note(conn, milestone_id: str, note: str) -> None:
    """사유를 남긴다 — **값이 달라질 때만.**

    `dispatch_count` 는 건드리지 않는다. 담당이 없어서 못 보낸 것은
    "미룬 것" 이지 "보냈는데 답이 없는 것" 이 아니다. 여기서 횟수를 올리면
    말을 걸어 본 적도 없는 마일스톤이 재시도 한도를 다 쓰고 포기 처리된다.
    """
    await conn.execute(
        "UPDATE milestones SET dispatch_note = $2, updated_at = NOW() "
        " WHERE id = $1::uuid AND dispatch_note IS DISTINCT FROM $2",
        milestone_id, note,
    )


async def _clear_answered_block(conn, milestone_id: str) -> None:
    """답이 확인되면 과거의 차단 신호와 설명 메모를 함께 치운다."""
    await conn.execute(
        "UPDATE milestones SET dispatch_blocked_at = NULL, dispatch_note = NULL, "
        "updated_at = NOW() WHERE id = $1::uuid "
        "AND (dispatch_blocked_at IS NOT NULL OR dispatch_note IS NOT NULL)",
        milestone_id,
    )


def note_record_reset(
    milestone_id: str,
    *,
    reason: str,
    where: str,
    prev_dispatched_at: Any = None,
    prev_dispatch_count: Any = None,
) -> None:
    """발송 기록을 **되돌리는** 쪽이 남기는 자취.

    `dispatched_at` 을 NULL 로 만들거나 `dispatch_count` 를 0 으로 내리는
    것은 "이 마일스톤에 다시 지시를 보내라" 는 뜻이다. 그 자체는 정상
    기능이지만, 기록이 사라졌을 때 **누가 지웠는지 로그에 아무것도 남지
    않는다** 는 것이 2026-09-17 중복 발송에서 원인 특정을 막았다. 조건부
    소유권 UPDATE 가 중복 발송은 이미 막지만(위 5번), 그것은 증상을 막는
    것이지 지우는 주체를 알려주지 않는다.

    그래서 되돌리는 세 경로(`restart_owner`, `rewind`, 반려)가 전부 이
    함수를 부른다. 호출 위치는 인자로 받은 이름(`where`)과 **실제 프레임**
    양쪽을 남긴다 — 새 경로가 생기면서 `where` 를 복사해 붙이고 고치지
    않는 일이 흔하고, 그때 프레임이 정본이 된다.

    지우기 **전** 값을 같이 남기는 것이 핵심이다. "몇 시의 발송 기록이
    지워졌는가" 가 사이클 로그와 맞춰 볼 수 있는 유일한 열쇠다.
    """
    caller = sys._getframe(1)
    prev_at = prev_dispatched_at
    if hasattr(prev_at, "isoformat"):
        prev_at = prev_at.isoformat()
    logger.info(
        "goal_dispatch_record_reset",
        milestone=str(milestone_id)[:8],
        reason=(reason or "")[:160],
        where=where,
        called_from="%s:%d" % (
            os.path.basename(caller.f_code.co_filename), caller.f_lineno,
        ),
        prev_dispatched_at=str(prev_at or ""),
        prev_dispatch_count=(
            int(prev_dispatch_count) if prev_dispatch_count is not None else None
        ),
    )


async def _requester_session(conn, row: Any) -> str:
    """승인 요청을 올릴 세션. **주도가 정본이다.**

    주도가 없으면 같은 워크스페이스의 최근 활성 세션으로 폴백한다. 카드는
    누군가의 이름으로 올라가야 하고, 그 대화에 결정이 돌아간다 —
    올릴 곳이 없으면 대표님은 카드를 보시고도 맥락을 알 수 없다.
    """
    lead = str(row["goal_lead_session_id"] or "").strip()
    if lead:
        return lead
    fallback = await conn.fetchval(
        """
        SELECT s.id::text FROM chat_sessions s
         WHERE s.role_key IS NOT NULL
           AND s.workspace_id = (
                 SELECT s2.workspace_id FROM goal_task_links l
                   JOIN chat_sessions s2 ON s2.id = l.task_id::uuid
                  WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session'
                    AND COALESCE(l.link_state, 'active') = 'active'
                  ORDER BY l.created_at DESC LIMIT 1)
         ORDER BY s.updated_at DESC LIMIT 1
        """,
        row["goal_id"],
    )
    return str(fallback or "")


async def _handle_missing_owner(conn, row: Any, *, remaining: int) -> int:
    """담당 세션이 없는 마일스톤 하나를 처리한다. 올린 요청 수를 돌려준다.

    **여기서 세션을 만들지 않는다.** 만드는 것은 CEO 가 승인 카드를 누른
    뒤 `provision_owner_session` 하나뿐이다 — 채팅창이 대표님 모르게
    늘어나면 안 된다는 원칙(`add_goal_owner`)은 그대로다.
    """
    milestone_id = row["milestone_id"]
    role = str(row["owner_role_key"] or "").strip()

    # 역할조차 안 적힌 마일스톤은 요청 대상이 아니다. 누구를 만들지 모른다.
    if not role:
        await _note(conn, milestone_id, "담당 역할이 지정되지 않음")
        return 0

    requester = await _requester_session(conn, row)
    if not requester:
        await _note(conn, milestone_id, "주도 세션이 없어 승인 요청을 올릴 곳이 없음")
        return 0

    if remaining <= 0:
        await _note(
            conn, milestone_id,
            f"담당 세션 없음(role={role}) — 이번 주기 요청 상한 초과, 다음 주기에 올림",
        )
        return 0

    try:
        from app.services.owner_session_provision import request_owner_session

        result = await request_owner_session(
            goal_id=row["goal_id"], role_key=role,
            project=str(row["project"] or ""), requester_session_id=requester,
        )
    except Exception as exc:  # noqa: BLE001
        # 요청 하나가 실패해도 사이클 전체를 멈추지 않는다. 나머지
        # 마일스톤 발송까지 같이 죽으면 한 건의 오류가 전부를 막는다.
        logger.warning(
            "goal_owner_request_failed",
            milestone=str(milestone_id)[:8], role=role, error=str(exc)[:160],
        )
        await _note(conn, milestone_id, f"담당 세션 없음(role={role}) — 승인 요청 실패")
        return 0

    if result.get("error"):
        logger.warning(
            "goal_owner_request_rejected",
            milestone=str(milestone_id)[:8], role=role, why=str(result["error"])[:160],
        )
        await _note(conn, milestone_id, f"담당 세션 없음(role={role}) — {result['error']}")
        return 0

    await _note(conn, milestone_id, f"담당 세션 없음(role={role}) — 생성 승인 요청함")
    logger.info(
        "goal_owner_request_open",
        milestone=str(milestone_id)[:8], role=role,
        request=str(result.get("request_id") or "")[:8],
        reused=bool(result.get("reused")),
    )
    return 1


async def repair_owner_links(conn) -> int:
    """주도·담당은 정해졌는데 **화면 연결고리가 없는** 목표를 이어 붙인다.

    2026-09-17 실측. 라일론 목표(NTV2, P0)는 `goals.owner_session_id` 와
    마일스톤 6건의 `owner_session_id` 가 전부 채워져 있었는데도 다섯 개 창
    **어디에도 보이지 않았다.** 화면 조회(`GET /goals/for-session/{sid}`)가
    `goal_task_links` 를 FROM 기준 표로 쓰기 때문이다. 목표를 API 가 아니라
    DB 직접 시드로 만들면 그 표가 비고, 목표는 등록됐는데 주도도 담당도
    자기 목표를 못 보는 상태가 된다 — 그날 대표님이 "안 뜨지?" 로 먼저
    알아채셨다.

    소유자가 정해진 것과 화면에 뜨는 것이 서로 다른 표에 적히는 구조라면,
    한쪽만 채워진 상태를 매 주기 메워 주는 편이 맞다. 시드 스크립트마다
    링크 INSERT 를 기억해 넣으라고 규칙으로 적어 두는 것은 또 잊힌다.

    **뗀 것은 다시 붙이지 않는다.** 이미 줄이 있으면(`detached` 포함) 손대지
    않는다 — 대표님이 손으로 떼신 담당을 스케줄러가 되살리면 그건 복원이
    아니라 되돌리기다.
    """
    result = await conn.execute(
        """
        INSERT INTO goal_task_links (goal_id, task_type, task_id, status,
                                     bind_source, bound_by, link_state)
        SELECT o.goal_id, 'chat_session', o.sid, 'active',
               'auto_repair', 'goal_dispatch', 'active'
          FROM (
                SELECT g.id AS goal_id, g.owner_session_id::text AS sid
                  FROM goals g
                 WHERE g.owner_session_id IS NOT NULL
                   AND g.status IN ('draft', 'active', 'blocked')
                UNION
                SELECT m.goal_id, m.owner_session_id::text
                  FROM milestones m
                  JOIN goals g2 ON g2.id = m.goal_id
                 WHERE m.owner_session_id IS NOT NULL
                   AND g2.status IN ('draft', 'active', 'blocked')
               ) o
          JOIN chat_sessions s ON s.id = o.sid::uuid
         WHERE NOT EXISTS (
                SELECT 1 FROM goal_task_links l
                 WHERE l.goal_id = o.goal_id
                   AND l.task_type = 'chat_session'
                   AND l.task_id = o.sid
               )
        """
    )
    try:
        inserted = int(str(result).split()[-1])
    except (ValueError, IndexError):
        inserted = 0
    if inserted:
        logger.info("goal_owner_links_repaired", inserted=inserted)
    return inserted


async def _load_defer_minutes(conn: Any, row: Any) -> float:
    """부하로 **연속해서** 밀린 시간(분).

    처음 밀리는 건이면 시작 시각을 DB 에 남긴다. 메모리에 두면 배포·재기동
    때마다 0 으로 돌아가고, 하루에 몇 번씩 배포하는 서버에서는 상한이
    영원히 오지 않는다 — 그러면 상한을 둔 의미가 없다.
    """
    since = row["load_deferred_since"]
    if since is None:
        await conn.execute(
            "UPDATE milestones SET load_deferred_since = NOW() "
            "WHERE id = $1::uuid AND load_deferred_since IS NULL",
            row["milestone_id"],
        )
        return 0.0
    waited = await conn.fetchval(
        "SELECT EXTRACT(EPOCH FROM (NOW() - $1::timestamptz)) / 60", since,
    )
    return float(waited or 0.0)


async def _clear_load_defer(conn: Any, milestone_id: str) -> None:
    """부하가 풀렸다. 다음에 또 밀리면 그때부터 다시 센다."""
    await conn.execute(
        "UPDATE milestones SET load_deferred_since = NULL "
        "WHERE id = $1::uuid AND load_deferred_since IS NOT NULL",
        milestone_id,
    )


async def _note_detached(milestone_id: str, note: str) -> None:
    """태스크에서 사유를 남긴다 — 사이클의 커넥션은 이미 풀로 돌아갔다.

    여기서 터져도 발송 태스크를 죽이지 않는다. 사유를 못 남긴 것이
    발송 실패 로그까지 같이 삼킬 이유는 없다.
    """
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            await _note(conn, milestone_id, note)
    except Exception as exc:  # noqa: BLE001 - 기록 실패가 태스크를 죽이면 안 된다
        logger.warning(
            "goal_dispatch_note_failed",
            milestone=milestone_id[:8], error=str(exc)[:160],
        )


async def _send_milestone(
    *,
    milestone_id: str,
    session_id: str,
    message: str,
    attempt: int,
    project: str | None,
    count_after: int | None = None,
) -> None:
    """지시를 넣고 스트림이 끝날 때까지 기다린다 — **사이클 밖에서.**

    사이클은 이 함수를 태스크로 띄우기만 하고 다음 마일스톤으로 넘어간다.
    그래서 담당 한 명의 긴 응답이 뒤 마일스톤을 자르지 못한다.
    """

    async def _consume() -> None:
        # 세마포어를 상한 **안에서** 잡는다. 밖에 두면 순서를 기다리는
        # 동안에는 상한이 안 걸려 결국 상한 없는 대기가 된다.
        async with _send_semaphore():
            from app.services import chat_service as cs

            async for _chunk in cs.send_message_stream(
                session_id=session_id,
                content=message,
                intent_override="system_trigger",
                response_mode="quality",
            ):
                pass

    try:
        await asyncio.wait_for(_consume(), _SEND_TIMEOUT)
    except TimeoutError:
        # 발송 기록은 사이클에서 이미 남았다. 담당이 답을 못 하면
        # `_RETRY_AFTER_MIN` 뒤 재알림이 이어받는다.
        logger.warning(
            "goal_dispatch_send_timeout",
            milestone=milestone_id[:8], session=session_id[:8],
            attempt=attempt, limit=_SEND_TIMEOUT,
        )
        return
    except asyncio.CancelledError:
        # 이제 사이클 상한은 이 경로에 닿지 않는다. 남는 취소 경로는
        # 서버 종료다. `CancelledError` 는 `Exception` 의 하위가 아니므로
        # 여기서 잡지 않으면 아무 흔적 없이 사라진다 — 2026-09-17 M4 가
        # 그랬다. 잡아서 남기고 다시 던진다.
        logger.warning(
            "goal_dispatch_cancelled",
            milestone=milestone_id[:8], session=session_id[:8],
            attempt=attempt,
            note="스트림이 취소됐다(종료 등) — 답이 없으면 재알림된다",
        )
        raise
    except Exception as exc:
        # 횟수는 사이클에서 이미 올렸다. 여기서 또 올리면 한 번의 시도가
        # 재시도 한도를 두 칸 깎는다. 사유만 남긴다.
        logger.warning(
            "goal_dispatch_send_failed",
            milestone=milestone_id[:8], error=str(exc)[:160],
        )
        await _note_detached(milestone_id, f"발송 실패: {str(exc)[:200]}")
        return

    # `count_after` 는 소유권을 잡은 UPDATE 가 **DB 에서 돌려준** 횟수다.
    #
    # 2026-09-17 22:28~22:33Z 실측. 로그에는 `attempt` 밖에 없었고 네 건이
    # 전부 `attempt=1` 이었다. 그런데 DB 의 `dispatch_count` 는 1 이었다 —
    # 발송은 두 번, 카운트는 한 번. 로그만으로는 "조회가 0 을 읽었다" 와
    # "기록이 지워졌다" 를 구분할 수 없었고, 원인 추적이 DB 대조로 넘어갔다.
    # 실측값을 같이 남기면 그 대조가 로그 안에서 끝난다.
    logger.info(
        "goal_dispatch_sent",
        milestone=milestone_id[:8], session=session_id[:8],
        attempt=attempt, count_after=count_after, project=project,
    )


def _spawn_send(**kwargs: Any) -> asyncio.Task:
    """태스크를 띄우고 **참조를 붙든다.**"""
    task = asyncio.create_task(_send_milestone(**kwargs))
    _send_tasks.add(task)
    task.add_done_callback(_send_tasks.discard)
    return task


async def dispatch_pending_milestones(project: str | None = None) -> dict[str, int]:
    """착수했는데 담당이 모르는 마일스톤에 지시를 넣는다."""
    if not _ENABLED:
        return {"sent": 0, "skipped": 0, "gave_up": 0, "owner_requests": 0}

    # 사이클이 겹치면 같은 행을 두 번 집는다.
    #
    # 조건부 UPDATE 가 발송 자체는 막지만, 겹친 사이클은 그 전에 이미
    # 조회·게이트·링크 복원을 통째로 한 벌 더 돌린다. 그 비용도 쓸데없고,
    # 무엇보다 "한 번에 하나" 라는 가정 위에 서 있는 것들(`touched`,
    # `_MAX_PER_CYCLE`, `_OWNER_REQUEST_PER_CYCLE`)이 전부 두 배가 된다.
    #
    # 기다리지 않고 **즉시 돌아간다.** 이미 도는 사이클이 같은 일을 하고
    # 있으므로 줄을 서 봐야 한 박자 늦게 같은 일을 또 하는 것뿐이다.
    lock = _cycle_lock()
    if lock.locked():
        logger.info("goal_dispatch_cycle_skipped_overlap", project=project or "ALL")
        return {
            "sent": 0, "skipped": 0, "gave_up": 0,
            "owner_requests": 0, "links_repaired": 0, "overlap_skipped": 1,
        }

    async with lock:
        from app.core.db_pool import get_pool

        pool = get_pool()
        sent = skipped = gave_up = owner_requests = 0
        # 한 세션에 한 주기 한 번만. 세션이 목표 두 개에 참여하면 양쪽에서
        # 동시에 지시가 나갈 수 있는데, 담당은 그걸 두 개의 새 대화로 받는다.
        # 어느 쪽부터 할지 모른 채 섞어서 답한다.
        touched: set[str] = set()

        async with pool.acquire() as conn:
            # 지시를 보내기 전에 연결고리부터 메운다. 링크가 없으면 담당은
            # 지시를 받아도 자기 화면에서 그 목표를 찾을 수 없다.
            links_repaired = await repair_owner_links(conn)

            # 부하로 밀린 시각을 적어 둘 칸. 없으면 만든다(있으면 아무 일도
            # 안 한다). 마이그레이션 파일이 안 돈 서버에서도 게이트가 상한
            # 없이 도는 일이 없도록 여기서 보장한다.
            await conn.execute(
                "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS "
                "load_deferred_since timestamptz"
            )
            await conn.execute(
                "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS "
                "dispatch_blocked_at timestamptz"
            )

            rows = await conn.fetch(
                """
                SELECT m.id::text AS milestone_id, m.title AS milestone_title,
                       m.description, m.completion_criteria,
                       m.dispatch_count, m.dispatched_at, m.load_deferred_since,
                       m.dispatch_blocked_at,
                       g.title AS goal_title, g.project, g.id::text AS goal_id,
                       COALESCE(m.owner_role_key, '') AS owner_role_key,
                       COALESCE(g.owner_session_id::text, '') AS goal_lead_session_id,
                       COALESCE(m.owner_session_id, s.id) AS session_id
                FROM milestones m
                JOIN goals g ON g.id = m.goal_id
                LEFT JOIN chat_sessions s
                       ON m.owner_session_id IS NULL
                      AND m.owner_role_key IS NOT NULL
                      AND s.role_key = m.owner_role_key
                WHERE m.status = 'in_progress'
                  AND g.status = 'active'
                  AND ($1::text IS NULL OR g.project = $1)
                  AND (m.dispatched_at IS NULL
                       OR m.dispatched_at < NOW() - ($2 || ' minutes')::interval)
                ORDER BY m.dispatched_at NULLS FIRST, m.sequence_order
                LIMIT 20
                """,
                project, str(_RETRY_AFTER_MIN),
            )

            for row in rows:
                if not row["session_id"]:
                    # 담당이 안 정해진 마일스톤. 말을 걸 곳이 **아직** 없다.
                    #
                    # 2026-09-17 이전에는 여기서 그냥 넘어갔다. 기록이 어디에도
                    # 남지 않아서, 담당 없는 마일스톤은 착수 상태로 열린 채
                    # 영원히 방치됐다 — 아무도 그런 것이 있는 줄 몰랐다.
                    # 이제 사유를 남기고, 주도 세션이 생성 승인을 요청한다.
                    skipped += 1
                    owner_requests += await _handle_missing_owner(
                        conn, row, remaining=_OWNER_REQUEST_PER_CYCLE - owner_requests,
                    )
                    continue

                # 발송 상한은 담당 없는 건을 처리한 **뒤에** 본다. 먼저 보면
                # 발송이 상한에 차는 사이클마다 승인 요청이 통째로 밀린다.
                #
                # `break` 가 아니라 `continue` 인 이유도 같다. 상한에 찼다고
                # 루프를 끊으면 뒤에 남은 담당 없는 마일스톤이 이번 사이클에
                # 아예 보이지 않는다 — 발송이 바쁜 목표일수록 담당 공백이
                # 영원히 안 드러난다. 한 사이클 20건이라 도는 비용은 없다.
                if sent >= _MAX_PER_CYCLE:
                    continue

                # 상한 셋을 본다. **미루는 것과 포기하는 것은 다르다** —
                # 아래 셋은 전부 미루기이므로 `dispatch_count` 를 올리지 않는다.
                # 올리면 부하나 비용 때문에 미룬 것이 재시도 한도를 헛되이 깎는다.
                from app.services.orchestration_limits import (
                    cost_gate, load_gate, owner_paused,
                )

                if str(row["session_id"]) in touched:
                    skipped += 1
                    continue

                paused, why = await owner_paused(row["goal_id"], str(row["session_id"]))
                if paused:
                    skipped += 1
                    continue

                ok, why = await cost_gate(row["goal_id"])
                if not ok:
                    logger.info(
                        "goal_dispatch_cost_gated",
                        milestone=row["milestone_id"][:8], why=why,
                    )
                    skipped += 1
                    continue

                ok, why = await load_gate(row["project"])
                if not ok:
                    waited = await _load_defer_minutes(conn, row)
                    if waited < _LOAD_DEFER_MAX_MIN:
                        logger.info(
                            "goal_dispatch_load_gated",
                            milestone=row["milestone_id"][:8], why=why,
                            waited_min=round(waited, 1),
                        )
                        skipped += 1
                        continue
                    # 상한을 넘겼다. 부하는 여전하지만 **더 미루지 않는다** —
                    # 계속 미루면 장중 내내 한 건도 안 나간다.
                    logger.warning(
                        "goal_dispatch_load_defer_expired",
                        milestone=row["milestone_id"][:8], why=why,
                        waited_min=round(waited, 1),
                        limit_min=_LOAD_DEFER_MAX_MIN,
                    )
                elif row["load_deferred_since"] is not None:
                    await _clear_load_defer(conn, row["milestone_id"])

                # 보낸 뒤에 **진짜 답**이 왔는지 본다.
                if row["dispatched_at"] is not None:
                    answered = await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM chat_messages
                            WHERE session_id = $1 AND role = 'assistant'
                              AND created_at > $2
                              AND length(content) > 40
                              AND content NOT LIKE '⏳%'
                              AND content NOT LIKE '⚠️ _응답 생성이%'
                        )
                        """,
                        row["session_id"], row["dispatched_at"],
                    )
                    if answered:
                        await _clear_answered_block(conn, row["milestone_id"])
                        skipped += 1
                        continue

                count = int(row["dispatch_count"] or 0)
                if count >= _MAX_DISPATCH:
                    if row["dispatched_at"] is not None:
                        await conn.execute(
                            "UPDATE milestones SET dispatch_blocked_at = "
                            "COALESCE(dispatch_blocked_at, NOW()), dispatch_note = $2, "
                            "updated_at = NOW() WHERE id = $1::uuid AND "
                            "(dispatch_blocked_at IS NULL OR dispatch_note IS DISTINCT FROM $2)",
                            row["milestone_id"],
                            f"{_MAX_DISPATCH}회 보냈으나 답이 확인되지 않음 — 사람이 확인해야 한다",
                        )
                    gave_up += 1
                    continue

                # **보내기 전에 기록한다.** 순서가 뒤집히면 기록이 사라진다.
                #
                # 2026-09-17 실측. 여기는 스트림을 끝까지 소비한 **뒤에**
                # 기록하고 있었다. 그런데 이 코루틴은 상위 사이클에서
                # `asyncio.wait_for(..., 120초)` 로 감싸여 돈다(main.py). 상한이
                # 먼저 터지면 코루틴이 취소되고 뒤에 있던 UPDATE 는 실행되지
                # 않는다. 지시는 담당 세션에 이미 들어갔는데 `dispatched_at` 은
                # NULL 로 남는다.
                #
                # 라일론 목표 M4 에서 그대로 일어났다. 10:17:12 에 발송된 지시가
                # 기록되지 않아 다음 주기가 10:20:09 에 **같은 지시를 다시**
                # 보냈고, 첫 턴은 `interrupted_partial` 로 죽었다. 담당은 같은
                # 일을 두 번 받고 LLM 비용은 두 번 든다.
                #
                # 그래서 순서를 바꾼다. 못 보냈는데 보냈다고 적히는 쪽이
                # 나은가 — 그렇다. 그건 다음 주기가 "재알림" 으로 복구한다.
                # 반대는 복구되지 않고 중복 발송을 계속 만든다.
                #
                # **그리고 기록이 아니라 소유권으로 잡는다.**
                #
                # 2026-09-17 22:28~22:33Z 실측. GO100 #119 의 마일스톤
                # 05fdc2bb·d195f3d2 가 세 사이클 연속 `attempt=1` 로 나갔고,
                # 담당 세션 둘에 같은 "[목표 진행 — 착수]" 가 2회씩 실제로
                # 들어갔다(chat_messages 07:28:44·07:31:42, 07:29:46·07:32:30 KST).
                # 발송 간격은 3분 — 재시도 간격은 30분이다.
                #
                # 조회 조건은 지켜졌다. 그 사이클의 `skipped=8` 은 담당이 없어
                # 건너뛴 8건(sequence_order 1~4)과 정확히 맞고, 정렬이
                # `dispatched_at NULLS FIRST, sequence_order` 이므로 seq 5·6 이
                # 아홉째·열째로 잡히려면 **그 시점에 `dispatched_at` 이 NULL**
                # 이어야 한다. 즉 사이클이 30분 창을 어긴 것이 아니라, 앞
                # 사이클이 남긴 발송 기록이 조회와 발송 사이에 지워졌다.
                #
                # 무조건 UPDATE 는 그것을 알아챌 방법이 없다 — 누가 지웠든
                # 덮어쓰고 보낸다. 조건부 UPDATE 는 알아챈다. 재시도 창 안에
                # 이미 기록이 있으면 `RETURNING` 이 비고, 그러면 **보내지
                # 않는다.** 경합에서 둘 다 보내는 대신 한 쪽만 보낸다.
                # `$3` 은 위 조회 SQL 의 `$2` 와 **같은 `_RETRY_AFTER_MIN`** 이다.
                # 두 조건식이 어긋나면 소유권 획득이 영원히 실패하거나(조회보다
                # 좁을 때) 영원히 통과한다(넓을 때) — 어느 쪽이든 조용히
                # 망가진다. 이 대응은 테스트가 정적으로 고정한다
                # (`tests/unit/test_goal_dispatch_record_reset.py`).
                claimed = await conn.fetchrow(
                    "UPDATE milestones SET dispatched_at = NOW(), "
                    "dispatched_session_id = $2, dispatch_count = dispatch_count + 1, "
                    "dispatch_note = NULL, load_deferred_since = NULL, "
                    "dispatch_blocked_at = NULL, "
                    "updated_at = NOW() WHERE id = $1::uuid "
                    "  AND status = 'in_progress' "
                    "  AND (dispatched_at IS NULL "
                    "       OR dispatched_at < NOW() - ($3 || ' minutes')::interval) "
                    "RETURNING dispatch_count",
                    row["milestone_id"], row["session_id"], str(_RETRY_AFTER_MIN),
                )
                if claimed is None:
                    # 다른 사이클이 이미 가져갔거나, 그 사이 마일스톤이
                    # 진행중에서 빠졌다. 어느 쪽이든 여기서 보내면 중복이다.
                    #
                    # **WARNING 이다.** 소유권을 놓쳤다는 것은 이 사이클과
                    # 겹쳐 도는 다른 주체가 실제로 있었다는 뜻이고, 그것이
                    # 바로 2026-09-17 중복 발송의 조건이다. INFO 로 남기면
                    # 초당 수백 줄 사이에 묻혀 "봉쇄가 몇 번 걸렸는가" 를
                    # 사후에 셀 수 없다.
                    #
                    # 이번 사이클이 **조회에서 읽은** 값을 같이 남긴다.
                    # 기록이 지워지는 경로를 좁히려면 "조회가 본 값" 과
                    # "발송 직전 DB 값" 의 차이가 필요하다 — 이번 사건에서
                    # 그 대조를 하려고 DB 를 직접 뒤져야 했다.
                    logger.warning(
                        "goal_dispatch_claim_lost",
                        milestone=str(row["milestone_id"])[:8],
                        session=str(row["session_id"])[:8],
                        read_count=count,
                        read_dispatched_at=str(row["dispatched_at"] or ""),
                        why="이미 재시도 창 안에 발송 기록이 있거나 진행중이 아니다",
                    )
                    skipped += 1
                    continue

                count_after = int(claimed["dispatch_count"] or 0)

                # 스트림 소비는 **사이클에서 떼어낸다.**
                #
                # 2026-09-17 실측. 이 루프 전체가 상위에서
                # `asyncio.wait_for(..., 120초)` 로 감싸여 돈다(main.py). 첫
                # 담당의 응답이 120초를 넘기면 루프가 통째로 취소되고, 뒤에
                # 남은 마일스톤은 그 사이클에 한 건도 못 나갔다. 끊긴 담당의
                # 턴은 `interrupted_partial` 로 죽었다.
                #
                # 이제 사이클은 태스크만 띄우고 다음 건으로 넘어간다. 태스크는
                # 자체 상한(`_SEND_TIMEOUT`)과 동시 실행 상한
                # (`_SEND_CONCURRENCY`) 아래에서 혼자 끝난다.
                _spawn_send(
                    milestone_id=row["milestone_id"],
                    session_id=str(row["session_id"]),
                    message=_build_message(row, sent_before=count_after - 1),
                    attempt=count_after,
                    project=row["project"],
                    count_after=count_after,
                )

                sent += 1
                touched.add(str(row["session_id"]))

        if sent or gave_up or owner_requests or links_repaired:
            logger.info(
                "goal_dispatch_cycle", sent=sent, skipped=skipped, gave_up=gave_up,
                owner_requests=owner_requests, links_repaired=links_repaired,
            )
        # `sent` 는 이제 "스트림을 끝까지 소비한 수" 가 아니라 "발송 태스크를
        # 띄운 수" 다 — 실제 완료는 `goal_dispatch_sent` 로그가 알린다.
        return {
            "sent": sent, "skipped": skipped, "gave_up": gave_up,
            "owner_requests": owner_requests, "links_repaired": links_repaired,
        }
