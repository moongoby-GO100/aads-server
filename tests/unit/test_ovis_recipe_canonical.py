from pathlib import Path

from app.services.ovis_recipe import canonical_json, recipe_checksum


def test_ovis_recipe_checksum_is_stable_for_key_order():
    first = {"tenant": "t1", "steps": [{"action": "navigate", "seq": 1}]}
    second = {"steps": [{"seq": 1, "action": "navigate"}], "tenant": "t1"}
    assert canonical_json(first) == canonical_json(second)
    assert recipe_checksum(first) == recipe_checksum(second)


def test_canonical_migration_preserves_legacy_rows_and_provides_rollback_reference():
    root = Path(__file__).resolve().parents[2]
    sql = (root / "migrations" / "20260919_m6_ovis_recipe_canonical.sql").read_text(encoding="utf-8")
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "CREATE TABLE IF NOT EXISTS ovis_recipes" in sql
    assert "CREATE TABLE IF NOT EXISTS ovis_recipe_versions" in sql
    assert "CREATE TABLE IF NOT EXISTS ovis_recipe_legacy_refs" in sql
    assert "rollback_definition" in sql
    assert "FROM browser_recipes" in sql
    assert "FROM work_recipes" in sql
    assert "ops_skill_versions" in sql
    assert "skill_version_id UUID NULL REFERENCES ops_skill_versions" in sql
    assert "promotion_artifact_id UUID NULL REFERENCES browser_learned_artifacts" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "UPDATE browser_recipes" not in sql.upper()
    assert "UPDATE work_recipes" not in sql.upper()


def test_legacy_adapters_sync_only_through_ovis_recipe_reference_contract():
    root = Path(__file__).resolve().parents[2]
    browser = (root / "app/services/browser_recipe_registry.py").read_text(encoding="utf-8")
    work = (root / "app/services/work_recipe/store.py").read_text(encoding="utf-8")
    registration = (root / "app/services/work_recipe/registration.py").read_text(encoding="utf-8")
    for source in (browser, work, registration):
        assert "sync_legacy_reference" in source
        assert "ovis_recipe_ref" in source


def test_work_recipe_save_serializes_same_identity_version_allocation():
    root = Path(__file__).resolve().parents[2]
    source = (root / "app/services/work_recipe/store.py").read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in source
    assert "async with conn.transaction()" in source


def test_browser_adapter_uses_content_addressed_canonical_version_for_legacy_overwrites():
    root = Path(__file__).resolve().parents[2]
    source = (root / "app/services/browser_recipe_registry.py").read_text(encoding="utf-8")
    assert "@{recipe['version_hash'][:16]}" in source
