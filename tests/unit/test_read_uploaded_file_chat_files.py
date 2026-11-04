"""read_uploaded_file 이 현재 채팅 세션 첨부(chat_files)를 목록·검색에서 빠뜨리지 않는지 검증한다.

DB 는 가짜 pool/conn 으로 대체한다.
"""
import datetime as dt
import uuid

import pytest

SESSION_A = str(uuid.uuid4())
SESSION_B = str(uuid.uuid4())
WS_A = uuid.uuid4()
WS_B = uuid.uuid4()
_T0 = dt.datetime(2026, 11, 5, 7, 0, 0)


def _ilike(value, pattern):
    return pattern.strip("%").lower() in str(value).lower()


class _FakeConn:
    def __init__(self, chat_files, drive_files):
        self.chat_files = chat_files
        self.drive_files = drive_files

    async def fetch(self, sql, *args):
        if "FROM chat_files" in sql:
            rows = [r for r in self.chat_files if str(r["session_id"]) == str(args[0])]
        elif "FROM chat_drive_files" in sql:
            rows = list(self.drive_files)
            if "id = $1" in sql:
                rows = [r for r in rows if r["id"] == args[0]]
                if "workspace_id = $2" in sql:
                    rows = [r for r in rows if r["workspace_id"] == args[1]]
            elif "workspace_id = $1 AND filename ILIKE $2" in sql:
                rows = [r for r in rows if r["workspace_id"] == args[0] and _ilike(r["filename"], args[1])]
            elif "filename ILIKE $1" in sql:
                rows = [r for r in rows if _ilike(r["filename"], args[0])]
            elif "workspace_id = $1" in sql:
                rows = [r for r in rows if r["workspace_id"] == args[0]]
        else:
            raise AssertionError(f"예상 밖 SQL: {sql}")
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    async def fetchrow(self, sql, *args):
        assert "FROM chat_files" in sql, sql
        rows = sorted(self.chat_files, key=lambda r: r["created_at"], reverse=True)
        if "WHERE id = $1" in sql:
            rows = [r for r in rows if r["id"] == args[0]]
            if "session_id = $2" in sql:
                rows = [r for r in rows if str(r["session_id"]) == str(args[1])]
        elif "original_name ILIKE $1" in sql:
            rows = [r for r in rows if _ilike(r["original_name"], args[0])]
            if "session_id = $2" in sql:
                rows = [r for r in rows if str(r["session_id"]) == str(args[1])]
        return rows[0] if rows else None


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _SessionVar:
    def __init__(self, value):
        self._value = value

    def get(self, default=""):
        return self._value or default


def _chat_row(session_id, name, path, mime="text/csv", size=100, minutes=0):
    return {
        "id": uuid.uuid4(), "session_id": uuid.UUID(session_id), "original_name": name,
        "mime_type": mime, "file_size": size, "storage_path": str(path),
        "created_at": _T0 + dt.timedelta(minutes=minutes),
    }


def _drive_row(name, path, ws=WS_A, minutes=0):
    return {
        "id": uuid.uuid4(), "workspace_id": ws, "filename": name, "file_path": str(path),
        "file_type": "txt", "file_size": 10, "created_at": _T0 - dt.timedelta(days=200, minutes=-minutes),
    }


def _setup(monkeypatch, chat_files, drive_files, session_id=SESSION_A):
    from app.core import db_pool
    from app.services import tool_executor as te

    monkeypatch.setattr(db_pool, "get_pool", lambda: _FakePool(_FakeConn(chat_files, drive_files)))
    monkeypatch.setattr(te, "current_chat_session_id", _SessionVar(session_id))
    return te.ToolExecutor()


def _make_xlsx(path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "2024"
    ws.append(["월급여", "공제대상", "세액"])
    ws.append([1060, 1, None])
    ws2 = wb.create_sheet("2026")
    ws2.append(["a", "b"])
    wb.save(path)


@pytest.mark.asyncio
async def test_t1_empty_filename_lists_session_attachments_first(monkeypatch, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    chat = [
        _chat_row(SESSION_A, "간이세액_2024.xlsx", f, minutes=1),
        _chat_row(SESSION_A, "간이세액_2026.xlsx", f, minutes=2),
        _chat_row(SESSION_B, "남의파일.csv", f, minutes=3),
    ]
    drive = [_drive_row(f"old_{i}.txt", f, minutes=i) for i in range(13)]
    ex = _setup(monkeypatch, chat, drive)

    res = await ex._read_uploaded_file({"filename": ""})

    assert res["status"] == "list"
    assert res["total"] == 15
    first = res["files"][:2]
    assert [x["source"] for x in first] == ["chat_files", "chat_files"]
    assert {x["filename"] for x in first} == {"간이세액_2024.xlsx", "간이세액_2026.xlsx"}
    assert all(x["source"] == "drive" for x in res["files"][2:])
    assert "남의파일.csv" not in [x["filename"] for x in res["files"]]
    for item in res["files"]:
        assert {"source", "file_id", "filename", "size", "created_at"} <= set(item)


@pytest.mark.asyncio
async def test_t2_filename_prefers_session_chat_files_over_drive(monkeypatch, tmp_path):
    csv_file = tmp_path / "tax.csv"
    csv_file.write_text("a,b\n1,2\n")
    drive_file = tmp_path / "unrelated.txt"
    drive_file.write_text("drive 내용")
    chat = [_chat_row(SESSION_A, "근로소득 간이세액표.csv", csv_file)]
    drive = [_drive_row("간이세액 옛파일.txt", drive_file)]
    ex = _setup(monkeypatch, chat, drive)

    res = await ex._read_uploaded_file({"filename": "간이세액"})

    assert res["status"] == "ok"
    assert res["filename"] == "근로소득 간이세액표.csv"
    assert "a,b" in res["content"]


@pytest.mark.asyncio
async def test_t2b_session_miss_falls_back_to_drive(monkeypatch, tmp_path):
    drive_file = tmp_path / "old.txt"
    drive_file.write_text("drive 내용")
    ex = _setup(monkeypatch, [], [_drive_row("old.txt", drive_file)])

    res = await ex._read_uploaded_file({"filename": "old"})

    assert res["status"] == "ok"
    assert res["content"] == "drive 내용"


@pytest.mark.asyncio
async def test_t3_other_session_attachment_is_not_readable(monkeypatch, tmp_path):
    f = tmp_path / "secret.csv"
    f.write_text("secret")
    other = _chat_row(SESSION_B, "secret.csv", f)
    ex = _setup(monkeypatch, [other], [])

    by_name = await ex._read_uploaded_file({"filename": "secret"})
    by_id = await ex._read_uploaded_file({"filename": str(other["id"])})

    assert by_name["status"] == "not_found"
    assert by_id["status"] == "not_found"


@pytest.mark.asyncio
async def test_t4_xlsx_is_read_as_sheet_csv(monkeypatch, tmp_path):
    xlsx = tmp_path / "간이세액.xlsx"
    _make_xlsx(xlsx)
    chat = [_chat_row(SESSION_A, "간이세액.xlsx", xlsx, mime="application/vnd.ms-excel", size=xlsx.stat().st_size)]
    ex = _setup(monkeypatch, chat, [])

    res = await ex._read_uploaded_file({"filename": "간이세액"})

    assert res["status"] == "ok"
    assert "## Sheet: 2024" in res["content"]
    assert "월급여,공제대상,세액" in res["content"]
    assert "1060,1," in res["content"]
    assert "## Sheet: 2026" in res["content"]
    assert res["truncated"] is False


@pytest.mark.asyncio
async def test_t4b_xlsx_respects_max_chars(monkeypatch, tmp_path):
    xlsx = tmp_path / "a.xlsx"
    _make_xlsx(xlsx)
    ex = _setup(monkeypatch, [_chat_row(SESSION_A, "a.xlsx", xlsx)], [])

    res = await ex._read_uploaded_file({"filename": "a.xlsx", "max_chars": 20})

    assert res["status"] == "ok"
    assert len(res["content"]) <= 20
    assert res["truncated"] is True


@pytest.mark.asyncio
async def test_t4c_xlsx_parse_failure_reports_reason_and_size(monkeypatch, tmp_path):
    bad = tmp_path / "broken.xlsx"
    bad.write_bytes(b"not a zip file")
    ex = _setup(monkeypatch, [_chat_row(SESSION_A, "broken.xlsx", bad, size=14)], [])

    res = await ex._read_uploaded_file({"filename": "broken"})

    assert res["status"] == "read_error"
    assert res["file_size"] == 14
    assert res["error"]


@pytest.mark.asyncio
async def test_t5_missing_storage_path_is_file_missing(monkeypatch, tmp_path):
    ex = _setup(monkeypatch, [_chat_row(SESSION_A, "gone.xlsx", tmp_path / "없음.xlsx")], [])

    res = await ex._read_uploaded_file({"filename": "gone"})

    assert res["status"] == "file_missing"
    assert "원본이 서버에 없음" in res["message"]
    assert res["status"] != "not_found"


@pytest.mark.asyncio
async def test_t6_no_session_keeps_drive_listing(monkeypatch, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    chat = [_chat_row(SESSION_A, "첨부.xlsx", f)]
    drive = [_drive_row("a.txt", f), _drive_row("b.txt", f, minutes=1)]
    ex = _setup(monkeypatch, chat, drive, session_id="")

    res = await ex._read_uploaded_file({"filename": ""})

    assert res["status"] == "list"
    assert res["total"] == 2
    assert all(x["source"] == "drive" for x in res["files"])


@pytest.mark.asyncio
async def test_t6b_no_session_filename_still_uses_global_chat_files_fallback(monkeypatch, tmp_path):
    f = tmp_path / "paste.csv"
    f.write_text("x,y\n")
    ex = _setup(monkeypatch, [_chat_row(SESSION_A, "paste.csv", f)], [], session_id="")

    res = await ex._read_uploaded_file({"filename": "paste"})

    assert res["status"] == "ok"
    assert "x,y" in res["content"]


@pytest.mark.asyncio
async def test_drive_file_id_lookup_respects_workspace_filter(monkeypatch, tmp_path):
    f = tmp_path / "ws_b.txt"
    f.write_text("B 워크스페이스 비밀")
    other_ws = _drive_row("ws_b.txt", f, ws=WS_B)
    ex = _setup(monkeypatch, [], [other_ws])

    denied = await ex._read_uploaded_file({"filename": str(other_ws["id"]), "workspace_id": str(WS_A)})
    allowed = await ex._read_uploaded_file({"filename": str(other_ws["id"]), "workspace_id": str(WS_B)})

    assert denied["status"] == "not_found"
    assert allowed["status"] == "ok"
    assert allowed["content"] == "B 워크스페이스 비밀"
