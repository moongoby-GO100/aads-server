from app.api.project_docs import SERVER_CONFIG
from app.core.project_config import PROJECT_MAP, get_server
from app.services.server_registry import PROJECT_TO_SERVER, SERVER_REGISTRY


def test_go100_remote_tools_target_contabo14():
    go100 = PROJECT_MAP["GO100"]

    assert get_server("GO100") == "5.104.86.14"
    assert go100["server_name"] == "contabo14"
    assert go100["workdir"] == "/root/kis-autotrade-v4"
    assert SERVER_CONFIG["GO100"]["host"] == "contabo14"


def test_go100_server_registry_is_separate_from_kis():
    assert PROJECT_TO_SERVER["GO100"] == "contabo14"
    assert PROJECT_TO_SERVER["KIS"] == "211"
    assert SERVER_REGISTRY["contabo14"]["host"] == "5.104.86.14"
    assert SERVER_REGISTRY["contabo14"]["projects"] == ["GO100"]
    assert SERVER_REGISTRY["211"]["projects"] == ["KIS"]


def test_kis_remote_mapping_remains_on_server_211():
    assert get_server("KIS") == "211.188.51.113"
    assert SERVER_CONFIG["KIS"]["host"] == "server-211"
