"""tool_registry 프로젝트 enum 단일 소스 회귀 테스트."""

import re
from pathlib import Path

from app.core.project_config import ALL_PROJECTS, SEARCH_PROJECT_ENUM, SSH_PROJECT_ENUM
from app.services.tool_registry import _TOOLS


def _project_enum(tool_name: str):
    return _TOOLS[tool_name]["input_schema"]["properties"]["project"]["enum"]


def test_project_tool_enums_match_expected_sets():
    assert _project_enum("read_remote_file") == SSH_PROJECT_ENUM
    assert _project_enum("run_remote_command") == SSH_PROJECT_ENUM
    assert _project_enum("query_project_database") == SSH_PROJECT_ENUM
    assert _project_enum("pipeline_runner_submit") == SSH_PROJECT_ENUM
    assert _project_enum("search_all_projects") == SEARCH_PROJECT_ENUM
    assert SSH_PROJECT_ENUM == ALL_PROJECTS == ["AADS", "KIS", "GO100", "SF", "NTV2"]
    assert SEARCH_PROJECT_ENUM == ["AADS", "KIS", "GO100", "SF", "NTV2", "NAS"]


def test_tool_registry_has_no_hardcoded_project_enum_literals():
    source = Path(__file__).parents[2].joinpath("app/services/tool_registry.py").read_text()
    project_enum_literal = re.compile(
        r'"enum"\s*:\s*\[(?:"AADS"\s*,\s*"KIS"\s*,\s*"GO100"\s*,\s*"SF"\s*,\s*"NTV2"(?:\s*,\s*"NAS")?|'
        r'"KIS"\s*,\s*"GO100"\s*,\s*"SF"\s*,\s*"NTV2"\s*,\s*"AADS")\]'
    )
    assert not project_enum_literal.search(source)
