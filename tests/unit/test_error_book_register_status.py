import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "error_book.py"
_spec = importlib.util.spec_from_file_location("error_book_cli", _SCRIPT)
error_book_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(error_book_cli)


def _args(cause="", **kw):
    base = dict(
        project="AADS", key="pc_agent.sample", symptom="증상", cause=cause,
        prevention="예방", signature=["sample"], fix_commit=[], fix_file=[], fix_note="",
    )
    base.update(kw)
    return Namespace(**base)


def _run_register(monkeypatch, capsys, args):
    sqls = []
    monkeypatch.setattr(error_book_cli, "psql", lambda sql: sqls.append(sql) or "")
    assert error_book_cli.do_register(args) == 0
    assert len(sqls) == 1
    return sqls[0], capsys.readouterr().out


@pytest.mark.parametrize("cause", ["", "   ", "\n\t "])
def test_register_without_cause_is_candidate(monkeypatch, capsys, cause):
    sql, out = _run_register(monkeypatch, capsys, _args(cause=cause))
    assert "'candidate'" in sql
    assert "'active'" not in sql
    assert "candidate" in out and "promote" in out


def test_register_with_cause_is_active(monkeypatch, capsys):
    sql, out = _run_register(monkeypatch, capsys, _args(cause="호스트/컨테이너 경로 불일치"))
    assert "'active'" in sql
    assert "'candidate'" not in sql
    assert "active" in out and "candidate" not in out


def test_register_status_helper():
    assert error_book_cli.register_status("") == "candidate"
    assert error_book_cli.register_status(None) == "candidate"
    assert error_book_cli.register_status("  ") == "candidate"
    assert error_book_cli.register_status("x") == "active"
