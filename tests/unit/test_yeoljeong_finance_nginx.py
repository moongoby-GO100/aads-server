from pathlib import Path


def test_finance_html_is_not_browser_cached() -> None:
    config = (Path(__file__).parents[2] / "nginx-fb.conf").read_text(encoding="utf-8")

    assert config.count("location = /static/apps/yeoljeong-finance/index.html") == 2
    assert config.count('Cache-Control "no-store, no-cache, must-revalidate, max-age=0"') == 4
    assert config.count("index.html?v=20260722.2135") == 4
