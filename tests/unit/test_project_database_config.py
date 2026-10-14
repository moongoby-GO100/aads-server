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


def _set_aads_env(monkeypatch, host="postgres", port="5432"):
    monkeypatch.setenv("AADS_DB_HOST", host)
    monkeypatch.setenv("AADS_DB_PORT", port)
    monkeypatch.setenv("AADS_DB_NAME", "aads")
    monkeypatch.setenv("AADS_DB_USER", "u")
    monkeypatch.setenv("AADS_DB_PASSWORD", "p")


def test_aads_host_prefers_database_url(monkeypatch):
    _set_aads_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@aads-postgres:5432/aads")

    config = ceo_chat_tools_db._get_project_db_config("AADS")

    assert config is not None
    assert config["host"] == "aads-postgres"
    assert config["port"] == "5432"
    assert config["database"] == "aads"


def test_aads_database_url_port_overrides_env_port(monkeypatch):
    _set_aads_env(monkeypatch, port="5432")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@aads-postgres:6543/aads")

    config = ceo_chat_tools_db._get_project_db_config("AADS")

    assert config["host"] == "aads-postgres"
    assert config["port"] == "6543"


def test_aads_without_database_url_keeps_env_host(monkeypatch):
    _set_aads_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    config = ceo_chat_tools_db._get_project_db_config("AADS")

    assert config["host"] == "postgres"


def test_aads_unparseable_database_url_keeps_env_host(monkeypatch):
    _set_aads_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "not-a-url")

    config = ceo_chat_tools_db._get_project_db_config("AADS")

    assert config["host"] == "postgres"


def test_go100_ignores_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@aads-postgres:5432/aads")
    monkeypatch.setenv("GO100_DB_HOST", "10.0.0.5")
    monkeypatch.setenv("GO100_DB_PORT", "5433")
    monkeypatch.setenv("GO100_DB_NAME", "go100")
    monkeypatch.setenv("GO100_DB_USER", "u")
    monkeypatch.setenv("GO100_DB_PASSWORD", "p")

    config = ceo_chat_tools_db._get_project_db_config("GO100")

    assert config["host"] == "10.0.0.5"
    assert config["port"] == "5433"
