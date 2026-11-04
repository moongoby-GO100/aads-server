"""contabo42(2026-11-04 증설, AADS 범용 러너 풀) 는 원장·서버 카드에만 올라가고 감시·프로젝트 라우팅은 바뀌지 않는다."""

from app.services.server_registry import (
    CANONICAL_SERVER_IDS,
    LEDGER_SERVER_IDS,
    PROJECT_TO_SERVER,
    SERVER_REGISTRY,
    get_server_host,
    get_server_ssh_port,
    list_ledger_servers,
    list_servers,
    resolve_server_id,
)


def test_contabo42_is_in_ledger_but_not_health_monitored():
    assert "contabo42" in [s["id"] for s in list_ledger_servers()]
    assert "contabo42" in LEDGER_SERVER_IDS
    assert "contabo42" not in CANONICAL_SERVER_IDS
    assert "contabo42" not in [s["id"] for s in list_servers()]


def test_contabo42_lookup():
    assert get_server_host("contabo42") == "46.250.255.42"
    assert get_server_ssh_port("contabo42") == 22
    assert resolve_server_id("contabo42") == "contabo42"
    assert SERVER_REGISTRY["contabo42"]["ssh_alias"] == "contabo42"
    assert SERVER_REGISTRY["contabo42"]["status"] == "active"
    assert SERVER_REGISTRY["contabo42"]["projects"] == []


def test_contabo42_does_not_take_over_project_routing():
    assert "contabo42" not in PROJECT_TO_SERVER.values()
