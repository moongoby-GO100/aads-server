"""resolve_recipe: 자동화 제목·별칭으로 반복 실행 매칭 (PRD FR-21/AC-08)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import automation_draft_service as drafts
from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
from app.services.work_recipe import orchestrator

TENANT = "00000000-0000-0000-0000-000000000001"


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
    candidates = await orchestrator.resolve_recipe_candidates("어제 매출 받아줘", "tenant-1")
    assert [r.name for r in candidates] == ["eats_sales"]
    # 핵심어가 "매출" 하나뿐이라 후보는 잡히지만 바로 실행하지는 않는다.
    assert await orchestrator.resolve_recipe("어제 매출 받아줘", "tenant-1") is None


async def test_date_words_and_spacing_are_ignored(setup):
    setup([_row("eats_sales")], [_draft("쿠팡이츠 매출 받기", [], ["eats_sales"])])
    for text in ("이번주 매출 받아줘", "오늘 매출받아줘", "지난 달 쿠팡이츠 매출"):
        candidates = await orchestrator.resolve_recipe_candidates(text, "tenant-1")
        assert [r.name for r in candidates] == ["eats_sales"], text
    # 날짜어를 빼고도 핵심어가 둘이면 확인 없이 잡힌다.
    resolved = await orchestrator.resolve_recipe("지난 달 쿠팡이츠 매출", "tenant-1")
    assert resolved is not None and resolved.name == "eats_sales"


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


# ── 확인 카드 (AADS-CONVAUTO-T4B) ─────────────────────────────────────────────


def _eats(setup, *, aliases=("어제 매출",)):
    setup(
        [_row("eats_sales", "coupangeats.com"), _row("other", "other.com")],
        [_draft("쿠팡이츠 어제 매출 받기", list(aliases), ["eats_sales"])],
    )


@pytest.mark.parametrize(
    "text, needs",
    [
        ("어제 매출 받아줘", True),
        ("매출 알려줘", True),
        ("쿠팡이츠 알려줘", True),
        ("이번주 매출 받아줘", True),
        ("쿠팡이츠 매출 받아줘", False),
        ("지난 달 쿠팡이츠 매출", False),
    ],
)
async def test_match_strength_table(setup, text, needs):
    _eats(setup)
    match = await orchestrator.resolve_recipe_match(text, "tenant-1")
    assert match.recipe is not None and match.recipe.name == "eats_sales"
    assert match.needs_confirmation is needs
    assert match.draft_id == "쿠팡이츠 어제 매출 받기"
    assert match.draft_title == "쿠팡이츠 어제 매출 받기"
    assert match.matched_phrase
    resolved = await orchestrator.resolve_recipe(text, "tenant-1")
    assert (resolved is None) is needs


async def test_name_and_domain_matches_never_need_confirmation(setup):
    _eats(setup)
    for text in ("eats_sales", "coupangeats.com 확인", "https://coupangeats.com/x 열어줘"):
        match = await orchestrator.resolve_recipe_match(text, "tenant-1")
        assert match.recipe is not None and match.recipe.name == "eats_sales", text
        assert match.needs_confirmation is False and match.draft_id == "", text


async def test_no_match_and_ambiguous_have_no_recipe(setup):
    setup(
        [_row("eats_sales", "coupangeats.com"), _row("baemin_sales", "baemin.com")],
        [
            _draft("쿠팡이츠 매출 받기", ["어제 매출"], ["eats_sales"]),
            _draft("배민 매출 받기", ["어제 매출"], ["baemin_sales"]),
        ],
    )
    for text in ("어제 매출 받아줘", "재고 현황 알려줘"):
        match = await orchestrator.resolve_recipe_match(text, "tenant-1")
        assert match.recipe is None and match.needs_confirmation is False, text


def _intent(directive: str, **extra):
    payload = {"directive": directive, "inputs": {}, **extra}
    envelope = directive_from_authenticated_context(
        {"tenant": {"id": TENANT}, "membership": {"user_id": "user@example.com"}},
        session_id="session-1",
        correlation_id="corr-1",
        payload=payload,
        capabilities=frozenset({"recipe.execute"}),
    )
    return ChannelRouter().route_directive(envelope, capability="recipe.execute")


@pytest.fixture
def played(monkeypatch):
    calls: list = []

    async def play(recipe, *args, **kwargs):
        calls.append(recipe.name)
        return SimpleNamespace(to_dict=lambda: {"status": "success"})

    async def scoped(recipe, inputs):
        return dict(inputs)

    monkeypatch.setattr(orchestrator, "play_recipe", play)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", lambda **kw: object())
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", lambda **kw: object())
    return calls


async def _run(text: str, **extra):
    return await orchestrator.run_directive(
        text, TENANT, triggered_by="user@example.com", action_intent=_intent(text, **extra)
    )


async def test_single_keyword_directive_returns_confirmation_without_playing(setup, played):
    _eats(setup)
    result = await _run("어제 매출 받아줘")
    assert isinstance(result, orchestrator.RecipeConfirmation)
    assert played == []
    data = result.to_dict()
    assert data["status"] == "confirmation_required"
    assert data["draft_id"] == "쿠팡이츠 어제 매출 받기"
    card = data["card"]
    assert card["body"] == "쿠팡이츠 어제 매출 받기를 실행할까요?"
    assert [b["label"] for b in card["buttons"]] == ["실행", "아니요"]
    assert {"state", "headline", "title", "body", "buttons"} <= set(card)


async def test_confirmed_draft_id_executes_only_when_it_matches(setup, played):
    _eats(setup)
    result = await _run("어제 매출 받아줘", confirmed_draft_id="다른 초안")
    assert isinstance(result, orchestrator.RecipeConfirmation) and played == []
    result = await _run("어제 매출 받아줘", confirmed_draft_id="")
    assert isinstance(result, orchestrator.RecipeConfirmation) and played == []
    result = await _run("어제 매출 받아줘", confirmed_draft_id="쿠팡이츠 어제 매출 받기")
    assert result.to_dict()["status"] == "success" and played == ["eats_sales"]


@pytest.mark.parametrize("text", ["매출 알려줘", "쿠팡이츠 알려줘"])
async def test_other_meaning_phrases_do_not_run_immediately(setup, played, text):
    _eats(setup, aliases=("어제 매출", "쿠팡이츠"))
    result = await _run(text)
    assert isinstance(result, orchestrator.RecipeConfirmation) and played == []


async def test_direct_name_and_domain_still_run_immediately(setup, played):
    _eats(setup)
    assert (await _run("eats_sales 실행")).to_dict()["status"] == "success"
    assert (await _run("coupangeats.com 매출 확인")).to_dict()["status"] == "success"
    assert played == ["eats_sales", "eats_sales"]


async def test_two_keywords_draft_match_runs_without_confirmation(setup, played):
    _eats(setup)
    assert (await _run("쿠팡이츠 매출 받아줘")).to_dict()["status"] == "success"
    assert played == ["eats_sales"]


async def test_drafts_disabled_keeps_previous_behavior(setup, played, monkeypatch):
    _eats(setup)
    monkeypatch.delenv(drafts.DRAFTS_FLAG)
    assert await _run("어제 매출 받아줘") is None
    assert played == []
