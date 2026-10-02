"""L1 문서 저장 규칙(R-DOC) 초안 마이그레이션 정적 검사 (DB 불필요)."""
import re
from pathlib import Path

MIG_DIR = Path(__file__).resolve().parents[2] / "migrations"
UP = MIG_DIR / "20261003_l1_doc_storage_rule_draft.sql"
DOWN = MIG_DIR / "20261003_l1_doc_storage_rule_draft_rollback.sql"
SLUG = "l1-doc-storage-rule"


def _up() -> str:
    return UP.read_text(encoding="utf-8")


def _content() -> str:
    m = re.search(r"\$\$(.*?)\$\$", _up(), re.S)
    assert m, "content 달러 인용 블록을 찾지 못함"
    return m.group(1)


def _strip_comments(sql: str) -> str:
    return "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))


def _values_tail() -> str:
    """content 블록 뒤의 컬럼 값(스코프·priority·enabled·created_by)."""
    return _up().split("$$")[-1]


def test_files_exist():
    assert UP.is_file()
    assert DOWN.is_file()


def test_slug_and_layer():
    sql = _up()
    assert f"'{SLUG}'" in sql
    head = sql.split("$$")[0]
    assert re.search(r"'\s*,\s*1\s*,\s*$", head.rstrip()), "layer_id 는 1 이어야 함"


def test_disabled_and_scope():
    tail = _values_tail()
    assert re.search(r"\b90\s*,\s*false\s*,", tail), "priority 90, enabled=false 여야 함"
    assert "true" not in tail.lower()
    assert tail.count("ARRAY['*']") == 4
    assert "runner_" in tail


def test_priority_below_handover_asset():
    m = re.search(r"\b(\d+)\s*,\s*false\s*,", _values_tail())
    assert m and int(m.group(1)) < 95


def test_idempotent_clause():
    sql = _strip_comments(_up())
    assert "ON CONFLICT (slug) DO NOTHING" in sql
    assert "DO UPDATE" not in sql


def test_no_other_table_writes():
    sql = _strip_comments(_up()).upper()
    assert sql.count("INSERT INTO") == 1
    assert "UPDATE " not in sql.replace("DO UPDATE", "")
    assert "DELETE" not in sql


def test_content_length_and_keywords():
    c = _content()
    assert len(c) <= 1500, len(c)
    for kw in ("docs/reports", "document_key", "R-HANDOVER-DB", "/api/v1/projects/", "왜:"):
        assert kw in c, kw


def test_content_has_no_secret_like_tokens():
    assert "sk-ant" not in _content()


def test_rollback_only_deletes_disabled_draft():
    sql = _strip_comments(DOWN.read_text(encoding="utf-8"))
    stmts = [s for s in re.split(r";", sql) if "DELETE" in s.upper()]
    assert len(stmts) == 1
    s = " ".join(stmts[0].split())
    assert f"slug = '{SLUG}'" in s
    assert "AND enabled = false" in s
    assert "DROP" not in sql.upper()
    assert "UPDATE" not in sql.upper()
