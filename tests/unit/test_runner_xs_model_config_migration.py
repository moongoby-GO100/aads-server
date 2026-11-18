import json
import re
from pathlib import Path


def test_runner_xs_model_config_migration():
    sql = (Path(__file__).resolve().parents[2] / "migrations/183_runner_xs_drop_antigravity_flash.sql").read_text()
    statement = re.sub(r"--[^\n]*", "", sql).strip()

    assert re.fullmatch(
        r"UPDATE\s+runner_model_config\s+SET\s+models\s*=\s*'(?P<models>\[[^']*\])'::jsonb,\s*"
        r"updated_at\s*=\s*NOW\(\),\s*updated_by\s*=\s*"
        r"'migration:runner-xs-drop-antigravity-flash'\s+WHERE\s+size\s*=\s*'XS'\s+"
        r"AND\s+models\s*\?\s*'antigravity-flash'\s*;",
        statement,
        re.IGNORECASE,
    )
    models = json.loads(re.search(r"models\s*=\s*'(\[[^']*\])'", statement).group(1))
    assert models == [
        "codex:gpt-6.1-terra",
        "codex:gpt-6-luna",
        "claude-haiku",
        "gemini-3.8-flash-medium",
        "claude-sonnet-5-5",
        "codex:gpt-6.1-sol",
    ]
    assert "antigravity-flash" not in models
    without_literals = re.sub(r"'(?:''|[^'])*'", "''", statement)
    assert not re.search(r"\b(?:DROP|TRUNCATE|DELETE)\b", without_literals, re.IGNORECASE)
