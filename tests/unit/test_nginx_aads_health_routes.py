from pathlib import Path


def test_legacy_api_health_uses_bluegreen_upstream() -> None:
    config = (Path(__file__).parents[2] / "nginx-aads-safe-api-failover.conf").read_text(
        encoding="utf-8"
    )

    assert "location = /api/health" in config
    assert "proxy_pass http://aads_api/api/v1/health;" in config
    assert "proxy_pass http://127.0.0.1:8001/;" not in config
