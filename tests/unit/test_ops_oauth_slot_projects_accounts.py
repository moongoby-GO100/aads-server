import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.ops import _anthropic_slot_from_key_name, get_oauth_slot_projects

ACTIVE_FIELDS = (
    "provider", "account", "slot", "label", "key_name", "priority",
    "last_resort", "rate_limited",
)


def _run(anthropic_records, anthropic_rows, codex_rows):
    async def fetch(sql, *args):
        if "chat_workspaces" in sql:
            return []
        if "provider = 'codex'" in sql:
            return codex_rows
        if "provider = 'anthropic'" in sql:
            return anthropic_rows
        raise AssertionError(sql)

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=fetch)
    with patch("app.core.db_pool.get_pool", return_value=pool), patch(
        "app.core.auth_provider.get_oauth_key_records_async",
        AsyncMock(return_value=anthropic_records),
    ), patch("app.services.slot_projects.slot_project_map", AsyncMock(return_value={})):
        return asyncio.run(get_oauth_slot_projects())


RECORDS = [
    {"slot": "1", "label": "메인", "key_name": "ANTHROPIC_AUTH_TOKEN", "priority": 1},
    {"slot": "2", "label": "두번째", "key_name": "ANTHROPIC_AUTH_TOKEN_2", "priority": 2,
     "rate_limited_until": datetime.now(timezone.utc) + timedelta(hours=1)},
    {"slot": "3", "label": "막판", "key_name": "ANTHROPIC_AUTH_TOKEN_3", "priority": 3},
]
DB_ROWS = [
    {"key_name": "ANTHROPIC_AUTH_TOKEN", "label": "메인", "priority": 1},
    {"key_name": "ANTHROPIC_AUTH_TOKEN_2", "label": "두번째", "priority": 2},
    {"key_name": "ANTHROPIC_AUTH_TOKEN_3", "label": "막판", "priority": 3},
    {"key_name": "ANTHROPIC_AUTH_TOKEN_4", "label": "라일론", "priority": 4},
]
CODEX = [{"key_name": "MAIN", "label": "MAIN", "priority": 1, "rate_limited_until": None}]


def test_inactive_slot_is_added_with_needs_login():
    out = _run(RECORDS, DB_ROWS, CODEX)
    row = next(a for a in out["accounts"] if a["key_name"] == "ANTHROPIC_AUTH_TOKEN_4")
    assert row["provider"] == "anthropic"
    assert row["account"] == row["slot"] == "4"
    assert row["label"] == "라일론"
    assert row["priority"] == 4
    assert row["last_resort"] is False
    assert row["rate_limited"] is False
    assert row["is_active"] is False
    assert row["needs_login"] is True


def test_active_rows_keep_existing_fields():
    out = _run(RECORDS, DB_ROWS, CODEX)
    by_key = {a["key_name"]: a for a in out["accounts"]}
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN_2", "ANTHROPIC_AUTH_TOKEN_3", "MAIN"):
        row = by_key[key]
        for field in ACTIVE_FIELDS:
            assert field in row
        assert row["is_active"] is True
        assert row["needs_login"] is False
    assert by_key["ANTHROPIC_AUTH_TOKEN_3"]["last_resort"] is True
    assert by_key["ANTHROPIC_AUTH_TOKEN_2"]["rate_limited"] is True
    assert by_key["ANTHROPIC_AUTH_TOKEN_2"]["slot"] == "2"


def test_no_duplicate_key_names_and_inactive_sorted_last():
    out = _run(RECORDS, DB_ROWS, CODEX)
    names = [a["key_name"] for a in out["accounts"]]
    assert len(names) == len(set(names))
    assert len(names) == 5
    flags = [a["is_active"] for a in out["accounts"] if a["provider"] == "anthropic"]
    assert flags == sorted(flags, reverse=True)


def test_unparseable_key_name_is_skipped():
    rows = DB_ROWS + [
        {"key_name": "ANTHROPIC_AUTH_TOKEN_BACKUP", "label": "x", "priority": 9},
        {"key_name": "ANTHROPIC_AUTH_TOKEN_", "label": "y", "priority": 9},
    ]
    out = _run(RECORDS, rows, CODEX)
    names = {a["key_name"] for a in out["accounts"]}
    assert "ANTHROPIC_AUTH_TOKEN_BACKUP" not in names
    assert "ANTHROPIC_AUTH_TOKEN_" not in names


def test_slot_from_key_name():
    assert _anthropic_slot_from_key_name("ANTHROPIC_AUTH_TOKEN") == "1"
    assert _anthropic_slot_from_key_name("ANTHROPIC_AUTH_TOKEN_4") == "4"
    assert _anthropic_slot_from_key_name("OTHER") == ""
