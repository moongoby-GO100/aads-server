r"""단위 테스트: 마이그레이션 2건 되돌림 SQL 가드 및 계약 검증.

TASK_ID: AADS-DEPLOY-MIGRATION-ROLLBACK-GUARD-20261115
검증 항목:
  1. 정방향 2파일 sha256 불변 (매니페스트 해시 보존)
  2. 같은 마이그레이션의 down.sql 과 _rollback.sql 내용 동일 (두 벌 동일성)
  3. 되돌림 파일 4개의 단일 트랜잭션화 (BEGIN; ... COMMIT;, \set ON_ERROR_STOP on, SET LOCAL 직후 배치)
  4. 삭제 전 객체 존재·정의 확인 가드 (DO 블록, schema_migrations 기록, 정방향 타입·nullable 일치, pg_depend 의존성, 멱등 NOTICE, 부분 잔존 EXCEPTION)
  5. CASCADE 일절 미사용 (기존 객체 보호)
  6. 서버 측 COPY 사용 금지 (\copy 클라이언트 백업 안내)
  7. 20261112 러너 롤백 설명 정정 (pipeline-runner.sh 잔존 열 사용, running 작업 0건 확인, 1차 롤백 무해성, 백업 검증)
  8. public 스키마 명시 (search_path 독립성)
  9. check_migration_txn.py 예외 경로 인정
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FORWARD_20261111 = REPO_ROOT / "migrations/20261111_canonical_document_approval_delegation.sql"
FORWARD_20261112 = REPO_ROOT / "migrations/20261112_code_review_requests_revert_of_verified.sql"

DOWN_20261111 = REPO_ROOT / "migrations/rollback/20261111_canonical_document_approval_delegation.down.sql"
ROLLBACK_20261111 = REPO_ROOT / "migrations/20261111_canonical_document_approval_delegation_rollback.sql"

DOWN_20261112 = REPO_ROOT / "migrations/rollback/20261112_code_review_requests_revert_of_verified.down.sql"
ROLLBACK_20261112 = REPO_ROOT / "migrations/20261112_code_review_requests_revert_of_verified_rollback.sql"

ALL_ROLLBACK_FILES = [
    DOWN_20261111,
    ROLLBACK_20261111,
    DOWN_20261112,
    ROLLBACK_20261112,
]

EXPECTED_SHA256_20261111 = "97a3e9edbbfed1d19e76f9c98a75fd93d54b7081348d53f5092cfb44142d9afa"
EXPECTED_SHA256_20261112 = "6d9f90e8139434e72370c248bedef9d72d7158487fb47574bd27ac128f281a0e"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_forward_migration_sha256_invariant():
    """정방향 2파일은 단 한 바이트도 변경되어서는 안 된다 (매니페스트 해시 불변)."""
    assert _sha256(FORWARD_20261111) == EXPECTED_SHA256_20261111, (
        f"20261111 sha256 mismatch! expected={EXPECTED_SHA256_20261111}, actual={_sha256(FORWARD_20261111)}"
    )
    assert _sha256(FORWARD_20261112) == EXPECTED_SHA256_20261112, (
        f"20261112 sha256 mismatch! expected={EXPECTED_SHA256_20261112}, actual={_sha256(FORWARD_20261112)}"
    )


def test_rollback_pairs_identical():
    """같은 마이그레이션의 down.sql 과 _rollback.sql 은 내용이 바이트 단위로 동일해야 한다."""
    assert DOWN_20261111.read_bytes() == ROLLBACK_20261111.read_bytes(), (
        "20261111 down.sql and _rollback.sql must be identical"
    )
    assert DOWN_20261112.read_bytes() == ROLLBACK_20261112.read_bytes(), (
        "20261112 down.sql and _rollback.sql must be identical"
    )


def test_single_transaction_and_timeout():
    """되돌림 파일 4개는 단일 트랜잭션(BEGIN; ... COMMIT;), ON_ERROR_STOP, lock_timeout을 갖춰야 한다."""
    for path in ALL_ROLLBACK_FILES:
        text = path.read_text(encoding="utf-8")
        assert "\\set ON_ERROR_STOP on" in text, f"{path.name} missing \\set ON_ERROR_STOP on"
        assert "BEGIN;" in text, f"{path.name} missing BEGIN;"
        assert "COMMIT;" in text, f"{path.name} missing COMMIT;"

        # BEGIN 이후 lock_timeout, statement_timeout 배치 확인
        begin_idx = text.find("BEGIN;")
        commit_idx = text.find("COMMIT;")
        lock_idx = text.find("SET LOCAL lock_timeout = '5s';")
        stmt_idx = text.find("SET LOCAL statement_timeout = '60s';")

        assert begin_idx < lock_idx < stmt_idx < commit_idx, (
            f"{path.name}: timeouts must be placed immediately after BEGIN and before COMMIT"
        )


def test_guard_do_block_structure():
    """되돌림 파일 4개는 삭제 전 객체 존재·정의 확인 DO 블록을 포함해야 한다."""
    for path in ALL_ROLLBACK_FILES:
        text = path.read_text(encoding="utf-8")
        assert "DO $$" in text and "END $$;" in text, f"{path.name} missing DO $$ ... END $$; block"
        assert "schema_migrations" in text, f"{path.name} must check schema_migrations"
        assert "RAISE EXCEPTION" in text, f"{path.name} must raise EXCEPTION on mismatch"
        assert "RAISE NOTICE" in text, f"{path.name} must raise NOTICE on already rolled back state"
        assert "pg_depend" in text, f"{path.name} must check dependencies via pg_depend"


def test_guard_specifics_20261111():
    """20261111 되돌림 파일의 가드 검증 항목 확인."""
    for path in (DOWN_20261111, ROLLBACK_20261111):
        text = path.read_text(encoding="utf-8")
        assert "project_document_approval_delegations" in text
        assert "project_document_events" in text
        assert "metadata" in text
        assert "jsonb" in text
        # DROP 문에 public 스키마 명시
        assert "DROP TABLE IF EXISTS public.project_document_approval_delegations;" in text
        assert "ALTER TABLE IF EXISTS public.project_document_events DROP COLUMN IF EXISTS metadata;" in text


def test_guard_specifics_20261112():
    """20261112 되돌림 파일의 가드 검증 항목 확인."""
    for path in (DOWN_20261112, ROLLBACK_20261112):
        text = path.read_text(encoding="utf-8")
        assert "code_review_requests" in text
        assert "pipeline_jobs" in text
        assert "revert_of_verified" in text
        assert "jsonb" in text
        # DROP 문에 public 스키마 명시
        assert "ALTER TABLE IF EXISTS public.code_review_requests DROP COLUMN IF EXISTS revert_of_verified;" in text
        assert "ALTER TABLE IF EXISTS public.pipeline_jobs DROP COLUMN IF EXISTS revert_of_verified;" in text


def test_no_cascade_usage():
    """되돌림 파일 4개 모두 DROP/ALTER 문에 CASCADE 키워드를 사용해서는 안 된다."""
    cascade_pattern = re.compile(r"\bCASCADE\b", re.IGNORECASE)
    for path in ALL_ROLLBACK_FILES:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue  # 주석 행 제외
            assert not cascade_pattern.search(stripped), (
                f"{path.name}:{idx} contains CASCADE outside comment: {line}"
            )


def test_no_server_copy():
    r"""되돌림 파일 4개 모두 서버 측 'COPY (' 구문을 포함하지 않고 클라이언트 \copy 를 써야 한다."""
    for path in ALL_ROLLBACK_FILES:
        text = path.read_text(encoding="utf-8")
        # 서버 측 COPY (SELECT ...) TO '/...' 패턴 차단
        assert not re.search(r"^\s*COPY\s*\(", text, re.MULTILINE | re.IGNORECASE), (
            f"{path.name} contains server-side COPY statement"
        )
        assert not re.search(r"--\s*COPY\s*\(", text, re.IGNORECASE), (
            f"{path.name} comment still uses server-side COPY (must use \\copy)"
        )
        assert "\\copy" in text, f"{path.name} must use \\copy in backup guidance"


def test_20261112_runner_rollback_explanation():
    """20261112 되돌림 파일 헤더는 scripts/pipeline-runner.sh 의 잔존 열 사용 사실과 롤백 절차를 명시해야 한다."""
    for path in (DOWN_20261112, ROLLBACK_20261112):
        text = path.read_text(encoding="utf-8")
        assert "pipeline-runner.sh" in text
        assert "status = 'running'" in text
        assert "1차 롤백" in text
        assert "wc -l" in text


def test_check_migration_txn_skips_rollback_files():
    """scripts/check_migration_txn.py 가 4개 되돌림 파일을 검사 대상에서 제외하고 exit 0 해야 한다."""
    from scripts import check_migration_txn as cmt

    for path in ALL_ROLLBACK_FILES:
        res = cmt.check_migration_file(path)
        assert res.skipped, f"{path.name} was not skipped by check_migration_txn"
        assert len(res.errors) == 0, f"{path.name} produced errors: {res.errors}"
