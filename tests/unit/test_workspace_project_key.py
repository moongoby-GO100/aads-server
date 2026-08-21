"""Workspace project parser regression tests."""

import pytest

from app.services.chat_service import _workspace_project_key


@pytest.mark.parametrize("workspace,expected", [
    ("[GO100] 백억이 투자분석", "GO100"),
    ("KIS 자동매매", "KIS"),
    ("ShortFlow 운영", "SF"),
    ("NewTalk V2", "NTV2"),
    ("[FOOD] 열정국밥", "FOOD"),
    ("NAS 운영", "NAS"),
    ("[VIBE] 실험", "VIBE"),
    ("알 수 없는 워크스페이스", "CEO"),
])
def test_workspace_project_key_regression(workspace, expected):
    assert _workspace_project_key(workspace) == expected
