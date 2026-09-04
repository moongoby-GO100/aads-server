from app.api import ceo_chat_tools_db
from app.services.server_registry import get_server_host


def test_kis_db_host_legacy_ip_is_remapped(monkeypatch):
    monkeypatch.setenv("KIS_DB_HOST", "211.188.51.113")
    monkeypatch.setenv("KIS_DB_NAME", "kisautotrade")
    monkeypatch.setenv("KIS_DB_USER", "kis_admin")
    monkeypatch.setenv("KIS_DB_PASSWORD", "dummy")

    config = ceo_chat_tools_db._get_project_db_config("KIS")

    assert config is not None
    assert config["host"] == get_server_host("contabo14")


def test_go100_uses_kis_db_host_alias(monkeypatch):
    monkeypatch.setenv("KIS_DB_HOST", "211.188.51.113")
    monkeypatch.setenv("KIS_DB_NAME", "kisautotrade")
    monkeypatch.setenv("KIS_DB_USER", "kis_admin")
    monkeypatch.setenv("KIS_DB_PASSWORD", "dummy")

    config = ceo_chat_tools_db._get_project_db_config("GO100")

    assert config is not None
    assert config["host"] == get_server_host("contabo14")
