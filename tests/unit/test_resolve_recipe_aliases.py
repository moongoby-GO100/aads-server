"""resolve_recipe: 자동화 제목·별칭으로 반복 실행 매칭 (PRD FR-21/AC-08)."""
from __future__ import annotations

import pytest

from app.services import automation_draft_service as drafts
from app.services.work_recipe import orchestrator


def _row(name: str, domain: str = "example.com") -> dict:
    return {
        "name": name,
        "domain": domain,
        "spec": {"name": name, "domain": domain, "steps": [{"action": "snapshot"}]},
    }


def _draft(title: str, aliases: list[str], recipe_names: list[str]) -> dict:
    return {"id": title, "title": title, "aliases": aliases, "recipe_names": recipe_names}


@pytest.fixture
def setup(monkeypatch):
    def install(rows, active):
        async def recipes(**kwargs):
            return rows

        async def active_drafts(*, tenant_id):
            return active

        monkeypatch.setattr(orchestrator, "list_recipes", recipes)
        monkeypatch.setattr(drafts, "list_active_drafts", active_drafts)
        monkeypatch.setenv(drafts.DRAFTS_FLAG, "true")

    return install


async def test_yesterday_sales_matches_title_and_alias(setup):
    setup(
        [_row("eats_sales", "coupangeats.com"), _row("other", "other.com")],
        [_draft("쿠팡이츠 어제 매출 받기", ["어제 매출", "배달 매출"], ["eats_sales"])],
    )
    resolved = await orchestrator.resolve_recipe("어제 매출 받아줘", "tenant-1")
    assert resolved is not None and resolved.name == "eats_sales"


async def test_date_words_and_spacing_are_ignored(setup):
    setup([_row("eats_sales")], [_draft("쿠팡이츠 매출 받기", [], ["eats_sales"])])
    for text in ("이번주 매출 받아줘", "오늘 매출받아줘", "지난 달 쿠팡이츠 매출"):
        resolved = await orchestrator.resolve_recipe(text, "tenant-1")
        assert resolved is not None and resolved.name == "eats_sales", text


async def test_two_candidates_returned_and_resolve_is_none(setup):
    setup(
        [_row("eats_sales", "coupangeats.com"), _row("baemin_sales", "baemin.com")],
        [
            _draft("쿠팡이츠 매출 받기", ["어제 매출"], ["eats_sales"]),
            _draft("배민 매출 받기", ["어제 매출"], ["baemin_sales"]),
        ],
    )
    candidates = await orchestrator.resolve_recipe_candidates("어제 매출 받아줘", "tenant-1")
    assert {r.name for r in candidates} == {"eats_sales", "baemin_sales"}
    assert await orchestrator.resolve_recipe("어제 매출 받아줘", "tenant-1") is None


async def test_no_match_returns_none_and_empty(setup):
    setup([_row("eats_sales")], [_draft("쿠팡이츠 매출 받기", ["어제 매출"], ["eats_sales"])])
    assert await orchestrator.resolve_recipe("재고 현황 알려줘", "tenant-1") is None
    assert await orchestrator.resolve_recipe_candidates("재고 현황 알려줘", "tenant-1") == []


async def test_existing_domain_match_wins_over_draft_alias(setup):
    setup(
        [_row("eats_sales", "coupangeats.com"), _row("other_login", "example.com")],
        [_draft("쿠팡이츠 매출 받기", ["어제 매출"], ["eats_sales"])],
    )
    resolved = await orchestrator.resolve_recipe("example.com 어제 매출 확인", "tenant-1")
    assert resolved is not None and resolved.name == "other_login"


async def test_existing_name_match_wins_over_draft_alias(setup):
    setup(
        [_row("eats_sales", "coupangeats.com"), _row("login_check")],
        [_draft("쿠팡이츠 매출 받기", ["어제 매출"], ["eats_sales"])],
    )
    resolved = await orchestrator.resolve_recipe("login_check 어제 매출", "tenant-1")
    assert resolved is not None and resolved.name == "login_check"


async def test_ambiguous_domain_stays_none_without_draft_fallback(setup):
    setup(
        [_row("shop_orders", "example.com"), _row("shop_refunds", "example.com"), _row("eats_sales", "x.com")],
        [_draft("매출 받기", ["어제 매출"], ["eats_sales"])],
    )
    assert await orchestrator.resolve_recipe("example.com 어제 매출", "tenant-1") is None


async def test_drafts_ignored_when_flag_off(setup, monkeypatch):
    setup([_row("eats_sales")], [_draft("쿠팡이츠 매출 받기", ["어제 매출"], ["eats_sales"])])
    monkeypatch.delenv(drafts.DRAFTS_FLAG)
    assert await orchestrator.resolve_recipe("어제 매출 받아줘", "tenant-1") is None


async def test_draft_lookup_failure_falls_back_to_none(setup, monkeypatch):
    setup([_row("eats_sales")], [])

    async def boom(*, tenant_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(drafts, "list_active_drafts", boom)
    assert await orchestrator.resolve_recipe("어제 매출 받아줘", "tenant-1") is None


async def test_empty_directive(setup):
    setup([_row("eats_sales")], [])
    assert await orchestrator.resolve_recipe("  ", "tenant-1") is None
