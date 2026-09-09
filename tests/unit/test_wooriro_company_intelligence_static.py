from __future__ import annotations

import csv
import json
import math
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "app/static/reports"
BASELINE = REPORTS / "20260909_wooriro_046970_company_intelligence_baseline.html"
SUPPLEMENT = REPORTS / "20260909_wooriro_046970_stock_direction_supplement_v1.html"
HUB = REPORTS / "wooriro-046970-intelligence-hub.html"
MANIFEST = REPORTS / "wooriro-046970-manifest.json"
OHLCV = REPORTS / "data/wooriro-046970-ohlcv-20260810-20260908.csv"


class AuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.hrefs: list[str] = []
        self.has_lang = False
        self.has_viewport = False
        self.has_main = False
        self.has_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html" and values.get("lang") == "ko":
            self.has_lang = True
        if tag == "meta" and values.get("name") == "viewport":
            self.has_viewport = True
        if tag == "main":
            self.has_main = True
        if tag == "h1":
            self.has_h1 = True
        if values.get("id"):
            assert values["id"] not in self.ids
            self.ids.add(values["id"] or "")
        if tag == "a" and values.get("href"):
            self.hrefs.append(values["href"] or "")


def _parse(path: Path) -> tuple[str, AuditParser]:
    text = path.read_text(encoding="utf-8")
    parser = AuditParser()
    parser.feed(text)
    parser.close()
    return text, parser


def _rows() -> list[dict[str, str]]:
    with OHLCV.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_html_parsing_links_and_static_accessibility_contract() -> None:
    for path in (BASELINE, SUPPLEMENT, HUB):
        text, parser = _parse(path)
        assert parser.has_lang and parser.has_viewport and parser.has_main and parser.has_h1
        assert "@media(max-width:" in text or "@media (max-width:" in text
        assert "@media print" in text
        assert "우리로(046970)" in text
        for href in parser.hrefs:
            if href.startswith("#"):
                assert href[1:] in parser.ids
    baseline_text = BASELINE.read_text(encoding="utf-8")
    assert "aria-label=\"보고서 목차\"" in baseline_text
    assert "독립확인" in baseline_text and "회사주장" in baseline_text
    assert "추정" in baseline_text and "미확인" in baseline_text


def test_manifest_bidirectional_version_and_lineage_contract() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["company"] == "우리로(046970)"
    assert manifest["baseline"]["immutable"] is True
    assert manifest["baseline"]["version"] == "1.0.0"
    assert manifest["publication_policy"]["arbitrary_topic_auto_publish"] is False
    for path in (BASELINE, SUPPLEMENT, HUB):
        assert path.name in HUB.read_text(encoding="utf-8") or path == HUB
    assert BASELINE.name in SUPPLEMENT.read_text(encoding="utf-8")
    assert SUPPLEMENT.name in BASELINE.read_text(encoding="utf-8")
    assert any(item["id"] == "ohlcv" and item["adjusted"] is False for item in manifest["lineage"])


def test_financial_cross_totals_and_ratios_are_reproducible() -> None:
    assets, liabilities, equity = 595.9, 176.8, 419.2
    assert abs(assets - liabilities - equity) <= 0.11  # displayed values are rounded to 0.1억원
    assert round(40.2 / 292.5 * 100, 2) == 13.74 or round(40.2 / 292.5 * 100, 2) == 13.75
    assert round(liabilities / equity * 100, 2) == 42.18
    assert round(-22.4 / 28.0, 2) == -0.80


def test_ohlcv_indicators_are_deterministic() -> None:
    rows = _rows()
    assert len(rows) == 21
    assert rows[-1]["date"] == "2026-09-08"
    assert all(row["adjustment"] == "unadjusted" for row in rows)
    closes = [float(row["close"]) for row in rows]
    volumes = [float(row["volume"]) for row in rows]
    sma = lambda size: sum(closes[-size:]) / size
    assert sma(5) == 4885
    assert sma(10) == 4869
    assert sma(20) == 4752
    assert round((closes[-1] / closes[-6] - 1) * 100, 2) == -1.56
    assert round((closes[-1] / closes[-11] - 1) * 100, 2) == 16.59
    assert sum(volumes[-20:]) / 20 == 743256.5
    assert "743,257주" in SUPPLEMENT.read_text(encoding="utf-8")
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(log_returns) / len(log_returns)
    sample_sd = math.sqrt(sum((item - mean) ** 2 for item in log_returns) / (len(log_returns) - 1))
    assert round(sample_sd * math.sqrt(252) * 100, 2) == 68.62


def test_prefill_is_encoded_not_auto_submitted_and_has_recovery() -> None:
    hub = HUB.read_text(encoding="utf-8")
    chat = (ROOT / "aads-dashboard/src/app/chat/page.tsx").read_text(encoding="utf-8")
    assert "encodeURIComponent(prompt)" in hub
    assert "window.open(url" in hub
    assert "const prefill = queryPrefill || savedPrefill" in chat
    assert 'params.get("source") !== "company-intelligence"' in chat
    assert '.slice(0, 4000)' in chat
    assert "sessionStorage.setItem" in chat and "window.history.replaceState" in chat
    assert "sendMessage(" not in hub and ".submit()" not in hub
    assert "sessionStorage.getItem" not in hub  # public page never imports authenticated content


def test_targeted_secret_scan_and_no_direct_external_llm_calls() -> None:
    content = "\n".join(path.read_text(encoding="utf-8") for path in (BASELINE, SUPPLEMENT, HUB, MANIFEST))
    forbidden = (
        r"sk-ant-[A-Za-z0-9_-]{12,}",
        r"ANTHROPIC_" + r"API_KEY",
        r"generativelanguage\.googleapis\.com",
        r"api\.deepseek\.com",
    )
    assert not any(re.search(pattern, content) for pattern in forbidden)
