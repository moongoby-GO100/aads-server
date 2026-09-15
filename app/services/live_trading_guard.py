"""실매매 조건 변경을 CEO 승인 전까지 막는다.

2026-09-14. CEO 지시: "실매매조건은 나의 승인후 진행해야지".

**프롬프트로 막지 않는다.** 역할 지침에 "승인 후" 라고 적어 두긴 했지만
그것만으로는 안 막힌다. 같은 날 하루에만 이런 일이 있었다.

    · "릴리스 중 병행 빌드 금지" 를 AGENTS.md 에 써넣은 세션이
      같은 날 그 규칙을 어겨 배포 #414 를 죽였다.
    · `pgrep -f` 자기 매칭을 오류 사전에 등록해 두고도 **다섯 번** 밟았다.
    · 자동 재시도가 스스로를 먹어 resume 요청이 시간당 1,256회까지 갔다.

AGENTS.md 에 이렇게 적혀 있다 — "에이전트는 자기가 쓴 규칙도 어긴다.
두 번 이상 어긴 규칙은 코드 차단으로 옮긴다."

실매매는 돈이 걸린다. 첫 번째부터 코드로 막는다.

## 무엇을 막나

도구가 **실매매 동작을 바꾸는 경로**를 건드릴 때만 막는다. 조사·분석·
백테스트·읽기는 그대로 돈다 — 담당들이 일을 못 하면 하네스가 무의미하다.

    막음:  live_trading/ · order_executor · card_service · risk
           전략 파라미터 · 주문 로직 · 유니버스 설정
    통과:  읽기 · 조회 · 백테스트 · 문서 · 분석 스크립트

## 막으면 어떻게 되나

도구가 실행되지 않고, `agent_permission_requests` 에 승인 요청이 남고,
담당에게는 "승인 대기 중" 이 돌아간다. CEO 가 승인하면 그때 실행한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

# 기능 스위치. 배포 없이 끌 수 있어야 한다 — 게이트가 정상 작업을 막는
# 상황이 오면 즉시 풀 수 있어야 하고, 그 반대(몰래 꺼져 있음)도 로그로
# 드러나야 한다.
_ENABLED = os.getenv("LIVE_TRADING_GATE_ENABLED", "true").lower() == "true"

# 실매매 동작을 바꾸는 경로. 변경 원장 실측(2026-09-14)에서 뽑았다.
#   live_trading/scalping_entry_engine.py   변경 22건
#   live_trading/live_engine.py             변경 21건
#   routers/go100/card_trades_router.py     변경 20건
#   execution/order_executor.py             변경 13건
#   strategy/card_service.py                변경 11건
# 구분자가 밑줄일 때도 하이픈일 때도 잡아야 한다. 파일은
# `kiwoom_scalping_runner.py` 인데 서비스는 `go100-kiwoom-scalping` 이다.
# 2026-09-14 첫 시험에서 `systemctl restart go100-kiwoom-scalping` 이
# 그대로 통과했다 — 파일만 보고 서비스 이름을 안 봤다.
_GUARDED_PATH = re.compile(
    r"(live[_-]trading|order[_-]executor|card[_-]trades|"
    r"card[_-]service|scalping|kiwoom|position[_-]manager|"
    r"account[_-]sync|live[_-]promotion|paper[_-]trading|"
    # `risk` 는 심볼로만 본다. `[/_-]risk|risk[_-]` 로 두면 `risk_report.md`
    # 같은 문서까지 잡는다.
    r"risk[_-](limit|manager|engine|config|guard))",
    re.IGNORECASE,
)

# 파일을 바꾸거나 원격에서 무언가를 실행하는 도구만 본다.
# 읽기 도구는 아무리 민감한 경로를 봐도 막지 않는다.
_WRITE_TOOLS = {
    "write_remote_file", "patch_remote_file", "db_safe_write",
    "deploy_safe", "pipeline_runner_submit", "delegate_to_agent",
}
_COMMAND_TOOLS = {"run_remote_command", "pc_execute", "execute_sandbox", "device_command"}

# 명령 안에서 쓰기로 보는 것. 조회는 통과시킨다.
_MUTATING_CMD = re.compile(
    r"(^|[;&|]\s*)(rm|mv|cp|tee|sed\s+-i|systemctl\s+(restart|stop|start)|"
    r"docker\s+(restart|stop|rm)|git\s+(push|checkout|reset)|"
    r"psql[^|]*\b(update|insert|delete|alter|drop)\b|"
    r"python[0-9.]*\s+[^|]*\b(deploy|apply|promote|activate)\b)",
    re.IGNORECASE,
)
_REDIRECT_WRITE = re.compile(r">\s*[^|&\s]|>>\s*[^|&\s]")

# `2>/dev/null` 은 쓰기가 아니다.
#
# 거의 모든 조회 명령에 붙는데, 리다이렉트 판정이 `>` 뒤에 글자만 있으면
# 쓰기로 쳤다. 그래서 `grep … live_engine.py 2>/dev/null` 이 "실매매 경로에서
# 변경 명령을 실행하려 함" 으로 막혔다(2026-09-15 실측). 버리는 리다이렉트를
# 먼저 지우고 나서 본다.
_NULL_REDIRECT = re.compile(r"\d?>>?\s*(/dev/null|&\d)")

# 문서·보고서·시험은 실매매를 바꾸지 않는다.
#
# 경로 정규식에 `risk`·`scalping` 이 들어 있어 `docs/risk_report.md` 와
# `reports/scalping_backtest_result.md` 가 막혔다. 파일명에 단어가 있다는 것과
# 그 파일이 주문을 바꾼다는 것은 다른 얘기다.
_NON_CODE_PATH = re.compile(
    r"(^|/)(docs?|reports?|tests?|plans?)/|\.(md|txt|html|csv|png|jpg|json|log)$",
    re.IGNORECASE,
)

# 돈이 직접 걸린 것. 여기 걸리면 무조건 막는다.
_CRITICAL = re.compile(
    r"(order[_-]executor|place[_-]order|cancel[_-]order|send[_-]order|"
    r"submit[_-]order|is[_-]live|allocated[_-]amount|account[_-]no|"
    r"dedicated[_-]account|systemctl\s+(restart|stop|start)\s+\S*go100)",
    re.IGNORECASE,
)


def _has_write_redirect(cmd: str) -> bool:
    """버리는 리다이렉트를 빼고 나서 쓰기 리다이렉트를 찾는다."""
    return bool(_REDIRECT_WRITE.search(_NULL_REDIRECT.sub("", cmd)))


# 명령문 안의 문서·시험 경로는 지우고 나서 본다.
#
# `bash scripts/run_unit_tests.sh tests/unit/test_live_trading_guard.py` 가
# 막혔다 — 실매매 코드를 **시험하는 파일 이름**에 `live_trading` 이 들어
# 있다는 이유였다. 시험을 돌리는 것과 실매매를 바꾸는 것은 다른 일이다.
_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:docs?|reports?|tests?|plans?)/\S*", re.IGNORECASE)


def _code_relevant_text(blob: str) -> str:
    return _PATH_TOKEN.sub(" ", blob)


def _work_key(session_id: str, tool_name: str, summary: str) -> str:
    """요청을 가리키는 키. **프로세스가 달라도 같아야 한다.**

    예전에는 파이썬 내장 `hash()` 를 썼다. 문자열 해시는 프로세스마다
    무작위로 바뀐다(PYTHONHASHSEED). API 워커가 여러 개라, 요청을 남긴
    워커와 승인을 확인하는 워커가 다르면 **같은 명령인데 키가 달라졌다.**

    결과는 이랬다 — 승인 카드는 뜨는데 눌러도 실행되지 않고, 다음 시도에서
    또 같은 카드가 새로 뜬다. 중복 요청이 쌓이던 것도 같은 원인이다.

    sha1 은 어느 프로세스에서 돌려도 같은 값을 준다. 비밀을 다루는 자리가
    아니므로 속도만 보면 된다.
    """
    digest = hashlib.sha1(summary.encode("utf-8", "replace")).hexdigest()[:7]
    return "%s:%s:%s" % (session_id[:8], tool_name, digest)


def _target_key(tool_input: Dict[str, Any]) -> str:
    """미션 승인이 덮는 **대상**. 명령 본문이 아니라 무엇을 건드리는가다.

    2026-09-15 실측 — `app/services/live_trading_guard.py` 한 건에 대한
    "이 미션 동안" 승인이 **같은 세션의 모든 `patch_remote_file` 을**
    통과시켰다. 미션 범위가 (세션 + 도구) 로만 정의돼 대상을 보지
    않았기 때문이다. AADS 파일 하나를 승인했더니 GO100 `live_engine.py`
    수정까지 같은 승인으로 열렸다 — 게이트를 켜 둔 이유와 정반대다.

    그렇다고 명령 본문 해시(`_work_key`) 로 좁히면 한 글자만 달라도 다시
    묻는다. 그래서 그 중간을 잡는다.

        쓰기 도구   프로젝트 + 파일 경로
        명령 도구   명령문에서 잡힌 실매매 경로 토큰(정렬·중복 제거)
                    → 같은 파일을 여러 명령으로 고쳐도 승인 하나로 이어진다
        둘 다 비면  요약 전문 (= 사실상 이번 건만)

    빈 문자열은 돌려주지 않는다. 빈 값끼리 맞으면 아무 대상이나 통과한다.
    """
    project = str(tool_input.get("project") or "").strip().upper()
    path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
    if path:
        target = "%s|%s" % (project, path.lstrip("./"))
    else:
        blob = _code_relevant_text(_text_of(tool_input))
        # 경로 토큰을 통째로 쓴다. `live_trading` 같은 정규식 조각만 쓰면
        # 같은 디렉터리의 다른 파일이 한 덩어리로 묶인다.
        tokens = sorted({
            t.strip("'\"`,;()") for t in re.split(r"\s+", blob)
            if t and _GUARDED_PATH.search(t)
        })
        if not tokens:
            tokens = sorted({m.group(0).lower() for m in _GUARDED_PATH.finditer(blob)})
        target = (
            "%s|%s" % (project, ",".join(tokens))
            if tokens else "raw|%s" % ((_text_of(tool_input) or "")[:400])
        )
    return hashlib.sha1(target.encode("utf-8", "replace")).hexdigest()[:10]


def _file_fingerprint(tool_input: Dict[str, Any]) -> str:
    """대상 파일의 **현재 내용** 지문. 대상 지문(``_target_key``)은 경로만 본다.

    2026-09-15 확인 — mission/session/project 범위는 파일 *경로*가 같으면
    통과한다. 승인 유효기간(미션 최대 2시간·20회, session/project 는 더
    넓다) 동안 다른 세션·배포가 같은 파일을 먼저 바꿔도 그대로 통과했다 —
    CEO 가 본 diff 와 실제 적용될 코드가 다를 수 있었다(commit_hash 미검증).

    이 컨테이너에서 직접 읽을 수 있는 파일(project=AADS, 상대경로 존재)만
    지문을 남긴다. 다른 프로젝트 파일은 이 컨테이너에서 보이지 않으므로
    빈 문자열(=지문 없음, 기존 동작 유지)로 두어 거짓 안전감을 주지 않는다.
    """
    project = str(tool_input.get("project") or "").strip().upper()
    path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
    if project != "AADS" or not path:
        return ""
    try:
        with open(os.path.join("/app", path.lstrip("./")), "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:10]
    except OSError:
        return ""


def _text_of(tool_input: Dict[str, Any]) -> str:
    parts = []
    for key in ("file_path", "path", "command", "query", "sql", "target", "task", "project"):
        v = tool_input.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


TIER_APPROVE = "approve"   # 막고 대표님 승인을 기다린다
TIER_NOTIFY = "notify"     # 막지 않고 알린다
TIER_PASS = ""             # 아무것도 안 한다


def classify_tier(tool_name: str, tool_input: Dict[str, Any]) -> Tuple[str, str, str]:
    """(등급, 위험도, 사유).

    기준 한 줄 — **되돌릴 수 없고 돈이 걸린 것만 막는다.**

    되돌릴 수 있는 것(코드 수정, 문서, 목표 정리)은 막지 않고 알린다.
    2026-09-15 실측에서 대기 8건 중 진짜 승인 대상은 2건이었다. 오탐이
    여섯이면 다음번엔 내용을 안 보고 누르게 되고, 그때 진짜 하나가 같이
    통과한다. 오탐은 규칙을 죽인다.
    """
    if not _ENABLED:
        return TIER_PASS, "", ""

    blob = _text_of(tool_input)
    if not blob:
        return TIER_PASS, "", ""

    # 문서·보고서·시험은 실매매를 바꾸지 않는다. 경로 판정보다 먼저 뺀다.
    if _NON_CODE_PATH.search(str(tool_input.get("file_path") or tool_input.get("path") or "")):
        return TIER_PASS, "", ""

    scanned = _code_relevant_text(blob)
    is_critical = bool(_CRITICAL.search(scanned))
    on_guarded_path = bool(_GUARDED_PATH.search(scanned))
    if not is_critical and not on_guarded_path:
        return TIER_PASS, "", ""

    if tool_name in _COMMAND_TOOLS:
        cmd = str(tool_input.get("command") or "")
        if not (_MUTATING_CMD.search(cmd) or _has_write_redirect(cmd)):
            # 조회 명령(cat, grep, ls, tail, psql SELECT…)은 통과.
            return TIER_PASS, "", ""
        if is_critical:
            return TIER_APPROVE, "critical", "실매매 서비스·주문 경로를 바꾸는 명령"
        return TIER_APPROVE, "high", "실매매 경로에서 변경 명령을 실행하려 함"

    if tool_name in _WRITE_TOOLS:
        if is_critical:
            return TIER_APPROVE, "critical", f"{tool_name} 이(가) 주문·자금 설정을 바꾸려 함"
        return TIER_APPROVE, "high", f"{tool_name} 이(가) 실매매 경로를 변경하려 함"

    return TIER_PASS, "", ""


def classify(tool_name: str, tool_input: Dict[str, Any]) -> Tuple[bool, str]:
    """(막을 것인가, 사유). 옛 호출부 호환."""
    tier, _risk, reason = classify_tier(tool_name, tool_input)
    return tier == TIER_APPROVE, reason


async def request_approval(
    tool_name: str,
    tool_input: Dict[str, Any],
    reason: str,
    *,
    session_id: str = "",
    tenant_id: str = "",
    gate_source: str = "live_trading",
    tier: str = TIER_APPROVE,
    risk_level: str = "high",
    label: str = "실매매",
) -> Optional[str]:
    """승인 요청을 남긴다. 요청 id 를 돌려준다.

    같은 세션이 같은 도구로 같은 대상을 반복 시도하면 요청이 쌓인다.
    그래서 대기 중인 동일 요청이 있으면 그것을 재사용한다.
    """
    from app.core.db_pool import get_pool

    summary = (_text_of(tool_input) or "")[:400]
    work_key = _work_key(session_id, tool_name, summary)
    try:
        pool = get_pool()
        # `decision` 은 NOT NULL 이고 기본값이 'pending' 이다. NULL 로 찾으면
        # 영원히 안 맞는다 — 2026-09-14 첫 구현에서 그래서 대기 목록이
        # 항상 비어 있었다.
        existing = await pool.fetchval(
            "SELECT id::text FROM agent_permission_requests "
            "WHERE work_key = $1 AND decision = ANY($2::text[]) AND expires_at > now() "
            "ORDER BY created_at DESC LIMIT 1",
            work_key,
            ["pending"] if tier == TIER_APPROVE else ["notified", "acknowledged"],
        )
        if existing:
            return existing

        # tenant_id 는 NOT NULL 이다. 세션에서 가져온다.
        tid = tenant_id or ""
        if not tid and session_id:
            try:
                tid = str(await pool.fetchval(
                    "SELECT tenant_id::text FROM chat_sessions WHERE id = $1::uuid",
                    session_id,
                ) or "")
            except Exception:
                tid = ""
        if not tid:
            logger.warning("live_trading_gate_no_tenant session=%s", session_id[:8])
            return None

        # 기본 만료가 10분이다. CEO 가 10분 안에 못 보면 요청이 사라진다 —
        # 그러면 담당은 막히기만 하고 승인받을 방법이 없다. 24시간으로 둔다.
        return await pool.fetchval(
            """
            INSERT INTO agent_permission_requests
                (tenant_id, work_key, origin, action_type, action_summary,
                 risk_level, decision, requested_by, approval_scope,
                 max_executions, expires_at, created_at, gate_source, tier)
            VALUES ($1::uuid, $2, 'chat_session', $3, $4, $7, $8, $5,
                    $6::jsonb, 1, now() + interval '24 hours', now(), $9, $10)
            RETURNING id::text
            """,
            tid, work_key, tool_name,
            f"[{label}] {reason}\n{summary}", session_id or "unknown",
            # 대상 지문을 요청 시점에 박아 둔다. 승인 시점에 다시 계산하면
            # 그때는 tool_input 이 없어 무엇을 허락하는지 알 수 없다.
            json.dumps({
                "scope": "single_call",
                "target": _target_key(tool_input),
                # 프로젝트 범위 승인이 나중에 이 값을 본다. 승인 시점에는
                # tool_input 이 없어 다시 계산할 수 없다 — 대상 지문과 같은 이유다.
                "project": str(tool_input.get("project") or "").strip().upper(),
                # 요청 시점 파일 내용 지문. mission/session/project 범위가
                # 소비 시점에 이 값과 비교해, 그 사이 파일이 바뀌면 재승인을
                # 요구한다 (_file_fingerprint 참고).
                "file_fp": _file_fingerprint(tool_input),
            }),
            risk_level,
            # 알림 등급은 승인 개념이 없다. 대기 목록에 섞이면 진짜 승인
            # 대상이 그 사이에 묻힌다.
            "pending" if tier == TIER_APPROVE else "notified",
            gate_source, tier,
        )
    except Exception as exc:
        logger.warning("live_trading_gate_request_failed", error=str(exc))
        return None


async def notify_only(
    tool_name: str,
    tool_input: Dict[str, Any],
    reason: str,
    *,
    session_id: str = "",
    tenant_id: str = "",
    gate_source: str = "live_trading",
    risk_level: str = "medium",
    label: str = "실매매 주변",
) -> Optional[str]:
    """막지 않고 기록·알림만 남긴다.

    알림 실패가 도구를 막으면 본말전도다 — 조용히 통과시킨다.
    """
    request_id = await request_approval(
        tool_name, tool_input, reason,
        session_id=session_id, tenant_id=tenant_id,
        gate_source=gate_source, tier=TIER_NOTIFY,
        risk_level=risk_level, label=label,
    )
    try:
        from app.services import ohvis_alert

        await ohvis_alert.notify(
            "[%s] %s" % (label, reason[:60]),
            (_text_of(tool_input) or "")[:400],
            severity=ohvis_alert.INFO,
            category="approval_notify",
            project="AADS",
            dedupe_minutes=10,
        )
    except Exception as exc:
        logger.warning("notify_only_alert_failed", error=str(exc)[:160])
    return request_id


async def _active_goal_ids(session_id: str) -> list:
    """이 세션이 지금 참여 중인 목표들. 끝난 목표는 빠진다."""
    if not session_id:
        return []
    from app.core.db_pool import get_pool

    try:
        rows = await get_pool().fetch(
            """
            SELECT g.id::text AS id
            FROM goal_task_links l
            JOIN goals g ON g.id = l.goal_id
            WHERE l.task_type = 'chat_session' AND l.task_id = $1
              AND COALESCE(l.link_state, 'active') = 'active'
              AND g.status IN ('draft', 'active', 'blocked')
            """,
            session_id,
        )
        return [r["id"] for r in rows]
    except Exception as exc:
        logger.warning("active_goal_lookup_failed session=%s error=%s",
                       session_id[:8], str(exc)[:120])
        return []


async def goal_policy_allows(
    session_id: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    risk_level: str,
) -> Optional[str]:
    """목표에 걸어 둔 승인 설정이 이 일을 허락하는가. 허락하면 목표 id.

    2026-09-15 대표님 지시 — "골에 승인게이트 승인관련 설정할수 있게 반영해",
    "내가 계속 쳐다 봐야하잖아".

    목표 하나를 끝내는 동안 같은 종류의 변경이 수십 번 나온다. 그때마다
    물으면 대표님이 화면을 떠날 수 없다. 그래서 **목표 단위로 미리** 허락을
    받아 둔다.

    세 가지를 지킨다.

    1. **주문·자금(critical)은 기본 꺼짐이다.** 코드 수정을 미리 허락하는 것과
       주문을 미리 허락하는 것은 다른 얘기다. 켜려면 따로 켜야 한다.
    2. **횟수 상한이 있다.** 무제한이면 설정이 아니라 게이트 해제다.
    3. **쓸 때마다 남긴다.** 자동 통과도 기록이 남아야 나중에 "언제부터
       무엇이 그냥 나갔나" 를 볼 수 있다.

    목표가 끝나거나 담당이 떨어지면 설정도 같이 끝난다 — 따로 회수하지
    않아도 된다.
    """
    if not session_id:
        return None
    from app.core.db_pool import get_pool

    field = "auto_approve_critical" if risk_level == "critical" else "auto_approve_high"
    # 목표의 프로젝트 밖은 이 설정이 덮지 않는다.
    #
    # 2026-09-15 실측 — 자동 승인이 켜진 목표 3건은 전부 GO100 이고 각각
    # 채팅 세션 8~10 개가 묶여 있었다. 그런데 조회가 프로젝트를 보지 않아,
    # 그 세션이 AADS `live_trading_guard.py` 를 고쳐도 같은 설정으로
    # 통과했다. 파일명이 `_GUARDED_PATH` 에 걸리기만 하면 됐다.
    #
    # 대표님이 "이 목표 동안" 을 켜신 것은 그 목표의 일을 막지 말라는
    # 뜻이지, 다른 프로젝트까지 열라는 뜻이 아니다.
    #
    # 대상 프로젝트를 알 수 없는 호출(예: db_safe_write)은 통과시키지
    # 않는다. 무엇을 여는지 모르는 채로 여는 것이 가장 나쁘다.
    target_project = str(tool_input.get("project") or "").strip().upper()
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT g.id::text AS id, g.title,
                   COALESCE((g.approval_policy->>$2)::boolean, false) AS allowed,
                   COALESCE((g.approval_policy->>'max_executions')::int, 0) AS max_exec,
                   COALESCE((g.approval_policy->>'used')::int, 0) AS used
            FROM goal_task_links l
            JOIN goals g ON g.id = l.goal_id
            WHERE l.task_type = 'chat_session' AND l.task_id = $1
              AND COALESCE(l.link_state, 'active') = 'active'
              AND g.status IN ('draft', 'active', 'blocked')
              AND COALESCE((g.approval_policy->>$2)::boolean, false)
              AND $3 <> ''
              AND UPPER(COALESCE(g.project, '')) = $3
            ORDER BY g.updated_at DESC
            LIMIT 1
            """,
            session_id, field, target_project,
        )
    except Exception as exc:
        logger.warning("goal_policy_lookup_failed session=%s error=%s",
                       session_id[:8], str(exc)[:140])
        return None

    if not row or not row["allowed"]:
        return None
    if int(row["used"]) >= int(row["max_exec"] or 0):
        logger.info("goal_policy_exhausted goal=%s used=%s max=%s",
                    row["id"][:8], row["used"], row["max_exec"])
        return None

    try:
        await pool.execute(
            "UPDATE goals SET approval_policy = jsonb_set(approval_policy, '{used}', "
            "       to_jsonb(COALESCE((approval_policy->>'used')::int, 0) + 1), true), "
            "       updated_at = NOW() WHERE id = $1::uuid",
            row["id"],
        )
    except Exception as exc:
        logger.warning("goal_policy_count_failed goal=%s error=%s",
                       row["id"][:8], str(exc)[:140])

    # 자동으로 나간 것도 화면에 남는다. 안 보이면 없는 것과 같다.
    await notify_only(
        tool_name, tool_input,
        "목표 승인 설정으로 통과 (%s, %d/%d회)"
        % (row["title"][:40], int(row["used"]) + 1, int(row["max_exec"] or 0)),
        session_id=session_id,
        tenant_id=str(tool_input.get("tenant_id") or ""),
        gate_source="goal_policy", risk_level=risk_level or "high",
        label="목표 승인",
    )
    return str(row["id"])


async def is_approved(tool_name: str, tool_input: Dict[str, Any], session_id: str = "",
                      risk_level: str = "") -> bool:
    """이미 승인된 **범위** 안인지.

    예전에는 `work_key` 하나로만 찾았다. 그 키에 명령 본문 해시가 들어 있어
    **명령이 한 글자만 달라도 새 승인을 요구**했다. "이 미션 동안" 을 눌러도
    다음 수정에서 또 물었다는 뜻이다 — 대표님이 화면을 계속 쳐다봐야 했던
    이유다(2026-09-15 지적).

    이제 범위를 실제로 본다.

        single   이 요청 하나 (work_key 정확히 일치)
        mission  같은 세션 + 같은 도구 + **같은 대상**
        session  같은 세션 + 같은 도구 (대상은 묻지 않는다)
        project  같은 프로젝트 + 같은 도구 (**세션을 넘는다**)
        goal     그 목표가 살아 있는 동안 + 같은 도구

    2026-09-15 대표님 지시로 `session`·`project` 가 붙었다. 미션 범위가
    대상 지문까지 맞아야 해서, 파일 하나를 끝내고 다음 파일로 넘어갈 때마다
    새 카드가 떴다 — 12시간에 카드 89장, 실사용 18회(실측). 한 대화에서
    여러 파일을 고치는 것이 보통인데 범위가 그것을 담지 못했다.

    **주문·자금(critical)은 이 두 범위로 통과하지 않는다.** 넓은 범위는
    "같은 종류의 일을 계속 한다" 는 뜻이지 "돈이 나가는 일을 계속 해도
    된다" 는 뜻이 아니다. 승인 화면에서도 그 버튼을 그리지 않지만, 여기서
    한 번 더 막는다 — 화면은 바뀌어도 이 조회는 남는다.

    **무제한은 없다.** 어느 범위든 횟수 상한을 넘기면 다시 묻는다. 목표가
    끝나면 `_active_goal_ids` 에서 빠지므로 골 승인도 자동으로 닫힌다.

    미션 범위에 대상을 넣은 이유는 `_target_key` 주석에 적었다 — 요약하면,
    파일 하나를 승인했더니 세션의 모든 쓰기가 열렸다(2026-09-15 실측).
    대상 지문이 없는 옛 승인은 여기서 **맞지 않는다.** 다시 묻는 쪽이
    잘못 통과시키는 쪽보다 낫다.
    """
    from app.core.db_pool import get_pool

    summary = (_text_of(tool_input) or "")[:400]
    work_key = _work_key(session_id, tool_name, summary)
    target_key = _target_key(tool_input)
    goal_ids = await _active_goal_ids(session_id)
    # 넓은 범위를 쓸 수 있는가. critical 이면 아래 두 절이 통째로 꺼진다.
    wide_ok = (risk_level or "") != "critical"
    target_project = str(tool_input.get("project") or "").strip().upper()
    # 지금 이 파일이 승인 당시와 같은 내용인가. 다르면 mission/session/
    # project 범위 아래에서도 이 요청은 맞지 않는다 (_file_fingerprint 참고).
    file_fp = _file_fingerprint(tool_input)
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT id::text AS id, max_executions,
                   COALESCE((approval_scope->>'used')::int, 0) AS used,
                   COALESCE(approval_scope->>'scope', 'single') AS scope
            FROM agent_permission_requests
            WHERE decision = 'approved' AND expires_at > now()
              AND (
                    work_key = $1
                 OR (approval_scope->>'scope' = 'mission'
                     AND requested_by = $2 AND action_type = $3
                     AND approval_scope->>'target' = $5
                     AND COALESCE(approval_scope->>'file_fp', '') IN ('', $8))
                 OR (approval_scope->>'scope' = 'goal'
                     AND action_type = $3
                     AND approval_scope->>'goal_id' = ANY($4::text[]))
                 -- 이 대화 동안: 같은 세션의 같은 도구면 대상을 묻지 않는다.
                 OR ($6 AND approval_scope->>'scope' = 'session'
                     AND requested_by = $2 AND action_type = $3
                     AND COALESCE(approval_scope->>'file_fp', '') IN ('', $8))
                 -- 이 프로젝트 동안: 세션을 넘는다. 그래서 프로젝트를 모르는
                 -- 호출($7 = '')은 여기에 걸리지 않는다 — 무엇을 여는지
                 -- 모르는 채로 여는 것이 가장 나쁘다.
                 OR ($6 AND $7 <> '' AND approval_scope->>'scope' = 'project'
                     AND action_type = $3
                     AND UPPER(COALESCE(approval_scope->>'project', '')) = $7
                     AND COALESCE(approval_scope->>'file_fp', '') IN ('', $8))
              )
            -- 넓은 것부터 쓴다. 좁은 승인을 남겨 두어야 그 대상에 다시
            -- 물어보지 않는다.
            ORDER BY CASE COALESCE(approval_scope->>'scope', 'single')
                         WHEN 'goal' THEN 0 WHEN 'project' THEN 1
                         WHEN 'session' THEN 2 WHEN 'mission' THEN 3 ELSE 4 END,
                     decided_at DESC
            LIMIT 1
            """,
            work_key, session_id, tool_name, goal_ids or [""], target_key,
            wide_ok, target_project, file_fp,
        )
        if not row:
            return False
        if int(row["used"]) >= int(row["max_executions"] or 1):
            logger.info(
                "approval_exhausted id=%s scope=%s used=%s max=%s",
                row["id"][:8], row["scope"], row["used"], row["max_executions"],
            )
            return False
        # 쓴 횟수를 센다. 세지 않으면 상한이 장식이 된다.
        await pool.execute(
            "UPDATE agent_permission_requests "
            "   SET approval_scope = jsonb_set(approval_scope, '{used}', "
            "       to_jsonb(COALESCE((approval_scope->>'used')::int, 0) + 1), true), "
            "       updated_at = now() "
            " WHERE id = $1::uuid",
            row["id"],
        )
        return True
    except Exception as exc:
        logger.warning("is_approved_failed error=%s", str(exc)[:160])
        return False


async def check(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """도구 실행 전 검사. 막을 것이면 담당에게 보여줄 JSON 을 돌려준다.

    통과면 None. 호출자는 None 일 때만 도구를 실행한다.
    """
    tier, risk_level, reason = classify_tier(tool_name, tool_input)
    if tier == TIER_PASS:
        return None

    session_id = str(tool_input.get("session_id") or "")
    tenant_id = str(tool_input.get("tenant_id") or "")

    if tier == TIER_NOTIFY:
        # 막지 않는다. 되돌릴 수 있는 일까지 막으면 담당이 멈추고, 멈춘
        # 담당은 우회로를 찾는다. 그때부터 게이트는 없는 것과 같다.
        await notify_only(
            tool_name, tool_input, reason,
            session_id=session_id, tenant_id=tenant_id, risk_level=risk_level,
        )
        return None

    if await is_approved(tool_name, tool_input, session_id, risk_level):
        logger.info("live_trading_gate_approved_pass tool=%s session=%s",
                    tool_name, session_id[:8])
        return None

    # 목표에 걸어 둔 승인 설정. 대표님이 그 목표 동안 미리 허락해 두신 등급이면
    # 여기서 통과한다 — 매번 묻지 않기 위해 존재한다.
    goal_pass = await goal_policy_allows(session_id, tool_name, tool_input, risk_level)
    if goal_pass:
        logger.info(
            "live_trading_gate_goal_policy_pass tool=%s session=%s goal=%s risk=%s",
            tool_name, session_id[:8], goal_pass[:8], risk_level,
        )
        return None

    request_id = await request_approval(
        tool_name, tool_input, reason, session_id=session_id, tenant_id=tenant_id,
        risk_level=risk_level or "high",
    )
    logger.warning(
        "live_trading_gate_blocked tool=%s session=%s request=%s reason=%s",
        tool_name, session_id[:8], (request_id or "-")[:8], reason,
    )
    return json.dumps(
        {
            "error": "live_trading_approval_required",
            "blocked": True,
            "reason": reason,
            "request_id": request_id,
            "message": (
                "실매매 조건 변경은 CEO 승인이 필요합니다. 실행하지 않았습니다.\n"
                "무엇을 왜 바꾸려는지, 예상 효과와 되돌리는 방법까지 정리해 "
                "CEO 에게 보고하고 승인을 기다리세요.\n"
                "우회 시도(다른 도구·다른 경로로 같은 변경)는 하지 마세요."
            ),
        },
        ensure_ascii=False,
    )
