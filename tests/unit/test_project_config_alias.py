"""[C안 1/4] project_config 별칭(alias) 레이어 단위 테스트.

resolve_project() / normalize_project_label() / get_display_name() 계약 검증.
"""
import pytest

from app.core.project_config import (
    ALL_PROJECTS,
    PROJECT_MAP,
    get_display_name,
    normalize_project_label,
    resolve_project,
)


class TestProjectMapSchema:
    def test_every_project_has_alias_fields(self):
        for key, cfg in PROJECT_MAP.items():
            assert cfg.get("display_name"), f"{key}: display_name 누락"
            aliases = cfg.get("aliases")
            assert isinstance(aliases, list) and aliases, f"{key}: aliases 누락"
            assert key in aliases, f"{key}: 정규 키가 aliases에 없음"

    def test_alias_uniqueness_across_projects(self):
        seen = {}
        for key, cfg in PROJECT_MAP.items():
            for alias in cfg.get("aliases", []):
                low = str(alias).lower()
                owner = seen.get(low)
                assert owner in (None, key), f"별칭 충돌: {alias} ({owner} vs {key})"
                seen[low] = key

    def test_all_projects_matches_map(self):
        assert ALL_PROJECTS == list(PROJECT_MAP.keys())


class TestResolveProject:
    @pytest.mark.parametrize("value,expected", [
        ("AADS", "AADS"),
        ("aads", "AADS"),
        ("KIS", "KIS"),
        ("kis", "KIS"),
        ("자동매매", "KIS"),
        ("GO100", "GO100"),
        ("go100", "GO100"),
        ("백억이", "GO100"),
        ("SF", "SF"),
        ("shortflow", "SF"),
        ("숏폼", "SF"),
        ("NTV2", "NTV2"),
        ("newtalk", "NTV2"),
        ("newtalk-v2", "NTV2"),
    ])
    def test_alias_resolution(self, value, expected):
        assert resolve_project(value) == expected

    @pytest.mark.parametrize("value,expected", [
        ("[GO100] 백억이 투자분석", "GO100"),
        ("[AADS] 프로젝트 매니저", "AADS"),
        ("[NTV2] 뉴톡", "NTV2"),
        ("  [KIS] 자동매매  ", "KIS"),
    ])
    def test_bracket_pattern(self, value, expected):
        assert resolve_project(value) == expected

    def test_display_name_resolution(self):
        for key, cfg in PROJECT_MAP.items():
            assert resolve_project(cfg["display_name"]) == key

    @pytest.mark.parametrize("value", [None, "", "   ", "UNKNOWN", "[FOOD] 열정국밥", "[]"])
    def test_unresolvable_returns_none(self, value):
        assert resolve_project(value) is None

    def test_idempotent(self):
        for key in ALL_PROJECTS:
            assert resolve_project(resolve_project(key)) == key


class TestNormalizeProjectLabel:
    def test_known_project_returns_canonical_key(self):
        assert normalize_project_label("백억이") == "GO100"
        assert normalize_project_label("[AADS] 프로젝트 매니저") == "AADS"

    def test_unknown_bracket_returns_upper_token(self):
        assert normalize_project_label("[FOOD] 열정국밥") == "FOOD"
        assert normalize_project_label("[food] 열정국밥") == "FOOD"

    def test_unknown_plain_returns_stripped_original(self):
        assert normalize_project_label("  MYPROJ  ") == "MYPROJ"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_returns_none(self, value):
        assert normalize_project_label(value) is None


class TestGetDisplayName:
    def test_known_keys(self):
        assert get_display_name("GO100") == PROJECT_MAP["GO100"]["display_name"]
        assert get_display_name("AADS") == PROJECT_MAP["AADS"]["display_name"]

    def test_unknown_key_passthrough(self):
        assert get_display_name("FOOD") == "FOOD"
