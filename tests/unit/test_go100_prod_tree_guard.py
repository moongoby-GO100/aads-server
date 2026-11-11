import asyncio
import json
import logging

import pytest

from app.services import go100_prod_tree_guard as g


def _w(project, path, **kw):
    return g.check("write_remote_file", {"project": project, "file_path": path, "content": "x", **kw})


@pytest.mark.parametrize("path", [
    "frontend/src/app/page.tsx", "backend/app/main.py",
    "/root/kis-autotrade-v4/backend/app/x.py",
    "/opt/go100/frontend-releases/r1/a.tsx", "/opt/go100/backend-slots/blue/a.py",
    "artifacts/../backend/app/x.py", "/root/kis-autotrade-v4//backend///x.py",
    "./frontend/a.tsx", "logs/../../kis-autotrade-v4/backend/x.py",
    # R3: 게이트 경로 계산 == 쓰기 경로 계산 (strip 우회 방지)
    " /../backend/app/main.py", "backend/app/../app/main.py",
    "/opt/go100/frontend-releases/20261111/x.py", "frontend/src/x.tsx ",
    "\tbackend/app/x.py", "backend\\app\\x.py", "backend/app/x.py\n",
    "   ",
])
def test_blocks_prod_code(path):
    out = json.loads(_w("GO100", path))
    assert out["error"] == "go100_prod_tree_write_blocked" and out["blocked"]
    assert json.loads(g.check("patch_remote_file", {"project": "GO100", "file_path": path}))["blocked"]


@pytest.mark.parametrize("path", ["artifacts/a.json", "logs/x.log", "tmp/a", ".cache/a", "runtime/s", "/root/kis-autotrade-v4/logs/a.log"])
def test_allows_artifacts(path):
    assert _w("GO100", path) is None


@pytest.mark.parametrize("project", ["AADS", "SF", "NTV2"])
def test_other_projects_pass(project):
    assert _w(project, "backend/app/main.py") is None


def test_other_tool_passes():
    assert g.check("read_remote_file", {"project": "GO100", "file_path": "backend/app/x.py"}) is None


def test_gate_path_equals_write_path():
    """게이트가 보는 경로는 쓰기 함수의 normpath(pjoin(workdir, file_path)) 와 같다."""
    from posixpath import join, normpath
    for raw in (" /../backend/app/main.py", "a/../b", "/abs/x", "./c"):
        assert g._norm_abs(raw) == normpath(join(g.PROD_ROOT, raw))


def test_ticket(monkeypatch, caplog):
    monkeypatch.setenv("GO100_PROD_WRITE_TICKETS", "T1, T2-SECRET-XYZ")
    with caplog.at_level(logging.WARNING):
        assert _w("GO100", "backend/app/x.py", prod_write_ticket="T2-SECRET-XYZ") is None
    assert "go100_prod_tree_write_ticket_used" in caplog.text
    # 티켓 원문은 로그에 없고 sha256 앞 8자리만 있다.
    import hashlib
    for rec in caplog.records:
        assert "T2-SECRET-XYZ" not in rec.getMessage()
        assert "T2-SECRET-XYZ" not in str(rec.args)
    assert hashlib.sha256(b"T2-SECRET-XYZ").hexdigest()[:8] in caplog.text
    assert json.loads(_w("GO100", "backend/app/x.py", prod_write_ticket="BAD"))["blocked"]


def test_ticket_default_empty(monkeypatch):
    monkeypatch.delenv("GO100_PROD_WRITE_TICKETS", raising=False)
    assert json.loads(_w("GO100", "backend/app/x.py", prod_write_ticket=""))["blocked"]


def test_error_fails_open():
    assert g.check("write_remote_file", None) is None


# ── 채팅 경로(ceo_chat_tools.execute_tool) 및 ToolExecutor 경로 ──
def _spy_ssh(monkeypatch):
    """실제 SSH 쓰기/읽기 함수를 spy 로 바꾸고 목업 게이트를 통과시킨다."""
    from app.api import ceo_chat_tools as ct
    calls = []

    async def spy(*a, **k):
        calls.append((a, k))
        return "OK"

    async def gate(*a, **k):
        return None, ""

    monkeypatch.setattr(ct, "_write_remote_file_impl", spy)
    monkeypatch.setattr(ct, "_read_raw_file", spy)
    monkeypatch.setattr(ct, "_mockup_chat_gate", gate)
    return ct, calls


_PROBES = [" /../frontend/README_guard_probe.md", "frontend/README_guard_probe.md"]


@pytest.mark.parametrize("path", _PROBES)
@pytest.mark.parametrize("tool", ["write_remote_file", "patch_remote_file"])
def test_chat_path_blocked(monkeypatch, tool, path):
    ct, calls = _spy_ssh(monkeypatch)
    params = {"project": "GO100", "file_path": path, "content": "x",
              "old_string": "a", "new_string": "b"}
    out = asyncio.run(ct.execute_tool(tool, params, "postgresql://x"))
    assert json.loads(out)["error"] == "go100_prod_tree_write_blocked"
    assert calls == []


def test_chat_path_ticket_passes_gate(monkeypatch):
    monkeypatch.setenv("GO100_PROD_WRITE_TICKETS", "T9")
    ct, calls = _spy_ssh(monkeypatch)
    asyncio.run(ct.execute_tool("write_remote_file", {
        "project": "GO100", "file_path": "backend/app/probe.py", "content": "x",
        "prod_write_ticket": "T9"}, "postgresql://x"))
    assert len(calls) == 1


@pytest.mark.parametrize("project", ["KIS", "kis", "자동매매"])
@pytest.mark.parametrize("tool", ["write_remote_file", "patch_remote_file"])
def test_chat_path_kis_alias_blocked(monkeypatch, tool, project):
    ct, calls = _spy_ssh(monkeypatch)
    params = {"project": project, "file_path": "backend/app/main.py", "content": "x",
              "old_string": "a", "new_string": "b"}
    out = asyncio.run(ct.execute_tool(tool, params, "postgresql://x"))
    assert json.loads(out)["error"] == "go100_prod_tree_write_blocked"
    assert calls == []


def test_chat_path_other_project_not_blocked(monkeypatch):
    ct, calls = _spy_ssh(monkeypatch)
    asyncio.run(ct.execute_tool("write_remote_file", {
        "project": "AADS", "file_path": "backend/app/probe.py", "content": "x"}, "postgresql://x"))
    assert len(calls) == 1


def _isolate_executor_gates(monkeypatch):
    """GO100 게이트 앞단(계획모드·실매매·방향·중복·테넌트)을 통과시켜 GO100 게이트까지 도달시킨다."""
    from app.services import direction_guard, live_trading_guard, plan_mode_guard
    from app.services import tool_executor as te

    async def none_async(*a, **k):
        return None

    async def empty_tenant(*a, **k):
        return ""

    monkeypatch.setattr(plan_mode_guard, "check", lambda *a, **k: None)
    monkeypatch.setattr(live_trading_guard, "check", none_async)
    monkeypatch.setattr(direction_guard, "check", none_async)
    monkeypatch.setattr(te, "_sideeffect_duplicate", lambda *a, **k: None)
    monkeypatch.setattr(te, "resolve_bound_tenant_id", empty_tenant)
    reached = []
    real = g.check

    def spy_check(*a, **k):
        reached.append(a[0])
        return real(*a, **k)

    monkeypatch.setattr(g, "check", spy_check)
    return reached


@pytest.mark.parametrize("path", _PROBES)
@pytest.mark.parametrize("tool", ["write_remote_file", "patch_remote_file"])
def test_tool_executor_path_blocked(monkeypatch, tool, path):
    from app.services.tool_executor import ToolExecutor
    ct, calls = _spy_ssh(monkeypatch)
    reached = _isolate_executor_gates(monkeypatch)
    out = asyncio.run(ToolExecutor().execute(tool, {
        "project": "GO100", "file_path": path, "content": "x",
        "old_string": "a", "new_string": "b"}))
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    assert reached == [tool]  # GO100 게이트까지 도달했다
    assert "go100_prod_tree_write_blocked" in text
    assert calls == []


@pytest.mark.parametrize("project", ["KIS", "kis", "자동매매"])
@pytest.mark.parametrize("tool", ["write_remote_file", "patch_remote_file"])
def test_tool_executor_kis_alias_blocked(monkeypatch, tool, project):
    from app.services.tool_executor import ToolExecutor
    ct, calls = _spy_ssh(monkeypatch)
    reached = _isolate_executor_gates(monkeypatch)
    out = asyncio.run(ToolExecutor().execute(tool, {
        "project": project, "file_path": "backend/app/main.py", "content": "x",
        "old_string": "a", "new_string": "b"}))
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    assert "go100_prod_tree_write_blocked" in text
    assert calls == []


@pytest.mark.parametrize("project", ["KIS", "kis", "자동매매", "kis-autotrade", "GO100"])
def test_kis_alias_blocks_code(project, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        for tool in ("write_remote_file", "patch_remote_file"):
            out = json.loads(g.check(tool, {"project": project, "file_path": "backend/app/main.py"}))
            assert out["error"] == "go100_prod_tree_write_blocked"
            assert "GO100·KIS 공용 운영 폴더" in out["message"]
    assert any(f"project={project}" in r.getMessage() for r in caplog.records)


def test_kis_suspicious_path_blocked():
    assert json.loads(_w("KIS", " /../backend/x.py"))["blocked"]


def test_kis_artifacts_passes():
    assert _w("KIS", "artifacts/x.json") is None


def test_kis_ticket_passes_without_raw_in_log(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("GO100_PROD_WRITE_TICKETS", "SECRET-T1")
    with caplog.at_level(logging.WARNING):
        assert _w("KIS", "backend/app/main.py", prod_write_ticket="SECRET-T1") is None
    assert "SECRET-T1" not in caplog.text
    assert "ticket_sha8=" in caplog.text


def test_targets_prod_root_fallback_on_resolver_failure(monkeypatch):
    import app.core.project_config as pc
    monkeypatch.setattr(pc, "resolve_project", lambda v: (_ for _ in ()).throw(RuntimeError("x")))
    assert g.targets_prod_root("KIS") and g.targets_prod_root("go100")
    assert not g.targets_prod_root("AADS")
