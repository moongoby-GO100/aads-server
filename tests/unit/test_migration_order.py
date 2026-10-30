from pathlib import Path


def test_design_intake_sessions_create_precedes_alter():
    """Release migrations run in byte-sorted filename order under LC_ALL=C."""
    migration_dir = Path(__file__).resolve().parents[2] / "migrations"
    migrations = sorted(migration_dir.glob("*.sql"), key=lambda path: path.name.encode())

    create_files = [
        index
        for index, path in enumerate(migrations)
        if "CREATE TABLE IF NOT EXISTS design_intake_sessions" in path.read_text()
    ]
    alter_files = [
        index
        for index, path in enumerate(migrations)
        if "ALTER TABLE design_intake_sessions" in path.read_text()
    ]

    assert create_files, "design_intake_sessions CREATE migration not found"
    assert alter_files, "design_intake_sessions ALTER migration not found"
    assert min(create_files) < min(alter_files), (
        "design_intake_sessions ALTER migration sorts before its CREATE migration"
    )
