"""문서현황 SF·NTV2 원격 열람 — 정규 서버 ID 를 ssh 별칭으로 해석하는지 검증한다.

SERVER_CONFIG 의 host 는 정규 ID(cafe24_114)지만 ssh 는 별칭(server-114)만 해석한다.
"""
from __future__ import annotations

import asyncio

import pytest

from app.api import project_docs


@pytest.mark.parametrize(
    "host,expected",
    [
        ("cafe24_114", "server-114"),
        ("contabo14", "contabo14"),
        ("not-registered-host", "not-registered-host"),
    ],
)
def test_ssh_target_resolves_canonical_id(host, expected):
    assert project_docs._ssh_target(host) == expected


@pytest.mark.parametrize("project", ["SF", "NTV2"])
def test_cafe24_projects_resolve_to_ssh_alias(project):
    assert project_docs.SERVER_CONFIG[project]["host"] == "cafe24_114"
    assert project_docs._ssh_target(project_docs.SERVER_CONFIG[project]["host"]) == "server-114"


def test_remote_calls_pass_ssh_alias(monkeypatch):
    calls: list[list[str]] = []

    async def fake_run_cmd(cmd, timeout=10):
        calls.append(cmd)
        return "exists"

    monkeypatch.setattr(project_docs, "_run_cmd", fake_run_cmd)

    assert asyncio.run(project_docs._remote_file_exists("cafe24_114", "/srv/newtalk-v2/docs/a.html")) is True
    asyncio.run(project_docs._scan_remote("cafe24_114", "/srv/newtalk-v2/docs"))

    assert len(calls) == 2
    for cmd in calls:
        assert cmd[0] == "ssh"
        assert "server-114" in cmd
        assert "cafe24_114" not in cmd
