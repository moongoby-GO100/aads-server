#!/usr/bin/env python3
"""Record pre-ledger SQL files without claiming that they were actually applied."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_DIRS = (Path("migrations"), Path("scripts/migrations"))
BACKFILL_NOTE = "2026-09-18 이력테이블 도입 전 적용분 — 적용 여부 미검증"


def collect_migration_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Return regular .sql files from both migration directories."""
    files: list[Path] = []
    for relative_dir in MIGRATION_DIRS:
        directory = repo_root / relative_dir
        if directory.is_dir():
            files.extend(path for path in directory.iterdir() if path.is_file() and path.suffix == ".sql")
    return sorted(files, key=lambda path: path.relative_to(repo_root).as_posix())


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_backfill_sql(files: list[Path], repo_root: Path = REPO_ROOT) -> str:
    statements = ["BEGIN;"]
    for path in files:
        filename = path.relative_to(repo_root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        statements.append(
            "INSERT INTO schema_migrations (filename, sha256, applied_by, source, note) "
            f"VALUES ({sql_literal(filename)}, {sql_literal(digest)}, current_user, "
            f"'backfill', {sql_literal(BACKFILL_NOTE)}) "
            "ON CONFLICT (filename) DO NOTHING;"
        )
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files = collect_migration_files()
    print(f"수집한 SQL 파일: {len(files)}개")
    if args.dry_run:
        for path in files:
            print(path.relative_to(REPO_ROOT).as_posix())
        return 0

    command = [
        "docker", "exec", "-i", os.environ.get("PG_CONTAINER", "aads-postgres"),
        "psql", "-v", "ON_ERROR_STOP=1", "-U", os.environ.get("PGUSER", "aads"),
        "-d", os.environ.get("PGDATABASE", "aads"),
    ]
    subprocess.run(command, input=build_backfill_sql(files), text=True, check=True)
    print(f"백필 이력 기록 완료: 최대 {len(files)}개 (기존 filename 제외)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
