"""Opus 5 -> Opus 5.5 잔여 하드코딩 기본값 회귀 테스트."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAT_SERVICE = ROOT / "app" / "services" / "chat_service.py"
SDK_SERVICE = ROOT / "app" / "services" / "agent_sdk_service.py"


def _chat_tree() -> ast.Module:
    return ast.parse(CHAT_SERVICE.read_text(encoding="utf-8"))


def _find_dict_assign(tree: ast.AST, name: str) -> dict:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name and isinstance(node.value, ast.Dict):
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


def test_codex_to_claude_equivalent_gpt6_maps_to_opus_55():
    from app.services.model_selector import _codex_to_claude_equivalent

    assert _codex_to_claude_equivalent("gpt-6-astra") == "claude-opus-5-5"
    assert _codex_to_claude_equivalent("codex:gpt-6-sol") == "claude-opus-5-5"


def test_cross_fallback_map_never_targets_opus_5():
    chain = _find_dict_assign(_chat_tree(), "_FALLBACK_CHAIN_429")
    assert "claude-opus-5-5" in chain
    for key, targets in chain.items():
        assert "claude-opus-5" not in targets, f"{key} still falls back to claude-opus-5"


def test_default_premium_constant_is_opus_55():
    for node in _chat_tree().body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_DEFAULT_PREMIUM_CLAUDE" for t in node.targets
        ):
            assert ast.literal_eval(node.value) == "claude-opus-5-5"
            return
    raise AssertionError("_DEFAULT_PREMIUM_CLAUDE not defined")


def test_direct_execution_path_has_no_hardcoded_opus_5():
    src = CHAT_SERVICE.read_text(encoding="utf-8")
    start = src.index("[DIRECT_EXECUTION] session=")
    end = src.index("if sdk_success:", start)
    block = src[start:end]
    assert '"claude-opus-5"' not in block
    assert "model_used = _DEFAULT_PREMIUM_CLAUDE" in block
    assert "execution_model_id=_DEFAULT_PREMIUM_CLAUDE" in block
    assert "model=model_used" in block


def test_timeout_map_has_opus_55_same_as_opus_5():
    tmo = _find_dict_assign(_chat_tree(), "_MODEL_TIMEOUT_OVERRIDES")
    assert tmo["claude-opus-5-5"] == tmo["claude-opus-5"]


def test_sdk_service_accepts_model_so_label_matches_execution():
    src = SDK_SERVICE.read_text(encoding="utf-8")
    assert "model=model or " in src
    assert "model=model)" in src
