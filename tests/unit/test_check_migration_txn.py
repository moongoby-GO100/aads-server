"""마이그레이션 내부 트랜잭션 및 잠금 제한 검사 단위 테스트.

테스트 대상:
  - 최상위 BEGIN;/COMMIT; 있는 새 파일 → 차단
  - DO $$ BEGIN … END $$ 만 있는 파일 → 통과
  - 주석 안 -- BEGIN; → 통과
  - rollback/ 아래 파일 → 검사 제외
  - 업무 DB 표기 파일 → 건너뜀
  - lock_timeout 없음 → 경고만, 종료코드 0
  - 현재 origin/main 의 migrations/20261106_goal_retry_exhaust_requests.sql → 통과
  - 현재 migrations/20261111_canonical_document_approval_delegation.sql → 통과
  - pre-commit hook 연동 및 우회 변수 부재 확인
"""
from __future__ import annotations

from pathlib import Path

from scripts import check_migration_txn as cmt

_REPO = Path(__file__).resolve().parents[2]


def test_top_level_begin_commit_blocked(tmp_path: Path):
    """최상위 BEGIN;/COMMIT; 문장이 있는 파일은 차단되고 정확한 메시지를 출력해야 한다."""
    sql = """-- 신규 테이블 생성
BEGIN;

SET LOCAL lock_timeout = '5s';
CREATE TABLE test_table (
    id uuid PRIMARY KEY
);

COMMIT;
"""
    sql_file = tmp_path / "20261112_test_begin_commit.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert not res.skipped
    assert len(res.errors) == 2, f"BEGIN과 COMMIT 각각 1건씩 2건 오류 예상: {res.errors}"

    # 행 번호 및 메시지 확인
    assert f"{sql_file}:2:" in res.errors[0]
    assert cmt.BLOCK_MESSAGE in res.errors[0]
    assert f"{sql_file}:9:" in res.errors[1]
    assert cmt.BLOCK_MESSAGE in res.errors[1]

    # CLI main() 실행 시 종료코드 1
    rc = cmt.main([str(sql_file)])
    assert rc == 1


def test_top_level_begin_only_blocked(tmp_path: Path):
    """최상위 BEGIN; 만 있는 파일도 차단되어야 한다."""
    sql = """BEGIN;
CREATE TABLE test_single (id int);
"""
    sql_file = tmp_path / "20261112_begin_only.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert len(res.errors) == 1
    assert f"{sql_file}:1:" in res.errors[0]
    assert cmt.BLOCK_MESSAGE in res.errors[0]

    assert cmt.main([str(sql_file)]) == 1


def test_start_transaction_and_commit_work_blocked(tmp_path: Path):
    """START TRANSACTION 및 COMMIT WORK 구문도 차단되어야 한다."""
    sql = """START TRANSACTION;
CREATE TABLE test_variant (id int);
COMMIT WORK;
"""
    sql_file = tmp_path / "20261112_variant.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert len(res.errors) == 2
    assert cmt.BLOCK_MESSAGE in res.errors[0]
    assert cmt.BLOCK_MESSAGE in res.errors[1]
    assert cmt.main([str(sql_file)]) == 1


def test_do_block_begin_allowed(tmp_path: Path):
    """DO $$ BEGIN ... END $$ 만 있는 파일은 통과해야 한다 (오탐 금지)."""
    sql = """DO $$
BEGIN
    IF to_regclass('public.yeoljeong_contracts') IS NULL THEN
        RAISE EXCEPTION '테넌트 계약 테이블 없음';
    END IF;
END
$$;
"""
    sql_file = tmp_path / "20261112_do_block.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert not res.skipped
    assert len(res.errors) == 0, f"오류 없어야 함: {res.errors}"
    assert len(res.warnings) == 0, f"경고 없어야 함(DDL 없음): {res.warnings}"

    # CLI main() 실행 시 종료코드 0
    assert cmt.main([str(sql_file)]) == 0


def test_do_block_with_custom_tag_and_ddl_allowed(tmp_path: Path):
    """태그가 지정된 DO 블록 및 SET LOCAL 이후의 DDL 은 통과해야 한다."""
    sql = """DO $do$
BEGIN
    PERFORM 1;
END
$do$;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS test_after_do (
    id int
);
"""
    sql_file = tmp_path / "20261112_do_custom_tag.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert len(res.errors) == 0
    assert len(res.warnings) == 0
    assert cmt.main([str(sql_file)]) == 0


def test_comment_begin_allowed(tmp_path: Path):
    """주석 안의 -- BEGIN; 및 /* BEGIN; */ 은 통과해야 한다."""
    sql = """-- 트랜잭션 주석:
-- BEGIN;
-- 적용기가 트랜잭션을 감싸므로 파일 안에 두지 않는다.
/*
 * 다중행 주석:
 * BEGIN;
 * COMMIT;
 */
SET LOCAL lock_timeout = '5s';
CREATE TABLE test_comment (
    id int
);
"""
    sql_file = tmp_path / "20261112_comment_begin.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert len(res.errors) == 0, f"오류 없어야 함: {res.errors}"
    assert len(res.warnings) == 0, f"경고 없어야 함(DDL 없음): {res.warnings}"
    assert cmt.main([str(sql_file)]) == 0


def test_string_literal_begin_allowed(tmp_path: Path):
    """문자열 리터럴 안의 'BEGIN;' 은 통과해야 한다."""
    sql = """SET LOCAL lock_timeout = '5s';
INSERT INTO audit_logs (message) VALUES ('BEGIN;');
"""
    res = cmt.check_migration_text(sql, path="test.sql")
    assert len(res.errors) == 0


def test_rollback_file_excluded(tmp_path: Path):
    """rollback/ 아래 파일은 검사에서 제외(skipped)되어야 한다."""
    rollback_dir = tmp_path / "migrations" / "rollback"
    rollback_dir.mkdir(parents=True)
    sql_file = rollback_dir / "20261112_test.down.sql"
    sql_file.write_text("BEGIN;\nDROP TABLE IF EXISTS test_table;\nCOMMIT;\n", encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert res.skipped
    assert "rollback" in res.skip_reason.lower()
    assert len(res.errors) == 0
    assert len(res.warnings) == 0

    assert cmt.main([str(sql_file)]) == 0


def test_business_db_header_skipped(tmp_path: Path):
    """머리 주석에 '업무 DB' 표기가 있는 파일은 건너뛰어야 한다."""
    sql = """-- 업무 DB obys 전용. AADS DB에는 적용하지 않는다.
BEGIN;
CREATE TABLE yeoljeong_test (id int);
COMMIT;
"""
    sql_file = tmp_path / "20261112_obys_test.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert res.skipped
    assert "business db" in res.skip_reason.lower()
    assert len(res.errors) == 0
    assert len(res.warnings) == 0

    assert cmt.main([str(sql_file)]) == 0


def test_target_acct_business_header_skipped(tmp_path: Path):
    """머리 주석에 'target=acct_business' 표기가 있는 파일은 건너뛰어야 한다."""
    sql = """-- target=acct_business
BEGIN;
CREATE TABLE acct_test (id int);
COMMIT;
"""
    sql_file = tmp_path / "20261112_acct_test.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert res.skipped
    assert len(res.errors) == 0

    assert cmt.main([str(sql_file)]) == 0


def test_missing_lock_timeout_warning_only(tmp_path: Path):
    """첫 DDL 앞에 lock_timeout 이 없으면 경고만 발생하고 종료코드는 0이어야 한다."""
    sql = """-- 마이그레이션 예제
CREATE TABLE test_no_timeout (
    id int
);
"""
    sql_file = tmp_path / "20261112_no_timeout.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert not res.skipped
    assert len(res.errors) == 0, "오류가 아니어야 함"
    assert len(res.warnings) == 1, "경고 1건이어야 함"
    assert "첫 DDL 앞에 SET LOCAL lock_timeout 이 없습니다" in res.warnings[0]

    # 종료코드 0 확인
    assert cmt.main([str(sql_file)]) == 0


def test_lock_timeout_after_first_ddl_warns(tmp_path: Path):
    """lock_timeout 이 첫 DDL 뒤에 나오면 경고가 발생해야 한다."""
    sql = """CREATE TABLE first_table (id int);
SET LOCAL lock_timeout = '5s';
CREATE TABLE second_table (id int);
"""
    sql_file = tmp_path / "20261112_timeout_after_ddl.sql"
    sql_file.write_text(sql, encoding="utf-8")

    res = cmt.check_migration_file(sql_file)
    assert len(res.errors) == 0
    assert len(res.warnings) == 1
    assert cmt.main([str(sql_file)]) == 0


def test_real_migration_20261106_passes():
    """현재 origin/main 의 migrations/20261106_goal_retry_exhaust_requests.sql 은 통과해야 한다."""
    path = _REPO / "migrations" / "20261106_goal_retry_exhaust_requests.sql"
    assert path.is_file(), f"파일 존재 확인: {path}"

    res = cmt.check_migration_file(path)
    assert not res.skipped
    assert len(res.errors) == 0, f"오류 없어야 함: {res.errors}"
    assert len(res.warnings) == 0, f"경고 없어야 함: {res.warnings}"

    rc = cmt.main([str(path)])
    assert rc == 0


def test_real_migration_20261111_passes():
    """현재 origin/main 의 migrations/20261111_canonical_document_approval_delegation.sql 은 통과해야 한다."""
    path = _REPO / "migrations" / "20261111_canonical_document_approval_delegation.sql"
    assert path.is_file(), f"파일 존재 확인: {path}"

    res = cmt.check_migration_file(path)
    assert not res.skipped
    assert len(res.errors) == 0, f"오류 없어야 함: {res.errors}"
    assert len(res.warnings) == 0, f"경고 없어야 함: {res.warnings}"

    rc = cmt.main([str(path)])
    assert rc == 0


def test_pre_commit_hook_wiring_and_no_bypass():
    """scripts/hooks/pre-commit 이 check_migration_txn.py 를 부르고 우회 변수가 없어야 한다."""
    hook = (_REPO / "scripts" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "scripts/check_migration_txn.py" in hook, "pre-commit hook 에 check_migration_txn.py 연결 필수"
    assert "ALLOW_MIGRATION" not in hook, "마이그레이션 트랜잭션 우회 변수는 없어야 함"
    assert "exit 1" in hook, "검증 실패 시 차단 필수"
