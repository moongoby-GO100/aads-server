from pathlib import Path


def test_agent_result_artifacts_use_shared_persistent_storage():
    source = Path("app/services/tool_executor.py").read_text(encoding="utf-8")

    assert 'AADS_AGENT_ARTIFACT_DIR", "/app/app/static/exports/agent-results"' in source
    assert 'pathlib.Path("/tmp/aads_artifacts")' not in source
    assert "/api/v1/files/download?path=" in source


def test_export_data_default_matches_nginx_shared_export_mount():
    source = Path("app/api/ceo_chat_tools_export.py").read_text(encoding="utf-8")

    assert 'AADS_EXPORT_DIR", "/var/www/certbot/exports"' in source
    assert 'AADS_EXPORT_DIR", "/var/www/aads_exports"' not in source
