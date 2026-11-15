"""schedule_task report_when(조건부 보고) — 구조화 결과 판정·등록 검증·영속화."""
import asyncio
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api import ceo_chat_tools as ct
from app.api import ceo_chat_tools_scheduler as st

SID = "sess-1"
PATTERN = r"(?m)^(ALERT|STOP_REQUIRED|EMERGENCY|CRITICAL)\b"
MULTI_CMD = "echo ALERT_in_command\necho second ALERT line"


def _run(c):
    return asyncio.run(c)


class _FakeProc:
    def __init__(self, out, err="", rc=0):
        self._o, self._e, self.returncode = out.encode(), err.encode(), rc

    async def communicate(self):
        return self._o, self._e

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture
def ssh(monkeypatch):
    """실제 tool_run_remote_command 경로를 타되 ssh subprocess 만 가짜로 대체."""
    seen = {}

    def setup(out, err="", rc=0):
        async def fake_exec(*args, **kw):
            seen["args"] = args
            return _FakeProc(out, err, rc)
        monkeypatch.setattr(ct.asyncio, "create_subprocess_exec", fake_exec)
        return seen
    return setup


@pytest.fixture
def reports(monkeypatch):
    post = AsyncMock()
    monkeypatch.setitem(sys.modules, "app.services.session_reporter",
                        types.SimpleNamespace(post_session_report=post))
    bot = MagicMock()
    bot.is_ready = True
    bot.send_message = AsyncMock()
    monkeypatch.setitem(sys.modules, "app.services.telegram_bot",
                        types.SimpleNamespace(get_telegram_bot=lambda: bot))
    return post, bot


def _exec(action, cfg):
    _run(st._execute_scheduled_job("user_x", action, {"report_session_id": SID, **cfg}))


def _count(reports):
    post, bot = reports
    return post.await_count, bot.send_message.await_count


def test_unset_reports_as_before(ssh, reports):
    ssh("status ok")
    _exec("remote_command", {"project": "GO100", "command": "df -h"})
    assert _count(reports) == (1, 1)


def test_stdout_match_reports(ssh, reports):
    ssh("ok\nSTOP_REQUIRED free=3GiB")
    _exec("remote_command", {"project": "GO100", "command": "df -h", "report_when": PATTERN})
    assert _count(reports) == (1, 1)


def test_stdout_no_match_suppresses_both(ssh, reports):
    ssh("status ok free=50GiB\nwatch 70%")
    _exec("remote_command", {"project": "GO100", "command": "df -h", "report_when": PATTERN})
    assert _count(reports) == (0, 0)


def test_multiline_command_text_not_judged_but_stdout_is(ssh, reports):
    ssh("a\nb")
    cfg = {"project": "GO100", "command": MULTI_CMD, "report_when": r"ALERT"}
    _exec("remote_command", cfg)
    assert _count(reports) == (0, 0)  # 에코에 ALERT 가 있어도 판정에 쓰이지 않는다
    ssh("a\nALERT disk")
    _exec("remote_command", cfg)
    assert _count(reports) == (1, 1)


def test_compose_rewritten_command_not_judged_but_stdout_is(ssh, reports, monkeypatch):
    monkeypatch.setattr(ct, "get_workdir", lambda p: "/root/aads/aads-server", raising=False)
    cmd = "docker compose ps ALERT"
    ssh("svc up")
    res = _run(ct.run_remote_command_structured("AADS", cmd))
    # 재작성(shlex.join)된 한 줄이 에코되지만 stdout 에는 들어 있지 않다
    assert res["text"].split("\n")[1] != f"$ {cmd}" and "ALERT" in res["text"]
    assert res["stdout"] == "svc up"
    cfg = {"project": "AADS", "command": cmd, "report_when": r"(?m)^ALERT"}
    _exec("remote_command", cfg)
    assert _count(reports) == (0, 0)
    ssh("ALERT service down")
    _exec("remote_command", cfg)
    assert _count(reports) == (1, 1)


def test_nonzero_exit_reports_even_without_match(ssh, reports):
    ssh("boom", err="x", rc=2)
    _exec("remote_command", {"project": "GO100", "command": "false", "report_when": PATTERN})
    assert _count(reports) == (1, 1)


def test_remote_tool_error_reports(reports):
    _exec("remote_command", {"project": "NOPE", "command": "ls", "report_when": PATTERN})
    assert _count(reports) == (1, 1)


@pytest.mark.parametrize("err", [
    {"error": "x"},
    {"error": ""},
    {"error": None},
    '{"data": [], "error": null}',
    "{'error': 'relation does not exist'}",
    '{"error": "health failed"}',
    "[BLOCKED] no",
])
def test_non_remote_error_results_report(monkeypatch, reports, err):
    async def q(project, query, limit=10):
        return err
    monkeypatch.setattr("app.api.ceo_chat_tools_db.query_project_database", q)
    _exec("db_query", {"project": "GO100", "query": "select 1", "report_when": PATTERN})
    assert _count(reports) == (1, 1)


def test_db_query_judged_on_whole_result(monkeypatch, reports):
    async def q(project, query, limit=10):
        return "ALERT x"
    monkeypatch.setattr("app.api.ceo_chat_tools_db.query_project_database", q)
    _exec("db_query", {"project": "GO100", "query": "select 1", "report_when": PATTERN})
    assert _count(reports) == (1, 1)
    async def q2(project, query, limit=10):
        return "fine"
    monkeypatch.setattr("app.api.ceo_chat_tools_db.query_project_database", q2)
    _exec("db_query", {"project": "GO100", "query": "select 1", "report_when": PATTERN})
    assert _count(reports) == (1, 1)  # 이전 호출분 그대로 — 억제됨


def test_exception_path_reports(monkeypatch, reports):
    monkeypatch.setattr("app.api.ceo_chat_tools.run_remote_command_structured",
                        AsyncMock(side_effect=RuntimeError("x")))
    _exec("remote_command", {"project": "GO100", "command": "a", "report_when": PATTERN})
    assert _count(reports)[0] == 1


def test_oversize_target_fail_open(ssh, reports):
    ssh("x" * (st._REPORT_WHEN_MAX_TARGET + 1))
    _exec("remote_command", {"project": "GO100", "command": "a", "report_when": "ALERT"})
    assert _count(reports) == (1, 1)


@pytest.mark.parametrize("backtracking_pattern", [
    r"(a+){30}$",
    r"((a+))+$",
    r"(a|aa)+$",
])
def test_real_backtracking_fail_open_within_2sec(monkeypatch, ssh, reports, backtracking_pattern):
    """실제 CPU 백트래킹으로 검증: (a+){30}$, ((a+))+$, (a|aa)+$ 를 report_when 으로
    등록 가능하되, 판정 시 대상 'a'*5000+'!' 에서 2초 이내 반환되고 결과가 '보고함(fail-open)'
    이며 보고 본문에 'report_when 판정 시간 초과/오류' 1줄이 표기됨을 assert.
    """
    ssh("a" * 5000 + "!")
    t0 = time.monotonic()
    _exec("remote_command", {"project": "GO100", "command": "a", "report_when": backtracking_pattern})
    dt = time.monotonic() - t0

    assert dt < 2.0
    assert _count(reports) == (1, 1)
    post, _ = reports
    body = post.call_args.kwargs.get("body", "")
    assert "report_when 판정 시간 초과/오류" in body


def test_event_loop_runs_during_backtracking():
    """판정 중에도 이벤트 루프가 돈다:
    판정과 동시에 asyncio.sleep(0.05) 루프 카운터를 돌려 판정 0.5초 동안
    카운터가 ≥5 증가함을 assert(concurrent=True 의 GIL release 검증).
    """
    async def _check():
        counter = 0
        stopped = False

        async def ticker():
            nonlocal counter
            while not stopped:
                await asyncio.sleep(0.05)
                counter += 1

        ticker_task = asyncio.create_task(ticker())
        t0 = time.monotonic()
        ok = await st._should_report("j", r"(a+){30}$", "dummy_result", "a" * 5000 + "!")
        dt = time.monotonic() - t0
        stopped = True
        await ticker_task

        return ok, dt, counter

    ok, dt, counter = _run(_check())
    assert ok is True  # fail-open
    assert dt < 2.0
    assert counter >= 5  # 0.5초 동안 0.05초 슬립이 최소 5회 이상 실행됨 (GIL 미점유 검증)


@pytest.mark.parametrize("bad", [
    "(unclosed", "[unclosed", "*invalid", "a" * 201, 123,
])
def test_invalid_syntax_or_length_rejected(bad):
    sched = MagicMock()
    sched.get_job.return_value = None
    r = _run(st.schedule_task_on(sched, "n", "interval", "url_check",
                                 {"url": "http://x"}, {"minutes": 5}, report_when=bad))
    assert "error" in r and not sched.add_job.called


@pytest.mark.parametrize("backtracking_pattern", [
    r"(a+){30}$",
    r"((a+))+$",
    r"(a|aa)+$",
    r"(((a*)))*b",
])
def test_backtracking_patterns_accepted_at_registration(backtracking_pattern):
    """휴리스틱 정적 차단 대신 regex timeout 으로 런타임 안전성을 보장하므로 등록은 정상 허용."""
    assert st.validate_report_when(backtracking_pattern) == ""


def test_recommended_pattern_accepted():
    assert st.validate_report_when(PATTERN) == ""


def test_persisted_config_keeps_report_when(monkeypatch, ssh, reports):
    calls = []

    class S:
        def get_job(self, *a, **k):
            return None

        def add_job(self, func, *t, **kw):
            calls.append(kw)

    monkeypatch.setattr(st, "_raw_add_job", lambda s: s.add_job)
    r = _run(st.schedule_task_on(S(), "n", "interval", "remote_command",
                                 {"project": "GO100", "command": "df"}, {"minutes": 5},
                                 report_session_id=SID, report_when=PATTERN))
    assert r["status"] == "registered" and r["report_when"] == PATTERN
    saved = dict(calls[0]["args"][2])
    assert saved["report_when"] == PATTERN
    ssh("status ok")
    _exec("remote_command", saved)
    assert _count(reports) == (0, 0)
    ssh("ALERT disk")
    _exec("remote_command", saved)
    assert _count(reports) == (1, 1)


def test_tool_run_remote_command_string_unchanged(ssh):
    ssh("line1\n", err="warn", rc=0)
    out = _run(ct.tool_run_remote_command("GO100", "  echo hi\necho there  "))
    assert out == "[GO100 명령 실행 — exit=0]\n$ echo hi\necho there\nline1"
    ssh("o", err="bad", rc=3)
    out = _run(ct.tool_run_remote_command("GO100", "false"))
    assert out == "[GO100 명령 실행 — exit=3]\n$ false\no\n[STDERR] bad"
    res = _run(ct.run_remote_command_structured("GO100", "false"))
    assert res == {"exit_code": 3, "stdout": "o", "stderr": "bad", "text": out}
    assert _run(ct.tool_run_remote_command("GO100", " ")) == "[ERROR] command 필수"


def test_go100_sample_output_discrimination(ssh, reports):
    """권장 패턴 (?m)^(ALERT|STOP_REQUIRED|EMERGENCY|CRITICAL)\\b 가
    GO100 실제 출력 샘플을 올바르게 가르는지(정상 → 비매치) 검증.
    """
    cmd = "timeout 50 bash -c '... echo \"ALERT ...\" ... STOP_REQUIRED ...'"
    go100_stdout_ok = (
        "2026-11-15 10:40:01 KST\n"
        "redis_bgsave_in_progress=0 (1이면 약 412MB 일시 감소분 포함)\n"
        "avail_mb=25001\n"
        "ok"
    )
    ssh(go100_stdout_ok)
    _exec("remote_command", {"project": "GO100", "command": cmd, "report_when": PATTERN})
    assert _count(reports) == (0, 0)  # stdout 에 'ok' 뿐이므로 비매치 → 억제됨

    go100_stdout_watch = (
        "2026-11-15 10:40:01 KST\n"
        "redis_bgsave_in_progress=0 (1이면 약 412MB 일시 감소분 포함)\n"
        "avail_mb=12000\n"
        "watch"
    )
    ssh(go100_stdout_watch)
    _exec("remote_command", {"project": "GO100", "command": cmd, "report_when": PATTERN})
    assert _count(reports) == (0, 0)  # stdout 에 'watch' 뿐이므로 비매치 → 억제됨

    go100_stdout_alert = (
        "2026-11-15 10:40:01 KST\n"
        "ALERT <8GiB avail_mb=7000\n"
    )
    ssh(go100_stdout_alert)
    _exec("remote_command", {"project": "GO100", "command": cmd, "report_when": PATTERN})
    assert _count(reports) == (1, 1)  # 줄 시작 ALERT 매치 → 보고됨


def test_regex_is_pinned_in_runtime_lock():
    """requirements.runtime.lock 에 ^regex== 줄이 존재함을 검증."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    lock = (root / "requirements.runtime.lock").read_text(encoding="utf-8").splitlines()
    assert any(re.match(r"^regex==", line) for line in lock)

