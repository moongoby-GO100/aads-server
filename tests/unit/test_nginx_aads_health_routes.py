from pathlib import Path


def test_legacy_api_health_uses_bluegreen_upstream() -> None:
    config = (Path(__file__).parents[2] / "nginx-aads-safe-api-failover.conf").read_text(
        encoding="utf-8"
    )

    assert "location = /api/health" in config
    assert "proxy_pass http://aads_api/api/v1/health;" in config
    assert "proxy_pass http://127.0.0.1:8001/;" not in config


def test_auth_me_read_timeout_outlasts_api_stalls() -> None:
    # 2026-11-04: 15초 한도에서 /auth/me 504 → 대시보드 로그아웃이 반복됐다.
    config = (Path(__file__).parents[2] / "nginx-aads-safe-api-failover.conf").read_text(
        encoding="utf-8"
    )
    block = config.split("location = /api/v1/auth/me {", 1)[1].split("}", 1)[0]
    assert "proxy_read_timeout 30s;" in block
