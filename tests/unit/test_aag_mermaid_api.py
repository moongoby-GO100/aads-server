from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import aag as aag_api
from app.services.aag_governance import AAGAuthorizationError
from app.services.aag_mermaid import render_snapshot_mermaid

NODES = [
    {"id": "app/main.py", "kind": "entrypoint"},
    {"id": "app/api/a.py", "kind": "router_module"},
    {"id": "app/api/b.py", "kind": "router_module"},
    {"id": "ns:/api/v1/a", "kind": "namespace"},
    {"id": "ns:/api/v1/b", "kind": "namespace"},
]
EDGES = [
    {"from": "app/main.py", "to": "app/api/a.py", "kind": "mounts"},
    {"from": "app/main.py", "to": "app/api/b.py", "kind": "mounts"},
    {"from": "app/api/a.py", "to": "ns:/api/v1/a", "kind": "owns"},
    {"from": "app/api/a.py", "to": "ns:/api/v1/b", "kind": "owns"},
    {"from": "app/api/b.py", "to": "ns:/api/v1/b", "kind": "owns"},
    {"from": "src/p.tsx", "to": "ns:/api/v1/a", "kind": "calls", "path": "/api/v1/a"},
    {"from": "src/p.tsx", "to": "ns:/api/v1/a", "kind": "calls", "path": "/api/v1/a"},
    {"from": "x.py", "to": "app/api/a.py", "kind": "imports"},
]
FINDINGS = [{"rule": "DOUBLE_MOUNT", "namespace": "/api/v1/b"}]

EXPECTED = "\n".join([
    "graph LR",
    "  classDef dbl fill:#ffd7d7,stroke:#c00,stroke-width:2px;",
    '  n_app_main_py["app/main.py"]',
    '  n_ns_api_v1_b(["/api/v1/b ⚠2+"])',
    '  n_app_api_a_py["app/api/a.py"]',
    "  n_app_api_a_py --> n_ns_api_v1_b",
    '  n_app_api_b_py["app/api/b.py"]',
    "  n_app_api_b_py --> n_ns_api_v1_b",
    "  class n_ns_api_v1_b dbl;",
    '  n_ns_api_v1_a(["/api/v1/a"])',
    "  n_app_api_a_py --> n_ns_api_v1_a",
    "  n_app_main_py --> n_app_api_a_py",
    "  n_app_main_py --> n_app_api_b_py",
    '  n_src_p_tsx[/"src/p.tsx"/]',
    "  n_src_p_tsx -. 2건 .-> n_ns_api_v1_a",
])


def test_small_fixture_matches_expected_text():
    out = render_snapshot_mermaid(NODES, EDGES, FINDINGS)
    assert out["mermaid"] == EXPECTED
    assert out["shown_namespaces"] == 2
    assert out["total_namespaces"] == 2
    assert out["truncated"] is False


def test_namespace_limit_and_char_cap():
    out = render_snapshot_mermaid(NODES, EDGES, FINDINGS, max_namespaces=1)
    assert out["shown_namespaces"] == 1
    assert out["total_namespaces"] == 2
    assert out["mermaid"].endswith("%% 네임스페이스 2개 중 상위 1개만 표시")
    assert "n_ns_api_v1_a" not in out["mermaid"]

    big_edges = [
        {"from": f"m{i}.py", "to": f"ns:/n{i}", "kind": "owns"} for i in range(500)
    ]
    capped = render_snapshot_mermaid([], big_edges, max_namespaces=80, max_chars=600)
    assert capped["shown_namespaces"] == 80
    assert capped["total_namespaces"] == 500
    assert capped["truncated"] is True
    assert len(capped["mermaid"]) <= 600
    assert capped["mermaid"].splitlines()[-1].startswith("%% 출력 600자 상한")

    default_cap = render_snapshot_mermaid([], big_edges, max_namespaces=80)
    assert default_cap["truncated"] is False


def test_deterministic_regardless_of_input_order():
    a = render_snapshot_mermaid(NODES, EDGES, FINDINGS)
    b = render_snapshot_mermaid(list(reversed(NODES)), list(reversed(EDGES)), FINDINGS)
    assert a["mermaid"] == b["mermaid"]
    assert render_snapshot_mermaid(NODES, EDGES, FINDINGS) == a


def _pool(row, calls=None):
    class Connection:
        async def fetchrow(self, statement, *args):
            if calls is not None:
                calls.append(args)
            return row

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    return lambda: Pool()


def _row():
    return {
        "snapshot_id": uuid4(),
        "resolved_commit_sha": "1" * 40,
        "generated_at": datetime(2026, 10, 22, tzinfo=UTC),
        "nodes": NODES,
        "edges": EDGES,
        "findings": FINDINGS,
        "node_count": len(NODES),
        "edge_count": len(EDGES),
    }


async def _allow(*_args, **_kwargs):
    return None


async def _call(**overrides):
    kwargs = dict(
        project="aads", repository_id="aads-server", target_ref="refs/heads/main",
        user={"user_id": "viewer"}, governance_scope="default", max_namespaces=40,
    )
    kwargs.update(overrides)
    return await aag_api.get_mermaid_v2(**kwargs)


@pytest.mark.asyncio
async def test_endpoint_response_shape_and_determinism(monkeypatch):
    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    monkeypatch.setattr(aag_api, "get_pool", lambda: _pool(_row())())
    monkeypatch.setattr(aag_api, "require_project_role", _allow)
    first = await _call()
    second = await _call()
    assert first["mermaid"] == second["mermaid"] == EXPECTED
    assert first["project"] == "AADS"
    assert set(first) == {
        "project", "snapshot_id", "generated_at", "resolved_commit_sha", "node_count",
        "edge_count", "shown_namespaces", "total_namespaces", "truncated", "mermaid",
    }
    assert first["node_count"] == len(NODES)
    assert first["edge_count"] == len(EDGES)


@pytest.mark.asyncio
async def test_endpoint_rejects_unauthorized_project(monkeypatch):
    queried = []

    async def deny(*_args, **_kwargs):
        raise AAGAuthorizationError("no role on project")

    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    monkeypatch.setattr(aag_api, "get_pool", _pool(_row(), queried))
    monkeypatch.setattr(aag_api, "require_project_role", deny)
    with pytest.raises(HTTPException) as exc:
        await _call(project="KIS")
    assert exc.value.status_code == 403
    assert queried == [], "DB must not be read before the role check passes"


@pytest.mark.asyncio
async def test_endpoint_404_when_no_pointer(monkeypatch):
    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    monkeypatch.setattr(aag_api, "get_pool", _pool(None))
    monkeypatch.setattr(aag_api, "require_project_role", _allow)
    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == 404
    assert "AADS" in exc.value.detail


@pytest.mark.asyncio
async def test_endpoint_disabled_returns_404(monkeypatch):
    monkeypatch.setenv("AAG_V2_ENABLED", "0")
    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == 404
