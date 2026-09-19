import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[2] / "app/services/aria_structure_signature.py"
SPEC = importlib.util.spec_from_file_location("aria_structure_signature_under_test", MODULE_PATH)
aria_signature = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = aria_signature
SPEC.loader.exec_module(aria_signature)

assess_revisit = aria_signature.assess_revisit
build_partial_signature = aria_signature.build_partial_signature
normalize_accessible_name = aria_signature.normalize_accessible_name
record_revisit_signature = aria_signature.record_revisit_signature


def _nodes():
    return [
        {"role": "searchbox", "accessible_name": "Product search", "states": {"required": True},
         "attributes": {"data-testid": "catalog-search", "id": "random-12345"}},
        {"role": "button", "accessible_name": "Search", "states": {"disabled": False}},
    ]


def test_stable_aria_signature_excludes_dynamic_id_price_ad_and_personalized_text():
    signature = build_partial_signature(_nodes() + [
        {"role": "status", "accessible_name": "Price $12.99"},
        {"role": "complementary", "accessible_name": "Sponsored advertisement"},
        {"role": "heading", "accessible_name": "Welcome Alice"},
    ], area_key="catalog-search")
    rendered = str(signature["structure"])
    assert "random-12345" not in rendered and "12.99" not in rendered and "alice" not in rendered.lower()
    assert len(signature["structure"]["nodes"]) == 2
    assert normalize_accessible_name(" Product   Search ") == "product search"


def test_allowed_dynamic_changes_reuse_and_unstable_sibling_order_is_ignored():
    prior = build_partial_signature(_nodes(), area_key="catalog-search")
    result = assess_revisit(previous=prior, current_nodes=list(reversed(_nodes())), area_key="catalog-search")
    assert result["decision"] == "reuse"
    assert result["similarity"] == 1.0


def test_missing_required_anchor_goes_to_human_gateway_and_ambiguous_goes_to_rediscovery():
    result = assess_revisit(
        previous=None, current_nodes=[{"role": "button", "accessible_name": "Search"}], area_key="catalog-search",
        template={"required_anchors": [{"role": "searchbox", "name": "Product search"}]},
    )
    assert result["decision"] == "human_gateway"
    ambiguous = assess_revisit(previous=None, current_nodes=[
        {"role": "button", "accessible_name": "Search"}, {"role": "button", "accessible_name": "Search"},
    ], area_key="catalog-search")
    assert ambiguous["decision"] == "rediscover"


def test_missing_aria_requires_dom_fallback_and_role_change_does_not_reuse():
    assert assess_revisit(previous=None, current_nodes=[], area_key="catalog-search")["reason"] == "aria_missing_dom_fallback_required"
    prior = build_partial_signature(_nodes(), area_key="catalog-search")
    changed = assess_revisit(previous=prior, current_nodes=[
        {"role": "textbox", "accessible_name": "Product search"}, {"role": "button", "accessible_name": "Search"},
    ], area_key="catalog-search")
    assert changed["decision"] == "rediscover"


def test_stable_data_attribute_names_are_case_normalized():
    lower = build_partial_signature(
        [{"role": "button", "name": "Search", "attributes": {"data-testid": "catalog-search"}}],
        area_key="catalog-search",
    )
    mixed = build_partial_signature(
        [{"role": "button", "name": "Search", "attributes": {"DATA-TESTID": "catalog-search"}}],
        area_key="catalog-search",
    )
    assert lower == mixed


def test_record_revisit_signature_scopes_every_query_to_tenant(monkeypatch):
    calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeConnection:
        async def fetchrow(self, query, *args):
            calls.append((query, args))
            if "FROM browser_recipes" in query:
                return {"capture_rules": {"aria_signature": {}}}
            return None

        async def execute(self, query, *args):
            calls.append((query, args))

    class Acquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    pool = SimpleNamespace(acquire=lambda: Acquire())
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)

    asyncio.run(
        record_revisit_signature(
            tenant_id="00000000-0000-0000-0000-000000000001",
            recipe_id="catalog-search",
            recipe_version="v1",
            page_key="catalog",
            area_key="search",
            current_nodes=_nodes(),
        )
    )

    assert len(calls) == 3
    assert all("tenant_id=$1::uuid" in query or "(tenant_id," in query for query, _ in calls)
    assert all(args[0] == "00000000-0000-0000-0000-000000000001" for _, args in calls)
