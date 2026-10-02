"""세션 간 협업 회귀 테스트.

2026-09-14. 담당 다섯이 서로 물어볼 방법이 없어 만들었다. 실제 왕복
시험에서 버그 셋을 잡았고 전부 여기 고정한다.

**1. 답을 실행 id 로 특정한다.**
   첫 구현은 대상 세션의 "가장 최근 assistant 메시지" 를 답으로 집었다.
   그랬더니 이 질문과 **무관한 중단 안내문**이 회신으로 갔다. 무관한 답을
   전달하는 것은 답이 없는 것보다 나쁘다 — 물어본 담당이 그걸 근거로
   판단한다.

**2. 백그라운드 태스크 참조를 붙든다.**
   `asyncio.create_task` 결과를 버리면 GC 가 중간에 거둬 간다. 같은 날
   `chat_messages.embedding` 이 그 방식으로 소리 없이 실패하고 있었다
   (assistant 메시지의 9.6% 만 임베딩).

**3. 루프를 세 겹으로 막는다.**
   A→B→A→B 가 끝없이 돈다. 같은 날 resume 자동 재시도가 시간당 1,256회까지
   간 것이 같은 종류의 사고다.
"""
import inspect

from app.services import session_relay


def test_answer_is_tied_to_execution():
    """가장 최근 메시지를 답으로 집으면 안 된다."""
    src = inspect.getsource(session_relay._run_relay)
    assert "execution_id = $1::uuid" in src, "실행 id 로 답을 특정하지 않는다"
    assert "ORDER BY created_at DESC LIMIT 1\",\n            target_session_id" not in src, (
        "세션의 최신 메시지를 그대로 답으로 쓰고 있다"
    )
    assert "실행 id 를 잡지 못했다" in src, "실행 id 없이 진행하고 있다"


def test_interrupted_is_not_an_answer():
    """중단·플레이스홀더를 답으로 전달하면 안 된다."""
    src = inspect.getsource(session_relay._run_relay)
    for bad in ("streaming_placeholder", "interrupted_partial",
                "resume_failure_notice", "stale_empty_placeholder"):
        assert bad in src, f"{bad} 를 답에서 걸러내지 않는다"


def test_background_task_reference_is_held():
    """태스크를 버리면 GC 가 중간에 거둬 간다."""
    src = inspect.getsource(session_relay.ask)
    assert "_running.add(task)" in src, "태스크 참조를 붙들지 않는다"
    assert "add_done_callback" in src


def test_three_loop_guards_exist():
    src = inspect.getsource(session_relay.ask)
    assert "hop_limit" in src, "홉 제한이 없다"
    assert "self_target" in src, "자기 자신 차단이 없다"
    assert "already_in_flight" in src, "같은 쌍 중복 차단이 없다"
    assert "target_busy" in src, "진행 중인 세션 보호가 없다"


def test_reply_triggers_next_action():
    """회신이 다음 행동을 유발해야 한다.

    2026-09-14 첫 구현은 회신을 assistant 메시지로 넣었다. 루프를 막으려는
    의도였는데 **물어본 담당이 그걸 읽고 움직이지 않았다.** 실측: 회신이
    18:08:56 에 도착했고 그 뒤 실행이 0건이었다. 답만 놓여 있었다.

    협업의 목적은 답을 받는 것이 아니라 받은 답으로 다음을 하는 것이다.
    루프는 회신을 죽여서가 아니라 **홉 수**로 막는다.
    """
    src = inspect.getsource(session_relay._deliver_answer)
    body = src[src.index("from app.services import chat_service"):]
    assert "send_message_stream" in body, "회신이 다음 행동을 유발하지 않는다"
    assert "system_trigger" in body, "응답률 96% 가 확인된 경로를 쓰지 않는다"


def test_delivery_waits_for_busy_origin():
    """진행 중인 응답에 회신을 밀어 넣으면 그 응답이 버려진다.

    오늘 세션 5090a247 에서 `stale_superseded_by_newer_user_message` 로
    54,301자짜리 진행 중 응답이 사라지는 것을 봤다.
    """
    src = inspect.getsource(session_relay._deliver_answer)
    assert "_target_is_busy" in src, "물어본 쪽이 바쁜지 확인하지 않는다"
    assert "_DELIVER_WAIT_TRIES" in src


def test_reply_tells_what_to_do_next():
    """답만 던지면 무엇을 하라는 건지 모른다."""
    src = inspect.getsource(session_relay._run_relay)
    assert "이제 할 일" in src, "회신에 다음 행동 안내가 없다"
    assert "ask_session" in src


def test_hop_limit_is_bounded():
    assert 1 <= session_relay.MAX_HOP <= 5


def test_context_is_capped():
    """A 의 맥락이 통째로 B 에 가면 B 의 다음 대화가 오염된다."""
    src = inspect.getsource(session_relay._build_question)
    assert "CONTEXT_LIMIT" in src


def test_target_stays_in_same_workspace():
    """프로젝트를 넘어 부르면 맥락이 섞인다."""
    src = inspect.getsource(session_relay._resolve_target)
    assert "workspace_id" in src, "워크스페이스 경계를 확인하지 않는다"


def test_set_streaming_unbound_masking_is_fixed():
    """조기 실패가 UnboundLocalError 로 가려지면 안 된다.

    `set_streaming` 은 `send_message_stream` 중간에서 import 된다. 그 전에
    예외가 나면 finally 가 UnboundLocalError 를 던져 **진짜 오류를 가린다.**
    2026-09-14 첫 시험에서 실패 사유가
    "cannot access local variable 'set_streaming'" 으로만 남았다.
    """
    from app.services import chat_service

    src = inspect.getsource(chat_service.send_message_stream)
    tail = src[src.rindex("finally:"):]
    assert "_clear_streaming" in tail, "finally 가 여전히 마스킹한다"
    assert "import set_streaming as _clear_streaming" in tail


def test_target_resolves_korean_aliases():
    """프롬프트는 담당을 한글로 부르는데 세션의 role_key 는 영문이다.

    2026-09-14 실측: `ask_session(target="데이터엔진담당")` 이 그대로
    실패했다. 주도가 자기 프롬프트에 적힌 이름으로 불렀는데 못 찾았다 —
    부를 수 없는 이름을 프롬프트에 적어 둔 셈이었다.
    """
    src = inspect.getsource(session_relay._resolve_target)
    assert "role_scope" in src, "한글 별칭(prompt_assets.role_scope)을 보지 않는다"
    assert "s.title ILIKE" in src, "세션 제목으로도 못 찾는다"


def test_relay_tool_is_registered():
    from app.services.tool_registry import ToolRegistry

    names = [t["name"] for t in ToolRegistry().get_tools("all")]
    assert "ask_session" in names, "도구가 모델에게 노출되지 않는다"


# ── 같은 역할 세션이 둘 이상이면 자동 선택하지 않는다 (2026-09-30) ────────────
#
# 17:17 백억이 세션이 "#119 전략카드 담당에게 목표등록 지시" 를 보냈는데
# 같은 워크스페이스에 role_key='CTO' 세션이 둘 있었고, `LIMIT 1` 이 최근에
# 쓰인 모멘텀 전략가(a0fe4743)를 골라 #119 전략관리자(d19a0e9e)는 받지 못했다.
import asyncio
import datetime as _dt

WS = "11111111-1111-1111-1111-111111111111"
ORIGIN = "00000000-0000-0000-0000-00000000000a"
S1 = "a0fe4743-0000-0000-0000-000000000001"
S2 = "d19a0e9e-0000-0000-0000-000000000002"


def _s(sid, title, role, ts=1):
    return {"id": sid, "title": title, "role_key": role, "workspace_id": WS,
            "updated_at": _dt.datetime(2026, 9, 30, 8, ts, tzinfo=_dt.timezone.utc)}


class _AmbigPool:
    """단계별로 미리 정한 행을 돌려준다. 쿼리 문구로 단계를 구분한다."""

    def __init__(self, sessions, alias_ids=(), title_match=None):
        self.sessions = sessions
        self.alias_ids = set(alias_ids)
        self.title_match = title_match
        self.inserts = []

    async def fetch(self, sql, *args):
        target, origin = args[0], args[1]
        rows = [s for s in self.sessions if s["id"] != origin and s["workspace_id"] == WS]
        if "s.role_key = $1" in sql:
            rows = [s for s in rows if s["role_key"] == target]
        elif "role_scope" in sql:
            rows = [s for s in rows if s["id"] in self.alias_ids]
        elif "s.title ILIKE" in sql:
            rows = [s for s in rows if s["role_key"] and target in s["title"]]
        else:
            raise AssertionError(sql)
        return sorted(rows, key=lambda s: s["updated_at"], reverse=True)[: args[2]]

    async def fetchrow(self, sql, *args):
        return next((s for s in self.sessions if s["id"] == args[0]), None)

    async def execute(self, sql, *args):
        self.inserts.append(sql)
        return "INSERT 0 1"


def _patch_pool(monkeypatch, pool):
    from app.core import db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)


def test_single_role_session_is_returned(monkeypatch):
    """① 회귀 잠금 — 후보 1건이면 지금처럼 그 세션."""
    pool = _AmbigPool([_s(ORIGIN, "백억이", "GO100Owner"), _s(S1, "전략가", "CTO")])
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("CTO", ORIGIN))
    assert tgt["id"] == S1 and not tgt.get("ambiguous")


def test_two_same_role_sessions_are_ambiguous_and_no_relay_row(monkeypatch):
    """② 같은 역할 2건 → 모호 결과 + 릴레이 row 미생성."""
    pool = _AmbigPool([_s(ORIGIN, "백억이", "GO100Owner"),
                       _s(S1, "모멘텀 전략가", "CTO", 9), _s(S2, "#119 전략관리자", "CTO", 1)])
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("CTO", ORIGIN))
    assert tgt["ambiguous"] is True and tgt["stage"] == "role_key"
    assert {c["id"] for c in tgt["candidates"]} == {S1, S2}

    async def _no_hop(_o):
        return 0, "direct_turn"

    monkeypatch.setattr(session_relay, "_hop_basis", _no_hop)
    out = asyncio.run(session_relay.ask(ORIGIN, "CTO", "목표등록 지시"))
    assert out["sent"] is False
    assert out["status"] == "ambiguous_target" and out["error"] == "ambiguous_target"
    assert "같은 역할(CTO) 세션이 2개" in out["message"]
    assert S1 in out["message"] and S2 in out["message"] and "KST" in out["message"]
    assert len(out["candidates"]) == 2
    assert not any("session_relay" in q for q in pool.inserts), "모호한데 릴레이를 만들었다"


def test_alias_and_title_stages_are_ambiguous_and_do_not_fall_through(monkeypatch):
    """③ 별칭·제목 단계에서도 2건이면 모호. 한 단계에서 모호하면 하위 단계로 안 간다."""
    sessions = [_s(ORIGIN, "백억이", "GO100Owner"),
                _s(S1, "전략관리자 A", "CTO", 9), _s(S2, "전략관리자 B", "CFO", 1)]
    pool = _AmbigPool(sessions, alias_ids={S1, S2})
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("전략담당", ORIGIN))
    assert tgt["ambiguous"] and tgt["stage"] == "alias"

    pool = _AmbigPool(sessions)
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("전략관리자", ORIGIN))
    assert tgt["ambiguous"] and tgt["stage"] == "title"

    # 별칭 1건 + 제목 2건이면 별칭 1건이 이긴다(단계 순서 유지).
    pool = _AmbigPool(sessions, alias_ids={S1})
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("전략관리자", ORIGIN))
    assert tgt["id"] == S1 and not tgt.get("ambiguous")


def test_uuid_target_ignores_candidate_count(monkeypatch):
    """④ UUID 직접 지정은 같은 역할이 몇이든 그 세션."""
    pool = _AmbigPool([_s(ORIGIN, "백억이", "GO100Owner"),
                       _s(S1, "전략가", "CTO", 9), _s(S2, "#119", "CTO", 1)])
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target(S2, ORIGIN))
    assert tgt["id"] == S2 and not tgt.get("ambiguous")


def test_origin_session_is_excluded_from_candidates(monkeypatch):
    """⑤ 발신 세션 자신은 후보가 아니다 — 자기 + 다른 1개면 그 1개가 확정."""
    pool = _AmbigPool([_s(ORIGIN, "발신 CTO", "CTO", 9), _s(S1, "다른 CTO", "CTO", 1)])
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("CTO", ORIGIN))
    assert tgt["id"] == S1 and not tgt.get("ambiguous")
    for sql_part in ("s.id <> $2::uuid",):
        assert inspect.getsource(session_relay._resolve_target).count(sql_part) == 3


def test_notify_does_not_deliver_when_ambiguous():
    src = inspect.getsource(session_relay.notify)
    assert "ambiguous_target" in src


# ── 후보 산정의 최근성 필터 (2026-09-30) ─────────────────────────────────────
# 휴면 세션(한 달 전 활동)까지 후보가 되어 되묻기 목록이 흐려지고, 최근 세션이
# 하나뿐인데도 불필요하게 되묻는 문제. `RELAY_CANDIDATE_ACTIVE_DAYS`(기본 7일).
S3 = "b3b3b3b3-0000-0000-0000-000000000003"


def _aged(sid, title, role, days_ago):
    ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)
    return {"id": sid, "title": title, "role_key": role, "workspace_id": WS, "updated_at": ts}


def test_one_recent_of_three_is_returned_without_asking(monkeypatch):
    """① 후보 3건 중 최근 7일 활동 1건 → 되묻지 않고 그 세션."""
    monkeypatch.delenv("RELAY_CANDIDATE_ACTIVE_DAYS", raising=False)
    pool = _AmbigPool([_s(ORIGIN, "백억이", "GO100Owner"),
                       _aged(S1, "최근", "CTO", 1), _aged(S2, "휴면A", "CTO", 27), _aged(S3, "휴면B", "CTO", 29)])
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("CTO", ORIGIN))
    assert tgt["id"] == S1 and not tgt.get("ambiguous")


def test_two_recent_are_ambiguous_and_exclude_dormant(monkeypatch):
    """② 최근 활동 2건 → 모호, 후보에 휴면 세션 미포함, 안내문 개수도 일치."""
    monkeypatch.delenv("RELAY_CANDIDATE_ACTIVE_DAYS", raising=False)
    pool = _AmbigPool([_s(ORIGIN, "백억이", "GO100Owner"),
                       _aged(S1, "최근A", "CTO", 1), _aged(S2, "최근B", "CTO", 3), _aged(S3, "휴면", "CTO", 28)])
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("CTO", ORIGIN))
    assert tgt["ambiguous"] and not tgt.get("stale_only")
    assert {c["id"] for c in tgt["candidates"]} == {S1, S2}
    out = session_relay._ambiguous_response(tgt)
    assert len(out["candidates"]) == 2 and "2개" in out["message"]
    assert S3 not in out["message"] and "활동이 없습니다" not in out["message"]
    assert "stale_only" not in out


def test_all_dormant_falls_back_to_all_with_stale_flag(monkeypatch):
    """③ 전부 휴면 → target_not_found 가 아니라 전체 후보로 모호 + stale_only."""
    monkeypatch.delenv("RELAY_CANDIDATE_ACTIVE_DAYS", raising=False)
    pool = _AmbigPool([_s(ORIGIN, "백억이", "GO100Owner"),
                       _aged(S1, "휴면A", "CTO", 20), _aged(S2, "휴면B", "CTO", 25), _aged(S3, "휴면C", "CTO", 30)])
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target("CTO", ORIGIN))
    assert tgt["ambiguous"] is True and tgt["stale_only"] is True
    assert {c["id"] for c in tgt["candidates"]} == {S1, S2, S3}
    out = session_relay._ambiguous_response(tgt)
    assert out["error"] == "ambiguous_target" and out["stale_only"] is True
    assert "최근 7일 내 활동이 없습니다" in out["message"] and "3개" in out["message"]
    assert len(out["candidates"]) == 3


def test_env_threshold_changes_verdict(monkeypatch):
    """④ 환경변수로 임계값을 바꾸면 판정이 바뀐다. 0 이하는 최소 1일로 보정."""
    pool = _AmbigPool([_s(ORIGIN, "백억이", "GO100Owner"),
                       _aged(S1, "A", "CTO", 2), _aged(S2, "B", "CTO", 10)])
    _patch_pool(monkeypatch, pool)
    monkeypatch.setenv("RELAY_CANDIDATE_ACTIVE_DAYS", "7")
    assert asyncio.run(session_relay._resolve_target("CTO", ORIGIN))["id"] == S1
    monkeypatch.setenv("RELAY_CANDIDATE_ACTIVE_DAYS", "14")
    tgt = asyncio.run(session_relay._resolve_target("CTO", ORIGIN))
    assert tgt["ambiguous"] and {c["id"] for c in tgt["candidates"]} == {S1, S2}
    monkeypatch.setenv("RELAY_CANDIDATE_ACTIVE_DAYS", "0")
    assert session_relay._candidate_active_days() == 1
    monkeypatch.setenv("RELAY_CANDIDATE_ACTIVE_DAYS", "abc")
    assert session_relay._candidate_active_days() == 7


def test_uuid_target_ignores_recency(monkeypatch):
    """⑤ UUID 직접 지정은 최근성과 무관하게 그 세션 — 회귀 잠금."""
    monkeypatch.delenv("RELAY_CANDIDATE_ACTIVE_DAYS", raising=False)
    pool = _AmbigPool([_s(ORIGIN, "백억이", "GO100Owner"),
                       _aged(S1, "최근", "CTO", 1), _aged(S2, "휴면", "CTO", 60)])
    _patch_pool(monkeypatch, pool)
    tgt = asyncio.run(session_relay._resolve_target(S2, ORIGIN))
    assert tgt["id"] == S2 and not tgt.get("ambiguous") and not tgt.get("stale_only")


# ── hop 은 세션 6시간 max 가 아니라 "지금 답하는 질문 줄기" 기준 (2026-10-02) ──
# 세션 3ad8d4c5 가 14:13/14:41/16:39 에 다른 담당에게서 hop3 질문을 받았다는 이유로
# 17:24 CEO 가 직접 지시한 협의(hop 1 이어야 함)가 hop4 로 막혔다.
RID_Q = "11111111-aaaa-aaaa-aaaa-000000000001"
RID_OLD = "22222222-aaaa-aaaa-aaaa-000000000002"
OTHER = "33333333-0000-0000-0000-00000000000c"


class _HopPool:
    """현재 턴 촉발 메시지·relay 행·pending relay 를 흉내 낸다."""

    def __init__(self, trigger="__none__", relays=None, pending_hop=0,
                 lookup_error=False, fallback_error=False):
        self.trigger = trigger
        self.relays = relays or {}
        self.pending_hop = pending_hop
        self.lookup_error = lookup_error
        self.fallback_error = fallback_error
        self.sqls = []

    async def fetchval(self, sql, *args):
        self.sqls.append(sql)
        if "FROM chat_turn_executions" in sql or "FROM chat_messages" in sql:
            if self.lookup_error:
                raise RuntimeError("db down")
            if "FROM chat_turn_executions" in sql:
                return None if self.trigger == "__none__" else self.trigger
            return None
        if "max(hop)" in sql:
            if self.fallback_error:
                raise RuntimeError("db down")
            return self.pending_hop or None
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        assert "FROM session_relay WHERE id" in sql
        return self.relays.get(args[0])


def _relay(hop, origin, target):
    return {"hop": hop, "origin": origin, "target": target}


def _hop(monkeypatch, pool):
    _patch_pool(monkeypatch, pool)
    return asyncio.run(session_relay._hop_basis(ORIGIN))


def test_direct_turn_ignores_old_hop3_history(monkeypatch):
    """① 6시간 내 hop3 질문을 받은 이력이 있어도 직접 메시지 턴이면 hop 0 → ask 는 hop=1."""
    pool = _HopPool(
        trigger="TASK_ID: X — 이 협의를 진행하세요",
        relays={RID_OLD: _relay(3, OTHER, ORIGIN)},
        pending_hop=3,
    )
    assert _hop(monkeypatch, pool) == (0, "direct_turn")
    assert not any("max(hop)" in q for q in pool.sqls), "직접 턴인데 세션 이력(max)을 봤다"

    class _AskPool(_AmbigPool):
        async def fetchval(self, sql, *args):
            return None  # _pair_in_flight / _target_is_busy / _relay_goal 모두 비어 있음

    async def _fake_hop(_o):
        return 0, "direct_turn"

    ask_pool = _AskPool([_s(ORIGIN, "백억이", "GO100Owner"), _s(S1, "전략가", "CTO")])
    _patch_pool(monkeypatch, ask_pool)
    monkeypatch.setattr(session_relay, "_hop_basis", _fake_hop)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(session_relay, "_pair_in_flight", lambda *a: _false())
    monkeypatch.setattr(session_relay, "_target_is_busy", lambda *a: _false())
    monkeypatch.setattr(session_relay, "_relay_goal", _noop)
    monkeypatch.setattr(session_relay, "_relay_paused", lambda *a: _false())
    monkeypatch.setattr(session_relay, "_run_relay", _noop)

    async def _go():
        out = await session_relay.ask(ORIGIN, "CTO", "협의 요청")
        await asyncio.sleep(0)
        return out

    out = asyncio.run(_go())
    assert out["sent"] is True and out["hop"] == 1


async def _false():
    return False


def test_answering_hop2_question_allows_hop3_and_hop3_is_blocked(monkeypatch):
    """② hop2 질문에 답하는 턴 → 새 질문 hop3 허용. hop3 질문에 답하는 턴 → hop_limit."""
    prompt2 = session_relay._build_question("T", "CTO", OTHER, "GO100Owner", "질문", "", RID_Q)
    pool = _HopPool(trigger=prompt2, relays={RID_Q: _relay(2, OTHER, ORIGIN)})
    base, basis = _hop(monkeypatch, pool)
    assert (base, basis) == (2, f"relay_question:{RID_Q[:8]}:hop2")
    assert base + 1 <= session_relay.MAX_HOP == 3

    prompt3 = session_relay._build_question("T", "CTO", OTHER, "GO100Owner", "질문", "", RID_Q)
    pool = _HopPool(trigger=prompt3, relays={RID_Q: _relay(3, OTHER, ORIGIN)})
    _patch_pool(monkeypatch, pool)
    out = asyncio.run(session_relay.ask(ORIGIN, "CTO", "또 묻기"))
    assert out["sent"] is False and out["error"] == "hop_limit"
    assert f"relay_question:{RID_Q[:8]}:hop3" in out["message"]
    assert out["hop_basis"] == f"relay_question:{RID_Q[:8]}:hop3"


def test_turn_that_received_reply_uses_relay_hop(monkeypatch):
    """③ 회신을 받은 턴에서 다시 물으면 해당 relay hop + 1 (루프 방지 유지)."""
    reply = (
        "📨 **CTO의 답이 도착했습니다**\n> 물어본 질문: 뭐였지\n\n답 본문 "
        f"(relay_id={RID_OLD}) 를 인용함\n\n── 이제 할 일 ──\n진행하세요."
        + session_relay._relay_tag(RID_Q)
    )
    pool = _HopPool(trigger=reply, relays={RID_Q: _relay(2, ORIGIN, OTHER),
                                           RID_OLD: _relay(1, OTHER, S1)})
    base, basis = _hop(monkeypatch, pool)
    # 본문이 인용한 표식이 아니라 꼬리의 우리 표식을 쓴다.
    assert (base, basis) == (2, f"relay_reply:{RID_Q[:8]}:hop2")


def test_marker_roundtrip_for_question_and_reply():
    q = session_relay._build_question("T", "CTO", OTHER, "GO100Owner", "질문", "ctx", RID_Q)
    assert session_relay._relay_id_from(q) == RID_Q
    assert session_relay._looks_like_relay(q)
    assert session_relay._relay_id_from(session_relay._build_question("T", "", OTHER, "", "q", "")) is None
    assert "_relay_tag(relay_id)" in inspect.getsource(session_relay._run_relay)


def test_unidentifiable_turn_uses_conservative_path(monkeypatch):
    """④ 판별 실패 → 최근 pending relay 의 hop, 없으면 0, 기준이 기록된다."""
    # 표식 없는 옛 형식 relay 질문
    legacy = "[CTO에게 — 백억이(3ad8d4c5)의 질문]\n\n질문"
    base, basis = _hop(monkeypatch, _HopPool(trigger=legacy, pending_hop=3))
    assert base == 3 and basis.startswith("relay_marker_missing:fallback_pending_relay_hop3")
    base, basis = _hop(monkeypatch, _HopPool(trigger=legacy))
    assert base == 0 and basis == "relay_marker_missing:no_pending_relay"
    # 표식은 있는데 relay 행이 없다
    base, basis = _hop(monkeypatch, _HopPool(trigger=f"x (relay_id={RID_Q})", pending_hop=2))
    assert base == 2 and basis.startswith("relay_row_missing:")
    # 다른 세션 사이의 relay 표식
    foreign = {RID_Q: _relay(3, OTHER, S1)}
    base, basis = _hop(monkeypatch, _HopPool(trigger=f"x (relay_id={RID_Q})", relays=foreign, pending_hop=1))
    assert base == 1 and basis.startswith("relay_row_foreign:")
    # 촉발 메시지를 못 찾음
    base, basis = _hop(monkeypatch, _HopPool(trigger="__none__", pending_hop=2))
    assert base == 2 and basis.startswith("no_trigger_message:")
    # 조회 오류 → 보수 경로 → 그것도 오류면 0 + 기준 기록
    base, basis = _hop(monkeypatch, _HopPool(lookup_error=True, pending_hop=2))
    assert base == 2 and basis.startswith("lookup_error:RuntimeError")
    base, basis = _hop(monkeypatch, _HopPool(lookup_error=True, fallback_error=True))
    assert base == 0 and "fallback_error" in basis


def test_fallback_is_time_bounded_and_pending_only():
    src = inspect.getsource(session_relay._conservative_hop)
    assert "status = 'pending'" in src and "_HOP_FALLBACK_MIN" in src
    assert "6 hours" not in inspect.getsource(session_relay._hop_basis)


def test_current_hop_is_thin_wrapper_over_hop_basis(monkeypatch):
    """`_current_hop` 은 삭제되지 않고 `_hop_basis` 의 hop 값만 돌려준다."""
    assert asyncio.iscoroutinefunction(session_relay._current_hop)
    assert list(inspect.signature(session_relay._current_hop).parameters) == ["origin_session_id"]

    async def _basis(_o):
        return 2, "relay_question:deadbeef:hop2"

    monkeypatch.setattr(session_relay, "_hop_basis", _basis)
    assert asyncio.run(session_relay._current_hop(ORIGIN)) == 2


def test_ceo_direct_turn_with_three_old_hop3_relays_sends_hop1(monkeypatch):
    """세션 3ad8d4c5 실측: 6시간 내 hop3 질문 3건(pending/answered) 이력이 있어도
    CEO 직접 턴의 ask() 는 실제 `_hop_basis`/`_current_hop` 경로로 hop=1 로 전송된다."""
    old = [(f"4444444{i}-aaaa-aaaa-aaaa-00000000000{i}", st) for i, st in
           enumerate(["pending", "answered", "answered"], start=1)]
    relays = {rid: _relay(3, OTHER, ORIGIN) for rid, _ in old}

    class _P(_AmbigPool):
        async def fetchval(self, sql, *args):
            if "FROM chat_turn_executions" in sql:
                return "TASK_ID: X — CEO 가 직접 시킨 협의를 진행하세요"
            if "max(hop)" in sql:
                return 3  # 옛 구현이 읽던 6시간 이력 — 직접 턴이면 읽으면 안 된다
            return None

        async def fetchrow(self, sql, *args):
            if "FROM session_relay WHERE id" in sql:
                return relays.get(args[0])
            return await super().fetchrow(sql, *args)

    pool = _P([_s(ORIGIN, "백억이", "GO100Owner"), _s(S1, "전략가", "CTO")])
    _patch_pool(monkeypatch, pool)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(session_relay, "_pair_in_flight", lambda *a: _false())
    monkeypatch.setattr(session_relay, "_target_is_busy", lambda *a: _false())
    monkeypatch.setattr(session_relay, "_relay_goal", _noop)
    monkeypatch.setattr(session_relay, "_relay_paused", lambda *a: _false())
    monkeypatch.setattr(session_relay, "_run_relay", _noop)

    assert asyncio.run(session_relay._current_hop(ORIGIN)) == 0

    async def _go():
        out = await session_relay.ask(ORIGIN, "CTO", "협의 요청")
        await asyncio.sleep(0)
        return out

    out = asyncio.run(_go())
    assert out["sent"] is True and out["hop"] == 1


def test_max_hop_env_and_notify_path_unchanged():
    """MAX_HOP 기본 3 · env 동작 불변, 알림 경로는 hop 판별을 타지 않는다."""
    src = inspect.getsource(session_relay)
    assert 'int(os.getenv("SESSION_RELAY_MAX_HOP", "3"))' in src
    notify_src = inspect.getsource(session_relay.notify) + inspect.getsource(session_relay._run_notify)
    assert "_hop_basis" not in notify_src and "_relay_tag" not in notify_src
