"""라우팅 정본 단일화 정리 SQL 초안의 정적 검사 (AADS-ROUTING-SSOT-DESIGN-20261002).

초안은 절대 실행되면 안 된다. 이 테스트는 DB 없이 파일만 보고 다음을 지킨다.
- 초안 이름이 _draft 로 끝난다.
- 롤백 짝이 있다.
- 행 제거(DROP/TRUNCATE/DELETE) 문장이 없다.
- 릴리스 도구의 자동 적용 대상(migrations/*.sql)에 초안이 섞여 있지 않다.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
DRAFT_DIR = MIGRATIONS / "drafts"
FORWARD = DRAFT_DIR / "20261002_routing_ssot_cleanup.sql_draft"
ROLLBACK = DRAFT_DIR / "20261002_routing_ssot_cleanup_rollback.sql_draft"
FORBIDDEN = re.compile(r"\b(DROP|TRUNCATE|DELETE)\b", re.IGNORECASE)


def _code_only(path: Path) -> str:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("--")]
    return "\n".join(lines)


def test_draft_names_end_with_draft():
    for path in (FORWARD, ROLLBACK):
        assert path.exists(), f"missing {path}"
        assert path.name.endswith("_draft"), path.name


def test_rollback_pair_exists():
    assert FORWARD.name.replace(".sql_draft", "") + "_rollback.sql_draft" == ROLLBACK.name
    assert ROLLBACK.exists()
    assert "rollback" in ROLLBACK.name


def test_drafts_have_no_destructive_statements():
    for path in (FORWARD, ROLLBACK):
        match = FORBIDDEN.search(_code_only(path))
        assert match is None, f"{path.name}: forbidden statement {match.group(0)}"


def test_draft_is_not_auto_applied_by_release_tooling():
    """apply_release_migrations.sh / deploy_release_assets.sh 는 migrations/ 아래 *.sql 을 자동 적용한다."""
    offenders = [
        str(p.relative_to(ROOT))
        for p in MIGRATIONS.rglob("*.sql")
        if "routing_ssot" in p.name
    ]
    assert offenders == [], f"routing_ssot draft must stay *.sql_draft: {offenders}"


def test_updates_are_guarded_and_backed_up():
    code = _code_only(FORWARD)
    updates = re.findall(r"\bUPDATE\b.*?;", code, flags=re.IGNORECASE | re.DOTALL)
    assert updates, "no UPDATE found"
    for stmt in updates:
        assert re.search(r"\bWHERE\b", stmt, flags=re.IGNORECASE), f"UPDATE without WHERE: {stmt[:80]}"
    assert "routing_ssot_backup_20261002" in code
    assert "routing_ssot_backup_20261002" in _code_only(ROLLBACK)


def test_sentinel_qwen_turbo_code_constant_not_targeted():
    """qwen-turbo 는 자동 라우팅 표식이다. 초안은 DB 행의 기본값만 바꾸고 상수는 건드리지 않는다."""
    assert "_AUTO_ROUTED_DB_DEFAULT_MODELS" not in _code_only(FORWARD)
