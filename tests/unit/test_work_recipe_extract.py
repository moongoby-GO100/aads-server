"""수집 레시피: 서버/PC 행 범위, 실패 판정 및 페이지네이션 회귀."""
import copy
import json

import pytest

from app.services.work_recipe.executor import BrowserRecipeExecutor
from app.services.work_recipe.guard import RiskLevel, classify_step
from app.services.work_recipe.player import RecipePlayer
from app.services.work_recipe.schema import RecipeStep, WorkRecipe


def extract(**changes):
    spec = {"action": "extract", "name": "items", "row_selector": ".row",
            "fields": {"id": {"selector": ".id", "type": "text", "required": True}},
            "key_fields": ["id"]}
    spec.update(changes)
    return spec


def recipe(steps):
    return WorkRecipe.from_dict({"name": "수집", "domain": "example.com", "steps": steps})


class Locator:
    def __init__(self, items):
        self.items = items

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return Locator(self.items[index:index + 1])

    @property
    def first(self):
        return self.nth(0)

    def locator(self, selector):
        assert selector.startswith(".")  # 부모 행 바깥 조회는 허용하지 않는다.
        value = self.items[0].get(selector)
        return Locator([] if value is None else [value])

    async def inner_text(self):
        return self.items[0]

    async def get_attribute(self, name):
        return self.items[0].get(name) if isinstance(self.items[0], dict) else None

    async def is_visible(self):
        return bool(self.items and self.items[0].get("visible", True))

    async def is_enabled(self):
        return not self.items[0].get("disabled", False)


class Page:
    def __init__(self, pages, marker=False):
        self.pages = pages
        self.index = 0
        self.marker = marker
        self.visits = []

    def locator(self, selector):
        if selector == ".row":
            return Locator(self.pages[self.index])
        if selector == ".empty":
            return Locator([{"visible": self.marker}])
        if selector == ".next":
            return Locator([{}] if self.index + 1 < len(self.pages) else [])
        raise AssertionError("page 레벨에서 셀을 조회하면 안 됩니다")

    async def goto(self, url):
        self.visits.append(url)
        self.index = int(url.rsplit("=", 1)[1]) - 1

    async def click(self, selector):
        assert selector == ".next"
        self.index += 1


@pytest.fixture
def executor():
    # 외부 서비스 연결 없이 실제 실행 메서드만 검증한다.
    instance = object.__new__(BrowserRecipeExecutor)
    instance._pc_lane_forced = False
    instance._page = None
    return instance


@pytest.mark.parametrize("changes", [
    {"row_selector": ""}, {"fields": {}}, {"key_fields": []}, {"key_fields": ["absent"]},
    {"max_rows": 2001}, {"max_rows": 0}, {"min_rows": -1}, {"min_rows": 501},
    {"fields": {"SecretId": {"selector": ".x", "type": "text"}}},
    {"fields": {"계좌번호": {"selector": ".x", "type": "text"}}},
    {"fields": {"id": {"selector": "xpath=../x", "type": "text"}}},
    {"fields": {"id": {"selector": ".x", "type": "html"}}},
    {"fields": {"id": {"selector": ".x", "type": "text", "required": "yes"}}},
])
def test_invalid_extract(changes):
    with pytest.raises(ValueError):
        recipe([extract(**changes)])


@pytest.mark.parametrize("changes", [
    {"max_pages": 51}, {"delay_ms": 999}, {"steps": ["missing"]},
    {"stop_when": ["whatever"]}, {"url_template": "https://example.com", "next_selector": None},
])
def test_invalid_paginate(changes):
    step = {"action": "paginate", "steps": ["items"], "next_selector": ".next", **changes}
    with pytest.raises(ValueError):
        recipe([extract(), step])


def test_defaults_roundtrip_and_risk():
    parsed = recipe([extract(), {"action": "paginate", "steps": ["items"], "next_selector": ".next"}])
    assert parsed.steps[0].extra["max_rows"] == 500
    assert parsed.steps[1].extra["max_pages"] == 20
    assert WorkRecipe.from_yaml(parsed.to_yaml()).to_dict() == parsed.to_dict()
    for step in parsed.steps:
        assert step.risk == "READ"
        assert classify_step(step) == RiskLevel.READ


@pytest.mark.asyncio
async def test_types_masking_and_relative_rows(executor):
    fields = {
        "id": {"selector": ".id", "type": "text", "required": True},
        "count": {"selector": ".count", "type": "int"},
        "price": {"selector": ".price", "type": "money"},
        "ratio": {"selector": ".ratio", "type": "number"},
        "day": {"selector": ".day", "type": "date"},
        "time": {"selector": ".time", "type": "datetime"},
        "link": {"selector": ".link", "attr": "href", "type": "url"},
        "enabled": {"selector": ".enabled", "type": "bool"},
        "contact": {"selector": ".contact", "type": "text"},
    }
    page = Page([[{".id": "A", ".count": "1,234", ".price": "₩2,500", ".ratio": "1.5",
                   ".day": "2026-10-30", ".time": "2026-10-30T12:00:00Z",
                   ".link": {"href": "https://example.com"}, ".enabled": "false",
                   ".contact": "test@example.com 010-1234-5678"}]])
    result = await executor._execute(page, "extract", extract(fields=fields))
    assert result["status"] == "success"
    row = result["rows"][0]
    assert row["count"] == 1234 and row["price"] == 2500 and row["ratio"] == 1.5
    assert row["enabled"] is False and row["link"] == "https://example.com"
    assert "test@example.com" not in row["contact"] and "010-1234-5678" not in row["contact"]
    assert "html" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("count,missing,status", [(20, 1, "success"), (20, 2, "extraction_failed")])
async def test_required_threshold(executor, count, missing, status):
    page = Page([[{".id": str(i)} if i >= missing else {} for i in range(count)]])
    output = await executor._extract(page, extract())
    assert output["status"] == status
    assert len(output["row_errors"]) == missing


@pytest.mark.asyncio
@pytest.mark.parametrize("marker,status", [(True, "no_records"), (False, "extraction_failed")])
async def test_empty_marker_must_be_visible(executor, marker, status):
    output = await executor._extract(Page([[]], marker), extract(no_records_marker=".empty"))
    assert output["status"] == status


@pytest.mark.asyncio
async def test_min_max_rows_and_conversion_errors(executor):
    page = Page([[{".id": "1"}, {".id": "bad"}, {".id": "3"}]])
    spec = extract(fields={"id": {"selector": ".id", "type": "int", "required": True}}, max_rows=2)
    output = await executor._extract(page, spec)
    assert output["row_count"] == 2 and output["status"] == "extraction_failed"
    assert output["row_errors"][0]["error"] == "invalid_type"
    assert (await executor._extract(Page([[{".id": "1"}]]), extract(min_rows=2)))["status"] == "extraction_failed"


@pytest.mark.asyncio
async def test_pc_browser_command(executor):
    class PC:
        async def _run_browser_command(self, command, params):
            assert command == "browser_eval"
            assert "row.querySelector(field.selector)" in params["expression"]
            assert "outerHTML" not in params["expression"]
            return {"value": json.dumps({"rows": [{"id": "pc@example.com"}], "marker": False})}
    output = await executor._extract(PC(), extract())
    assert output["status"] == "success"
    assert "pc@example.com" not in output["rows"][0]["id"]


def pagination(**changes):
    return {"action": "paginate", "steps": ["items"], "next_selector": ".next",
            "context": {"collection_steps": {"items": extract()}}, **changes}


@pytest.mark.asyncio
async def test_pagination_dedup_and_next_missing(executor, monkeypatch):
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr("asyncio.sleep", sleep)
    output = await executor._paginate(Page([[{".id": "a"}], [{".id": "a"}, {".id": "b"}]]), pagination())
    assert output["status"] == "success" and output["row_count"] == 2
    assert output["pages"] == 2 and delays == [1.0]


@pytest.mark.asyncio
async def test_no_new_keys_and_limit(executor, monkeypatch):
    async def sleep(_):
        pass
    monkeypatch.setattr("asyncio.sleep", sleep)
    page = Page([[{".id": "a"}], [{".id": "a"}], [{".id": "b"}]])
    result = await executor._paginate(page, pagination())
    assert result["pages"] == 2 and result["row_count"] == 1
    result = await executor._paginate(Page(page.pages), pagination(max_pages=1))
    assert result["status"] == "partial"


@pytest.mark.asyncio
async def test_url_template_and_failure(executor, monkeypatch):
    async def sleep(_):
        pass
    monkeypatch.setattr("asyncio.sleep", sleep)
    page = Page([[{".id": "a"}], [{}]])
    result = await executor._paginate(page, pagination(next_selector=None, url_template="https://example.com?page={page}"))
    assert result["status"] == "extraction_failed"
    assert len(page.visits) == 2


@pytest.mark.asyncio
async def test_player_rejects_fake_success_and_passes_definitions():
    payloads = []
    async def execute(payload):
        payloads.append(copy.deepcopy(payload))
        return {"ok": True, "output": {"status": "extraction_failed", "rows": []}}
    result = await RecipePlayer(execute, retries=0).play(recipe([extract()]))
    assert result.status == "failed"
    assert payloads[0]["context"]["collection_steps"]["items"]["action"] == "extract"


@pytest.mark.asyncio
async def test_executor_failure_status_does_not_collect_evidence(executor, monkeypatch):
    from types import SimpleNamespace
    async def gate(*_):
        return SimpleNamespace(to_dict=lambda: {})
    async def page(*_):
        return Page([[{}]])
    async def evidence(*_):
        pytest.fail("수집 단계는 원문 화면 증거를 반환하지 않는다")
    monkeypatch.setattr(executor, "_gate", gate)
    monkeypatch.setattr(executor, "_get_page", page)
    monkeypatch.setattr(executor, "_assert_trusted_arguments", lambda *_: None)
    monkeypatch.setattr(executor, "_evidence_for", evidence)
    result = await executor._attempt("extract", extract())
    assert result["ok"] is False
    assert result["output"]["status"] == "extraction_failed"


@pytest.mark.asyncio
async def test_player_retains_failed_output():
    async def execute(_):
        return {"ok": False, "output": {"status": "extraction_failed", "row_errors": [{"field": "id"}]}}
    result = await RecipePlayer(execute, retries=0).play(recipe([extract()]))
    assert result.steps[0].output["row_errors"] == [{"field": "id"}]


@pytest.mark.asyncio
async def test_pc_pagination(executor, monkeypatch):
    async def sleep(_):
        pass
    monkeypatch.setattr("asyncio.sleep", sleep)
    class PC:
        def __init__(self):
            self.index = 0
        async def _run_browser_command(self, command, params):
            assert command == "browser_eval"
            if "row.querySelector" in params["expression"]:
                return {"value": {"rows": [{"id": str(self.index)}], "marker": False}}
            return {"value": self.index == 0}
        async def click(self, selector):
            self.index += 1
    output = await executor._paginate(PC(), pagination())
    assert output["row_count"] == 2 and output["status"] == "success"


@pytest.mark.asyncio
async def test_partial_is_preserved(executor):
    async def execute(payload):
        if payload["action"] == "extract":
            return {"output": {"status": "success"}}
        return {"output": {"status": "partial", "rows": [{"id": "1"}]}}
    result = await RecipePlayer(execute).play(recipe([
        extract(), {"action": "paginate", "steps": ["items"], "next_selector": ".next", "max_pages": 1}]))
    assert result.steps[-1].output["status"] == "partial"
