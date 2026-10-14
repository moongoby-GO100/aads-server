"""migrations/ 의 yeoljeong_* 테이블 변경은 to_regclass 가드 뒤에 있어야 한다.

aads DB 에는 yeoljeong_* 테이블이 없다. 가드 없는 마이그레이션이 PENDING 으로 들어오면
apply_release_migrations.sh 가 schema_migrations 단계에서 멈추고 API 블루그린 릴리스 전체가 선다.
2026-10-06(20261006_obys_payroll_email_nullable) 과 2026-10-14(20261014_obys_journal_source_clobe,
deploy_run 5747) 에 같은 사고가 두 번 났다. 규칙으로만 남기면 재발하므로 이 테스트가 막는다.
"""
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATIONS = ROOT / "migrations"
BASELINE = ROOT / "scripts" / "migrations_auto_apply_baseline.txt"

_TARGET = r"(?:ONLY\s+)?(?:public\.)?yeoljeong_"
_MUTATION = re.compile(
    r"\b(?:ALTER\s+TABLE(?:\s+IF\s+EXISTS)?"
    r"|UPDATE"
    r"|INSERT\s+INTO"
    r"|DELETE\s+FROM"
    r"|CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+CONCURRENTLY)?(?:\s+IF\s+NOT\s+EXISTS)?\s+\w+\s+ON"
    r")\s+" + _TARGET,
    re.IGNORECASE,
)
_GUARD = re.compile(r"to_regclass\s*\(\s*'(?:public\.)?yeoljeong_", re.IGNORECASE)

# 가드가 없지만 aads DB 에 자동 적용되지 않는 파일. 새 파일은 여기에 추가하지 말고 가드를 넣는다.
UNGUARDED_ALLOWED = {
    # aads schema_migrations 에 source=backfill 로 기록돼 있다 → SKIP/DRIFT 로 재실행되지 않는다.
    "113_yeoljeong_finance_settings.sql": "ledger backfill",
    "115_yeoljeong_finance_hr_ledgers.sql": "ledger backfill",
    "116_yeoljeong_finance_delivery_ledgers.sql": "ledger backfill",
    "127_yeoljeong_delivery_ads.sql": "ledger backfill",
    "153_yeoljeong_delivery_empty_shell_quarantine.sql": "ledger backfill",
    # scripts/migrations_auto_apply_baseline.txt 에 올라 있다 → HOLD 로 자동 적용되지 않는다.
    "20260919_obys_bank_ledger_details.sql": "baseline HOLD",
    "20260919_obys_ledger_details_r2.sql": "baseline HOLD",
    "20260919_obys_tenant_uploads.sql": "baseline HOLD",
    "20260919_yeoljeong_hr_tenant_isolation.sql": "baseline HOLD",
    "20260921_obys_journal_vouchers_r3.sql": "baseline HOLD",
}


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _forward_migrations():
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name.endswith(".verify.sql") or "rollback" in path.name:
            continue
        yield path


def _unguarded(path: Path) -> bool:
    code = _code(path.read_text(encoding="utf-8"))
    mutation = _MUTATION.search(code)
    if mutation is None:
        return False
    guard = _GUARD.search(code)
    return guard is None or guard.start() > mutation.start()


def test_yeoljeong_mutations_are_guarded_by_to_regclass():
    offenders = [
        path.name
        for path in _forward_migrations()
        if _unguarded(path) and path.name not in UNGUARDED_ALLOWED
    ]

    assert not offenders, (
        "yeoljeong_* 테이블을 바꾸는데 앞선 to_regclass('public.yeoljeong_…') 가드가 없다: "
        f"{offenders}. 20261006_obys_payroll_email_nullable.sql 처럼 DO $mig$ … RETURN; … EXECUTE 로 감싸라."
    )


def test_allowlist_has_no_stale_entries():
    names = {path.name: path for path in _forward_migrations()}

    for name in UNGUARDED_ALLOWED:
        assert name in names, f"허용 목록의 {name} 이 migrations/ 에 없다 — 항목을 지워라"
        assert _unguarded(names[name]), f"{name} 은 이미 가드가 있거나 변경문이 없다 — 항목을 지워라"


def test_baseline_allowlist_entries_are_really_in_baseline():
    baseline = {
        line.split()[0]
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    for name, reason in UNGUARDED_ALLOWED.items():
        if reason == "baseline HOLD":
            assert f"migrations/{name}" in baseline, f"{name} 이 baseline 에 없다"


def test_detector_flags_an_unguarded_statement(tmp_path):
    bad = tmp_path / "x.sql"
    bad.write_text("BEGIN;\nALTER TABLE yeoljeong_foo DROP CONSTRAINT c;\nCOMMIT;\n", encoding="utf-8")
    late = tmp_path / "y.sql"
    late.write_text(
        "UPDATE yeoljeong_foo SET a = 1;\nSELECT to_regclass('public.yeoljeong_foo');\n",
        encoding="utf-8",
    )
    ok = tmp_path / "z.sql"
    ok.write_text(
        "DO $mig$ BEGIN IF to_regclass('public.yeoljeong_foo') IS NULL THEN RETURN; END IF;\n"
        "EXECUTE 'ALTER TABLE yeoljeong_foo ADD COLUMN a int'; END $mig$;\n",
        encoding="utf-8",
    )
    commented = tmp_path / "c.sql"
    commented.write_text("-- ALTER TABLE yeoljeong_foo ADD COLUMN a int\nSELECT 1;\n", encoding="utf-8")

    assert _unguarded(bad)
    assert _unguarded(late)
    assert not _unguarded(ok)
    assert not _unguarded(commented)
