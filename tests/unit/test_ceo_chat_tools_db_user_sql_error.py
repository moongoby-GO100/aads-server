"""사용자 SQL 오류는 warning(USER_SQL_ERROR)으로, 인프라 오류는 error 로 기록한다."""
import logging

import pytest

from app.api import ceo_chat_tools_db as db


class _PgError(Exception):
    def __init__(self, msg, sqlstate=None):
        super().__init__(msg)
        self.sqlstate = sqlstate


class UndefinedTableError(_PgError):
    pass


@pytest.mark.parametrize(
    "exc",
    [
        Exception('relation "x" does not exist'),
        Exception('syntax error at or near "FROM"'),
        Exception("permission denied for table users"),
        Exception("Unknown column 'foo' in 'field list'"),
        Exception("Table 'db.t' doesn't exist"),
        Exception(1146, "Table 'db.t' doesn't exist"),
        Exception(1054, "Unknown column 'foo' in 'field list'"),
        Exception(1064, "You have an error in your SQL syntax"),
        Exception(1142, "SELECT command denied"),
        _PgError("whatever", sqlstate="42P01"),
        _PgError("whatever", sqlstate="42601"),
        UndefinedTableError('relation "x" does not exist'),
    ],
)
def test_user_sql_errors_detected(exc):
    assert db._is_user_sql_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError(111, "Connection refused"),
        Exception("connection refused"),
        TimeoutError("timed out waiting for connection from pool"),
        Exception("SSH tunnel failed: Connection timed out"),
        Exception(2003, "Can't connect to MySQL server"),
        Exception('database "kisautotrade" does not exist'),
        _PgError("too many connections", sqlstate="53300"),
        _PgError("connection lost", sqlstate="08006"),
        _PgError('database "x" does not exist', sqlstate="3D000"),
    ],
)
def test_infra_errors_not_user_sql_errors(exc):
    assert db._is_user_sql_error(exc) is False


async def _run_failing_query(monkeypatch, exc):
    async def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(db, "_query_postgresql", boom)
    return await db.query_project_database(
        "GO100", "SELECT * FROM go100_strategy_card_decisions"
    )


async def test_user_sql_error_logged_as_warning_with_token(monkeypatch, caplog):
    exc = UndefinedTableError('relation "go100_strategy_card_decisions" does not exist', "42P01")
    with caplog.at_level(logging.DEBUG, logger=db.logger.name):
        result = await _run_failing_query(monkeypatch, exc)

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "USER_SQL_ERROR" in warnings[0].getMessage()
    assert "FAIL" not in warnings[0].getMessage()
    assert result == {
        "error": 'DB 쿼리 실패 (GO100): relation "go100_strategy_card_decisions" does not exist'
    }


async def test_infra_error_still_logged_as_error(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG, logger=db.logger.name):
        result = await _run_failing_query(monkeypatch, ConnectionRefusedError("connection refused"))

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "query_project_database: FAIL" in errors[0].getMessage()
    assert "USER_SQL_ERROR" not in errors[0].getMessage()
    assert result == {"error": "DB 쿼리 실패 (GO100): connection refused"}
