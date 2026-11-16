"""OBYS 백필 승인 마이그레이션의 업무 DB(acct_business) 분류 및 트랜잭션 검사 제외 검증."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from scripts import check_migration_txn as cmt

_REPO = Path(__file__).resolve().parents[2]
_MIGRATION_REL = "migrations/20261104_obys_backfill_approval.sql"
_MIGRATION_PATH = _REPO / _MIGRATION_REL

# release_migration_approval 모듈 로드
_approval_spec = importlib.util.spec_from_file_location(
    "release_migration_approval",
    _REPO / "scripts/release_migration_approval.py",
)
_rma = importlib.util.module_from_spec(_approval_spec)
_approval_spec.loader.exec_module(_rma)


def test_obys_backfill_migration_exists():
    """마이그레이션 파일이 존재하고 본문이 보존되어 있어야 한다."""
    assert _MIGRATION_PATH.is_file(), f"파일 존재 필수: {_MIGRATION_PATH}"
    content = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "-- target=acct_business" in content, "머리 주석에 -- target=acct_business 포함 필수"
    assert "public.obys_backfill_approval" in content, "SQL 본문 테이블 정의 보존 확인"


def test_migration_target_returns_acct_business():
    """release_migration_approval.migration_target 이 'acct_business' 를 반환해야 한다."""
    assert _MIGRATION_REL in _rma.MIGRATION_TARGETS
    assert _rma.MIGRATION_TARGETS[_MIGRATION_REL] == "acct_business"
    assert _rma.migration_target(_MIGRATION_REL) == "acct_business"


def test_check_migration_txn_skips_obys_backfill():
    """check_migration_txn 이 OBYS 백필 마이그레이션을 건너뛰어야 한다(skipped=True, rc=0)."""
    assert _MIGRATION_REL in cmt.KNOWN_ACCT_BUSINESS_MIGRATIONS

    res = cmt.check_migration_file(_MIGRATION_PATH)
    assert res.skipped, "업무 DB 마이그레이션은 건너뛰어야 함"
    assert res.skip_reason == "business db migration deferred"
    assert len(res.errors) == 0, f"오류 없어야 함: {res.errors}"
    assert len(res.warnings) == 0, f"경고 없어야 함: {res.warnings}"

    rc = cmt.main([str(_MIGRATION_PATH)])
    assert rc == 0, f"check_migration_txn main() 종료코드 0이어야 함 (실제: {rc})"
