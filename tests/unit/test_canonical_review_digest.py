"""검토 대기 정본 문서 일일 요약: 조회 조건, 0건 미발송, 하루 1회, 메시지 형식, API 라우트."""
import asyncio
from datetime import datetime, timezone

from app.api import canonical_documents as docs
from app.services import canonical_review_digest as digest

T1 = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=digest.KST)


def row(key, *, project="AADS", title="제목", days_ago=3, tenant=T1):
    review_at = datetime(2026, 10, 28 - days_ago, 12, 0, tzinfo=digest.KST).astimezone(timezone.utc)
    return {"tenant_id": tenant, "project_key": project, "document_key": key, "kind": "prd",
            "title": title, "review_at": review_at}


class FakeTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, rows, log):
        self.rows, self.log, self.executed = rows, log, []

    def transaction(self):
        return FakeTx()

    async def fetch(self, sql, *args):
        self.last_fetch = (sql, args)
        if "FROM tenant_memberships" in sql:
            return getattr(self, "recipients", [{"user_id": "ceo"}])
        return self.rows

    async def fetchval(self, sql, *args):
        return 1 if (args[0], args[1]) in self.log else None

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if sql.startswith("INSERT INTO canonical_review_digest_log"):
            self.log.add((args[0], args[1]))


class FakePool:
    def __init__(self, rows):
        self.log = set()
        self.conn = FakeConn(rows, self.log)

    def acquire(self):
        pool = self

        class Ctx(FakeTx):
            async def __aenter__(self):
                return pool.conn

        return Ctx()


def test_queue_sql_only_review_state_and_not_yet_approved():
    sql = digest._QUEUE_SQL
    assert "e.action IN ('review','approved','archived')" in sql
    assert "ORDER BY e.id DESC LIMIT 1" in sql
    assert "ev.action='review'" in sql
    assert "h.approved_revision_id IS DISTINCT FROM h.latest_revision_id" in sql
    assert "e.revision_id=h.latest_revision_id" in sql


def test_fetch_scopes_tenant_and_computes_kst_waiting_days():
    conn = FakeConn([row("a", days_ago=3)], set())
    items = asyncio.run(digest.fetch_review_queue(conn, T1, "AADS", now=NOW))
    assert conn.last_fetch[1] == (T1, "AADS")
    assert items[0]["waiting_days"] == 3
    assert items[0]["review_at"].endswith("+09:00")


def test_zero_pending_sends_nothing():
    sent = []

    async def sender(text):
        sent.append(text)
        return True

    result = asyncio.run(digest.send_daily_digest(FakePool([]), now=NOW, sender=sender))
    assert sent == [] and result["sent"] == 0


def test_same_kst_day_sends_once():
    sent = []

    async def sender(text):
        sent.append(text)
        return True

    pool = FakePool([row("a"), row("b")])
    first = asyncio.run(digest.send_daily_digest(pool, now=NOW, sender=sender))
    second = asyncio.run(digest.send_daily_digest(pool, now=NOW, sender=sender))
    assert len(sent) == 1 and first["sent"] == 1 and second["sent"] == 0 and second["skipped"] == 1
    assert any("pg_advisory_xact_lock" in s for s in pool.conn.executed)
    next_day = datetime(2026, 10, 29, 9, 0, tzinfo=digest.KST)
    asyncio.run(digest.send_daily_digest(pool, now=next_day, sender=sender))
    assert len(sent) == 2


def test_failed_send_is_not_recorded():
    async def sender(text):
        return False

    pool = FakePool([row("a")])
    result = asyncio.run(digest.send_daily_digest(pool, now=NOW, sender=sender))
    assert result["failed"] == 1 and not pool.log


def test_message_format_groups_by_project_and_caps_ten():
    items = [{"project_key": "AADS", "document_key": f"doc-{i:02d}", "title": f"문서 {i}", "waiting_days": i}
             for i in range(12)]
    items.append({"project_key": "KIS", "document_key": "kis-1", "title": "<b>위험</b>", "waiting_days": 1})
    text = digest.format_digest(items)
    assert "검토 대기 정본 문서 13건" in text
    assert "AADS</b> (12건)" in text and "… 외 2건" in text
    assert text.count("doc-") == 10
    assert "doc-11" in text and "doc-00" not in text
    assert "11일 대기" in text
    assert "&lt;b&gt;위험" in text
    assert "정본 문서" in text and "승인" in text


def test_review_queue_route_precedes_document_key_route():
    paths = [r.path for r in docs.router.routes if "GET" in getattr(r, "methods", ())]
    assert paths.index(docs.router.prefix + "/review-queue") < paths.index(docs.router.prefix + "/{document_key}")


def mock_channels(monkeypatch, *, sent=1, configured=True):
    from unittest.mock import AsyncMock
    from app.services import push_notifications, ceo_notify

    push = AsyncMock(return_value={"sent": sent, "failed": int(not sent)})
    telegram = AsyncMock(side_effect=RuntimeError("telegram unavailable"))
    monkeypatch.setattr(push_notifications, "is_web_push_configured", lambda: configured)
    monkeypatch.setattr(push_notifications, "send_web_push_to_user", push)
    monkeypatch.setattr(ceo_notify, "send_ceo_text", telegram)
    return push, telegram


def test_web_push_success_records_and_second_call_skips(monkeypatch):
    push, telegram = mock_channels(monkeypatch)
    pool = FakePool([row("a")])
    first = asyncio.run(digest.send_daily_digest(pool, now=NOW))
    sql, args = pool.conn.last_fetch
    assert args == (T1,)
    assert "role IN ('owner','admin')" in sql
    assert "status='active'" in sql and "deleted_at IS NULL" in sql
    second = asyncio.run(digest.send_daily_digest(pool, now=NOW))
    assert first["sent"] == 1 and len(pool.log) == 1
    assert second["skipped"] == 1
    assert push.await_count == telegram.await_count == 1
    assert push.call_args.kwargs["tenant_id"] == T1
    assert push.call_args.kwargs["user_id"] == "ceo"
    assert push.call_args.kwargs["payload"]["tag"] == "canonical-review-digest-20261028"


def test_zero_web_push_and_telegram_failure_does_not_record(monkeypatch):
    push, telegram = mock_channels(monkeypatch, sent=0)
    pool = FakePool([row("a")])
    result = asyncio.run(digest.send_daily_digest(pool, now=NOW))
    assert result["failed"] == 1 and not pool.log
    assert push.await_count == telegram.await_count == 1


def test_no_recipient_skips(monkeypatch):
    push, telegram = mock_channels(monkeypatch)
    pool = FakePool([row("a")])
    pool.conn.recipients = []
    result = asyncio.run(digest.send_daily_digest(pool, now=NOW))
    assert result["skipped"] == 1 and not pool.log
    push.assert_not_awaited()
    telegram.assert_not_awaited()


def test_vapid_not_configured_skips(monkeypatch):
    push, _ = mock_channels(monkeypatch, configured=False)
    pool = FakePool([row("a")])
    result = asyncio.run(digest.send_daily_digest(pool, now=NOW))
    assert result["skipped"] == 1 and not pool.log
    push.assert_not_awaited()


def test_push_payload_plain_bounded_and_oldest_first():
    items = asyncio.run(digest.fetch_review_queue(
        FakeConn([row("new", days_ago=1), row("old", title="<b>오래된</b>", days_ago=4)], set()),
        T1, now=NOW,
    ))
    payload = digest.format_push_payload(items, NOW.date())
    assert payload["title"] == "검토 대기 정본 2건"
    assert payload["url"] == "/docs"
    assert payload["body"] == "AADS 2건\n오래된 (4일); 제목 (1일)"
    items[0]["title"] = "긴 제목" * 100
    assert len(digest.format_push_payload(items, NOW.date())["body"]) <= 240


def test_recipient_lookup_failure_skips(monkeypatch):
    from unittest.mock import AsyncMock

    push, _ = mock_channels(monkeypatch)
    pool = FakePool([row("a")])
    original_fetch = pool.conn.fetch

    async def fetch(sql, *args):
        if "FROM tenant_memberships" in sql:
            raise RuntimeError("membership unavailable")
        return await original_fetch(sql, *args)

    pool.conn.fetch = AsyncMock(side_effect=fetch)
    result = asyncio.run(digest.send_daily_digest(pool, now=NOW))
    assert result["skipped"] == 1 and not pool.log
    push.assert_not_awaited()


def test_partial_recipient_success_records(monkeypatch):
    push, _ = mock_channels(monkeypatch)
    push.side_effect = [RuntimeError("push unavailable"), {"sent": 1}]
    pool = FakePool([row("a")])
    pool.conn.recipients = [{"user_id": "owner"}, {"user_id": "admin"}]
    result = asyncio.run(digest.send_daily_digest(pool, now=NOW))
    assert result["sent"] == 1 and len(pool.log) == 1
    assert push.await_count == 2
