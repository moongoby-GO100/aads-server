"""contabo92(GO100 보관 DB·백업) 는 원장에만 올라가고 감시·프로젝트 라우팅은 바뀌지 않는다."""

from app.services.server_registry import (
    ALL_PROJECTS,
    CANONICAL_SERVER_IDS,
    HEALTH_MONITORED_SERVER_IDS,
    LEDGER_SERVER_IDS,
    PROJECT_TO_SERVER,
    SERVER_REGISTRY,
    get_server_for_project,
    get_server_host,
    get_server_ssh_port,
    get_servers_for_projects,
    is_health_monitored,
    list_health_monitored_servers,
    list_ledger_servers,
    list_servers,
    resolve_server_id,
)


def test_contabo92_is_health_monitored_but_not_canonical():
    """2026-11-05 건강감시 편입. 기존 3대 순회(CANONICAL)는 그대로 둔다."""
    assert "contabo92" in [s["id"] for s in list_ledger_servers()]
    assert "contabo92" in LEDGER_SERVER_IDS
    assert "contabo92" in HEALTH_MONITORED_SERVER_IDS
    assert is_health_monitored("contabo92")
    assert "contabo92" not in CANONICAL_SERVER_IDS
    assert "contabo92" not in [s["id"] for s in list_servers()]
    assert [s["id"] for s in list_health_monitored_servers()][-1] == "contabo92"
    assert len(HEALTH_MONITORED_SERVER_IDS) == 4
    assert not is_health_monitored("contabo42")
    assert not is_health_monitored("jinah244")


def test_contabo92_lookup():
    assert get_server_host("contabo92") == "13.140.248.92"
    assert get_server_ssh_port("contabo92") == 22
    assert resolve_server_id("contabo92") == "contabo92"
    assert SERVER_REGISTRY["contabo92"]["ssh_alias"] == "contabo92"
    assert SERVER_REGISTRY["contabo92"]["status"] == "active"


def test_contabo92_has_no_numeric_alias():
    for alias in ("92", "server92"):
        assert alias not in SERVER_REGISTRY
        assert resolve_server_id(alias) == alias


def test_go100_routing_stays_on_contabo14():
    assert get_server_for_project("GO100") == "contabo14"
    assert PROJECT_TO_SERVER["GO100"] == "contabo14"
    assert get_servers_for_projects(["GO100"]) == ["contabo14"]
    assert "contabo92" not in PROJECT_TO_SERVER.values()
    assert SERVER_REGISTRY["contabo92"]["projects"] == []
    assert "GO100" in ALL_PROJECTS


def test_contabo92_not_duplicated_in_project_listing():
    claimed = [p for s in list_ledger_servers() for p in s.get("projects", [])]
    assert claimed.count("GO100") == 1
