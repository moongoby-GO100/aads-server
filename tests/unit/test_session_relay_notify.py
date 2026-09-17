"""외부 시스템 알림 → 담당 세션 수신 경로 (AADS-RELAY-INGEST-N1).

2026-09-17 CEO: "텔레그램은 알림에서 제외한다. 담당 세션에 알림 주고 조치할
수 있게 해라." 빼기 전에 받을 곳을 먼저 만들어야 한다 — 그때까지 세션에
무언가를 넣는 경로는 `ask_session` 도구 하나뿐이었다.

여기서는 소스 문자열이 아니라 **동작**을 본다. 인메모리 가짜 풀 위에서
`notify()` 를 실제로 실행해

* 정상 전달   — session_relay 에 queued 행이 생기고 대상이 그 역할의 세션인가
* 역할 미존재 — 행을 만들지 않고 target_not_found 를 돌려주는가(조용히 버리지 않는가)
* dedup 차단  — 같은 dedup_key 가 창 안에 다시 오면 행이 늘지 않는가

를 행 상태로 확인한다. DB 도 컨테이너도 건드리지 않는다.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services import session_relay

TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"
DATA_OWNER = "11111111-1111-4111-8111-111111111111"
OPS_OWNER = "22222222-2222-4222-8222-222222222222"


class _FakePool:
    """`notify()` 가 실제로 쓰는 쿼리만 구현한 가짜 풀.

    행을 진짜로 넣으므로 "두 번째 호출은 행이 안 는다"(dedup)나 "못 찾으면
    한 건도 안 남는다"(폴백) 를 상태 변화로 증명할 수 있다.
    """

    def __init__(self, sessions=None, *, dedup_column=True):
        self.sessions = sessions if sessions is not None else [
            {"id": DATA_OWNER, "title": "데이터엔진 담당", "role_key": "DataEngineOwner",
             "workspace_id": "w1", "tenant_id": TENANT, "project_key": "GO100",
             "updated_at": 2},
            {"id": OPS_OWNER, "title": "운영인프라 담당", "role_key": "OpsInfraOwner",
             "workspace_id": "w1", "tenant_id": TENANT, "project_key": "GO100",
             "updated_at": 1},
        ]
        self.relays: list[dict] = []
        self.dedup_column = dedup_column
        self.queries: list[str] = []

    # -- 조회 --------------------------------------------------------------
    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        self.queries.append(q)
        if "information_schema.columns" in q:
            return 1 if self.dedup_column else None
        if "SELECT id::text FROM session_relay" in q and "dedup_key = $2" in q:
            origin, key, window = args
            for row in reversed(self.relays):
                if row["origin_session_id"] == origin and row.get("dedup_key") == key:
                    return row["id"]
            return None
        raise AssertionError(f"예상하지 못한 fetchval: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        self.queries.append(q)
        assert "FROM chat_sessions s" in q, f"예상하지 못한 fetch: {q}"
        role, tenant_id, project = args
        out = []
        for s in self.sessions:
            if s["tenant_id"] != tenant_id:
                continue
            if project and (s.get("project_key") or "").upper() != project:
                continue
            if "lower($1)" in q:
                if (s.get("role_key") or "").lower() != role.lower():
                    continue
            elif "role_scope" in q:
                # 별칭 표는 이 테스트에서 비어 있다 — 1) 갈래로 못 찾으면
                # 여기서도 못 찾아야 3) 제목 갈래까지 내려간다.
                continue
            elif "ILIKE" in q:
                if role not in (s.get("title") or ""):
                    continue
            out.append(dict(s))
        out.sort(key=lambda r: r["updated_at"], reverse=True)
        return out[:5]

    # -- 쓰기 --------------------------------------------------------------
    async def execute(self, query, *args):
        q = " ".join(query.split())
        self.queries.append(q)
        assert "INSERT INTO session_relay" in q, f"예상하지 못한 execute: {q}"
        row = {
            "id": args[0], "origin_session_id": args[1], "target_session_id": args[2],
            "hop": 0, "question": args[3], "status": "queued",
        }
        if "dedup_key" in q:
            row["dedup_key"] = args[4]
        self.relays.append(row)
        return "INSERT 0 1"


@pytest.fixture
def pool(monkeypatch):
    fake = _FakePool()
    # 캐시된 컬럼 판정이 테스트 사이에 새지 않게 한다.
    monkeypatch.setattr(session_relay, "_dedup_column_ready", None, raising=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: fake)
    return fake


def _run(coro):
    return asyncio.run(coro)


# ─── 1. 정상 전달 ────────────────────────────────────────────────────────────
def test_notification_reaches_role_session(pool):
    """역할 키로 부르면 그 담당 세션 앞으로 queued 행이 생긴다."""
    result = _run(session_relay.notify(
        "DataEngineOwner", "수집 지연", "KRX 일봉이 09:20 까지 안 들어왔다",
        tenant_id=TENANT, project="GO100", severity="critical",
        dedup_key="go100:ingest-delay", source="go100-cron@contabo14",
    ))

    assert result["queued"] is True
    assert result["target_session"] == DATA_OWNER
    assert result["target_role"] == "DataEngineOwner"
    assert result["severity"] == "critical"

    assert len(pool.relays) == 1
    row = pool.relays[0]
    assert row["status"] == "queued", "대기열에 들어가지 않으면 배달기가 집어가지 않는다"
    assert row["target_session_id"] == DATA_OWNER
    assert row["origin_session_id"] == session_relay.SYSTEM_ORIGIN_SESSION_ID
    assert row["hop"] == 0, "알림이 협업 홉 예산을 깎으면 안 된다"
    assert row["dedup_key"] == "go100:ingest-delay"
    # 담당이 읽고 조치할 수 있는 본문인가
    assert "수집 지연" in row["question"]
    assert "KRX 일봉" in row["question"]
    assert "이제 할 일" in row["question"], "무엇을 하라는 안내가 없다"
    assert "go100-cron@contabo14" in row["question"], "발신자를 알 수 없다"
    assert uuid.UUID(row["id"])


def test_same_role_many_sessions_picks_most_recent(pool):
    """같은 역할 세션이 여럿이면 가장 최근에 움직인 하나만 고른다.

    전부에 넣으면 같은 알림에 담당 셋이 각자 조치해 서로를 덮어쓴다.
    """
    pool.sessions.append({
        "id": "33333333-3333-4333-8333-333333333333", "title": "데이터엔진 담당(구)",
        "role_key": "DataEngineOwner", "workspace_id": "w1", "tenant_id": TENANT,
        "project_key": "GO100", "updated_at": 99,
    })
    result = _run(session_relay.notify(
        "DataEngineOwner", "수집 지연", "본문", tenant_id=TENANT, project="GO100",
    ))
    assert result["target_session"] == "33333333-3333-4333-8333-333333333333"
    assert result["candidates"] == 2, "후보 수를 알려주지 않으면 왜 이 세션인지 모른다"
    assert len(pool.relays) == 1, "역할당 한 건만 나가야 한다"


# ─── 2. 역할 미존재 ──────────────────────────────────────────────────────────
def test_unknown_role_is_rejected_not_swallowed(pool):
    """갈 곳이 없으면 실패를 돌려준다 — 조용히 버리면 알림이 사라진 줄 모른다."""
    result = _run(session_relay.notify(
        "NoSuchOwner", "수집 지연", "본문", tenant_id=TENANT, project="GO100",
    ))
    assert result["queued"] is False
    assert result["error"] == "target_not_found"
    assert "NoSuchOwner" in result["message"]
    assert pool.relays == [], "찾지도 못한 대상에 행을 만들면 안 된다"


def test_other_tenant_session_is_not_a_target(pool):
    """테넌트를 넘어 알림이 가면 남의 대화에 우리 사고가 들어간다."""
    result = _run(session_relay.notify(
        "DataEngineOwner", "수집 지연", "본문",
        tenant_id="99999999-9999-4999-8999-999999999999",
    ))
    assert result["error"] == "target_not_found"
    assert pool.relays == []


def test_project_narrows_the_target(pool):
    """project 를 주면 그 워크스페이스 밖의 동명 담당은 대상이 아니다."""
    result = _run(session_relay.notify(
        "DataEngineOwner", "수집 지연", "본문", tenant_id=TENANT, project="NTV2",
    ))
    assert result["error"] == "target_not_found"
    assert pool.relays == []


# ─── 3. dedup 차단 ───────────────────────────────────────────────────────────
def test_same_dedup_key_within_window_is_blocked(pool):
    """5분마다 도는 cron 이 같은 알림을 다시 보내도 세션은 한 번만 깨어난다."""
    first = _run(session_relay.notify(
        "DataEngineOwner", "수집 지연", "본문", tenant_id=TENANT,
        project="GO100", dedup_key="go100:ingest-delay",
    ))
    second = _run(session_relay.notify(
        "DataEngineOwner", "수집 지연", "본문", tenant_id=TENANT,
        project="GO100", dedup_key="go100:ingest-delay",
    ))

    assert first["queued"] is True
    assert second["queued"] is False
    assert second["error"] == "duplicate"
    assert second["deduplicated"] is True
    assert second["relay_id"] == first["relay_id"], "앞서 전달된 건을 알려줘야 한다"
    assert len(pool.relays) == 1, "같은 알림이 두 번 들어갔다"


def test_dedup_key_is_derived_when_omitted(pool):
    """키를 안 붙이는 cron 이 정상에 가깝다. 내용이 같으면 그래도 막는다."""
    _run(session_relay.notify("DataEngineOwner", "수집 지연", "본문", tenant_id=TENANT))
    again = _run(session_relay.notify("DataEngineOwner", "수집 지연", "본문", tenant_id=TENANT))
    assert again["error"] == "duplicate"
    assert len(pool.relays) == 1


def test_different_content_is_not_a_duplicate(pool):
    """상태가 바뀌어 새로 보낸 알림까지 막으면 그게 더 큰 사고다."""
    _run(session_relay.notify("DataEngineOwner", "수집 지연", "09:20 미도착", tenant_id=TENANT))
    _run(session_relay.notify("DataEngineOwner", "수집 지연", "09:50 미도착", tenant_id=TENANT))
    assert len(pool.relays) == 2


def test_dedup_column_missing_does_not_drop_the_alert(monkeypatch):
    """이미지가 먼저 뜨고 DB 자산이 나중에 적용되는 순서를 견딘다.

    컬럼이 없으면 중복 방지만 쉬고 알림은 간다 — 긴급 알림이 사라지는 쪽이
    중복 한 건보다 훨씬 나쁘다.
    """
    fake = _FakePool(dedup_column=False)
    monkeypatch.setattr(session_relay, "_dedup_column_ready", None, raising=False)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: fake)

    result = _run(session_relay.notify(
        "DataEngineOwner", "수집 지연", "본문", tenant_id=TENANT, dedup_key="k",
    ))
    assert result["queued"] is True
    assert len(fake.relays) == 1
    assert "dedup_key" not in fake.relays[0], "없는 컬럼에 쓰려 하면 INSERT 가 터진다"


# ─── 입력 검증 / 경로 결선 ───────────────────────────────────────────────────
def test_blank_role_and_title_are_rejected(pool):
    assert _run(session_relay.notify("", "제목", "본문", tenant_id=TENANT))["error"] == "target_role_required"
    assert _run(session_relay.notify("DataEngineOwner", "  ", "본문", tenant_id=TENANT))["error"] == "title_required"
    assert _run(session_relay.notify("DataEngineOwner", "제목", "본문", tenant_id=""))["error"] == "tenant_required"
    assert pool.relays == []


def test_unknown_severity_falls_back_to_info(pool):
    """등급을 잘못 적었다고 알림을 버리지 않는다."""
    result = _run(session_relay.notify(
        "DataEngineOwner", "제목", "본문", tenant_id=TENANT, severity="EMERGENCY",
    ))
    assert result["queued"] is True
    assert result["severity"] == "info"


def test_system_notifications_bypass_ask_loop_guards():
    """알림 경로가 홉·같은 쌍 제한에 걸리면 cron 알림이 통째로 버려진다.

    `_pair_in_flight` 는 앞 건이 pending 인 동안 뒤 건을 전부 반려하는데,
    같은 쌍이 연달아 오는 것이 알림에서는 정상이다.
    """
    import inspect

    src = inspect.getsource(session_relay.notify)
    assert "_pair_in_flight" not in src
    assert "_current_hop" not in src
    # 반대로 ask 는 그 보호를 그대로 유지해야 한다
    ask_src = inspect.getsource(session_relay.ask)
    assert "_pair_in_flight" in ask_src and "_current_hop" in ask_src


def test_dispatcher_routes_system_rows_to_notification_runner():
    """시스템 발신 행을 `_run_relay` 로 태우면 없는 세션에 회신하려다 실패한다."""
    import inspect

    src = inspect.getsource(session_relay.dispatch_queued_relays)
    assert "SYSTEM_ORIGIN_SESSION_ID" in src, "알림 행을 갈라내지 않는다"
    assert "_run_notification" in src
    # 배달 대기열(대상이 한가해질 때까지 기다린다)은 알림도 같이 쓴다
    assert "_target_is_busy" in src

    run_src = inspect.getsource(session_relay._run_notification)
    assert "_deliver_answer" not in run_src, "알림에는 돌려줄 발신 세션이 없다"
    assert "system_trigger" in run_src, "담당이 읽고 움직이는 경로를 쓰지 않는다"


def test_endpoint_is_registered_with_tenant_auth():
    """새 시크릿 없이 기존 테넌트 인증을 그대로 쓴다 (R-KEY)."""
    import inspect

    from app.api import notifications as api

    paths = [r.path for r in api.router.routes]
    assert "/notifications/relay" in paths

    src = inspect.getsource(api.relay_notification_to_session)
    assert "Depends(get_current_user)" in inspect.getsource(api)
    assert "tenant_id" in src, "테넌트를 넘기지 않으면 남의 세션에 갈 수 있다"
    for token in ("sk-ant-", "api_key", "API_KEY"):
        assert token not in src
