"""재개 턴에서 도구 호출이 집계에서 빠지던 경로.

2026-09-17 실측: llmops_tool_calls 의 latency NULL 24건이 전부
재개(resume)된 trace 4개에 몰려 있었다. 재개 턴은 부분 스냅샷을 이어받아
tool_use 없이 tool_result 만 들고 오는 경우가 있고, 그 결과를 통째로
버리고 있었다. 지연은 못 재도 호출 사실은 남고, NULL 사유가 적혀야 한다.
"""

from app.services.llmops_chat_hook import pair_tool_calls


def _use(tool, use_id, at):
    return {"type": "tool_use", "tool_name": tool, "tool_use_id": use_id, "at": at}


def _result(tool, use_id, at, **kw):
    return {"type": "tool_result", "tool_name": tool, "tool_use_id": use_id, "at": at, **kw}


def test_paired_call_gets_latency():
    rows = pair_tool_calls([_use("query_database", "u1", 100.0), _result("query_database", "u1", 101.5)])
    assert len(rows) == 1
    assert rows[0]["latency_ms"] == 1500


def test_orphan_result_is_recorded_not_dropped():
    """재개 턴의 tool_result 만 오는 경우 — 예전에는 통째로 버렸다."""
    rows = pair_tool_calls([_result("read_remote_file", "u9", 200.0)])
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "read_remote_file"
    assert rows[0]["latency_ms"] is None if "latency_ms" in rows[0] else True
    assert rows[0]["metadata"]["latency_missing"] == "orphan_tool_result"


def test_orphan_error_result_keeps_error():
    rows = pair_tool_calls([_result("run_remote_command", "u8", 5.0, is_error=True, error_type="timeout")])
    assert rows[0]["status"] == "error"
    assert "timeout" in rows[0]["error"]


def test_unpaired_use_says_why_latency_is_missing():
    """결과가 오기 전에 턴이 끊긴 경우 — NULL 사유가 남아야 한다."""
    rows = pair_tool_calls([_use("todo_write", "u2", 10.0)])
    assert len(rows) == 1
    assert "latency_ms" not in rows[0]
    assert rows[0]["metadata"]["latency_missing"] == "no_tool_result"


def test_empty_input_returns_none():
    assert pair_tool_calls([]) is None
    assert pair_tool_calls(None) is None


def test_mixed_batch_keeps_every_call():
    rows = pair_tool_calls([
        _use("a", "u1", 1.0),
        _result("a", "u1", 2.0),
        _use("b", "u2", 3.0),
        _result("c", "u3", 4.0),
    ])
    assert [r["tool_name"] for r in rows] == ["a", "b", "c"]
    assert rows[0]["latency_ms"] == 1000
    assert rows[1]["metadata"]["latency_missing"] == "no_tool_result"
    assert rows[2]["metadata"]["latency_missing"] == "orphan_tool_result"
