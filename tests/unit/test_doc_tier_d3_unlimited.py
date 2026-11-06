import pytest

from app.services import doc_tier


@pytest.mark.parametrize("raw", [None, "", "0", "-1", "abc"])
def test_d3_daily_limit_unlimited_by_default(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("AADS_DOC_TIER_D3_DAILY_LIMIT", raising=False)
    else:
        monkeypatch.setenv("AADS_DOC_TIER_D3_DAILY_LIMIT", raw)
    assert doc_tier.settings()["d3_daily_limit"] >= 10**9


def test_d3_daily_limit_positive_value_kept(monkeypatch):
    monkeypatch.setenv("AADS_DOC_TIER_D3_DAILY_LIMIT", "5")
    assert doc_tier.settings()["d3_daily_limit"] == 5
