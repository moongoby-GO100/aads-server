"""CLI wrapper 가 MCP 브릿지에 세션 id 를 넘겨야 한다.

2026-09-15, #310 주도 세션의 `ask_session` 이 5회 연속
`origin_session_missing` 으로 막혔다. 원인은 여기였다.

릴레이는 세션 id 를 MCP 서버 설정의 **`env`** 에 넣는다(template 모드).
그런데 wrapper 는 **`args`** 에서 `-e AADS_SESSION_ID=` 만 찾았고, 못 찾으면
빈 문자열로 `AADS_SESSION_ID='' python -m ...` 를 만들어 넣었다. 그 빈 값이
CLI 가 제대로 넘겨준 환경변수를 **덮어썼다.**

최근 요청 131건이 전부 이 경로(slot_credentials)였다.
"""
import json
import pathlib
import subprocess
import tempfile

import pytest

WRAPPERS = (
    "scripts/claude-docker-wrapper.sh",
    "scripts/claude-docker-wrapper-active.sh",
)
REPO = pathlib.Path(__file__).resolve().parents[2]


def _extract_rewriter(wrapper: str) -> str:
    src = (REPO / wrapper).read_text()
    marker = 'python3 - "$source_config" "$LOCAL_MCP_CONFIG" <<\'PY\'\n'
    assert marker in src, wrapper
    return src.split(marker, 1)[1].split("\nPY\n", 1)[0]


def _run(wrapper: str, config: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        script = tmp_path / "rewrite.py"
        script.write_text(_extract_rewriter(wrapper))
        src = tmp_path / "in.json"
        dst = tmp_path / "out.json"
        src.write_text(json.dumps(config))
        subprocess.run(
            ["python3", str(script), str(src), str(dst)], check=True, capture_output=True
        )
        return json.loads(dst.read_text())


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_session_id_from_env_survives(wrapper):
    """릴레이가 쓰는 실제 모양 — 세션은 env 에만 있다."""
    sid = "5090a247-47f7-4a05-a965-da89f844ad2f"
    out = _run(wrapper, {"mcpServers": {"aads-tools": {
        "command": "/root/aads/aads-server/scripts/mcp-active-bridge.sh",
        "args": [],
        "env": {"AADS_SESSION_ID": sid},
    }}})
    command_line = out["mcpServers"]["aads-tools"]["args"][1]
    assert sid in command_line


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_session_id_from_args_still_works(wrapper):
    """cfg 의 command 가 docker 일 때는 args 에 실린다. 그 경로도 유지한다."""
    sid = "11111111-2222-3333-4444-555555555555"
    out = _run(wrapper, {"mcpServers": {"aads-tools": {
        "command": "docker",
        "args": ["exec", "-i", "-e", "AADS_SESSION_ID=" + sid, "aads-server", "python"],
    }}})
    assert sid in out["mcpServers"]["aads-tools"]["args"][1]


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_never_clobbers_with_an_empty_value(wrapper):
    """세션을 못 찾았으면 물려받게 둔다 — 빈 값을 박으면 멀쩡한 것도 지운다."""
    out = _run(wrapper, {"mcpServers": {"aads-tools": {
        "command": "/root/aads/aads-server/scripts/mcp-active-bridge.sh",
        "args": [],
    }}})
    command_line = out["mcpServers"]["aads-tools"]["args"][1]
    assert "AADS_SESSION_ID=''" not in command_line
    assert command_line.strip().startswith("python -m mcp_servers.aads_tools_bridge")
