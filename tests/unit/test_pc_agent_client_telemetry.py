from __future__ import annotations

import json

from pc_agent import agent


def test_windows_e2e_role_is_loaded_from_agent_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"node_role": "windows_e2e"}), encoding="utf-8")
    monkeypatch.setattr(agent, "CONFIG_PATH", config_path)
    monkeypatch.delenv("AADS_PC_AGENT_NODE_ROLE", raising=False)

    assert agent._node_role() == "windows_e2e"
    assert "windows_e2e" in agent._collect_capabilities()


def test_agent_start_count_is_persistent(monkeypatch, tmp_path) -> None:
    count_path = tmp_path / ".agent_start_count"
    monkeypatch.setattr(agent, "AGENT_START_COUNT_FILE", count_path)

    assert agent._increment_agent_start_count() == 1
    assert agent._increment_agent_start_count() == 2
