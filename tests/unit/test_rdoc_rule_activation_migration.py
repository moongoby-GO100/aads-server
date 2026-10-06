"""R-DOC 규칙 활성화 마이그레이션·롤백 정적 검사(DB 불필요).

동작 검증(적용·재적용·롤백·동시변경 중단)은 스크래치 Postgres 에서 따로 수행했고 결과는 RESULT 문서에 있다.
여기서는 안전장치 문구가 사라지지 않도록 고정한다.
"""
import re
from pathlib import Path

MIG = Path(__file__).resolve().parents[2] / "migrations"
UP = MIG / "20261006_l1_doc_storage_rule_activate.sql"
DOWN = MIG / "rollback" / "20261006_l1_doc_storage_rule_activate.down.sql"
ORIGINAL_MD5 = "e2fcbb594daa8089004b2f9d6b025d73"


def _sql(path):
    return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("--"))


def test_activation_is_guarded_and_transactional():
    sql = _sql(UP)
    assert sql.count("BEGIN;") == 1 and sql.rstrip().endswith("COMMIT;")
    assert "FOR UPDATE" in sql
    assert ORIGINAL_MD5 in sql
    assert "RAISE EXCEPTION" in sql
    assert "prompt_asset_versions" in sql and "prompt_rdoc_activation_backup" in sql
    assert "DELETE" not in sql.upper().replace("DELETE FROM PROMPT_ASSET_VERSIONS", "")


def test_activation_touches_only_the_one_asset():
    sql = _sql(UP)
    updates = re.findall(r"^\s*UPDATE\s+(\w+)", sql, re.I | re.M)
    assert updates == ["prompt_assets"]
    assert sql.count("l1-doc-storage-rule") >= 2
    assert "WHERE id = cur.id" in sql


def test_activated_content_states_only_true_things():
    body = re.search(r"\$rule\$(.*?)\$rule\$", UP.read_text(encoding="utf-8"), re.S).group(1)
    assert "YYYYMMDD_{PROJECT}_{한글 제목}.md" in body
    assert "열람 링크 미제공" in body and "정본 미등록(미완료)" in body
    assert "파일 경로를 링크처럼 적지 않는다" in body
    assert "승인은 별도 승인 경로" in body


def test_rollback_refuses_to_overwrite_later_edits():
    sql = _sql(DOWN)
    assert "applied_md5" in sql and "RAISE EXCEPTION" in sql and "FOR UPDATE" in sql
    for column in ("content", "title", "enabled", "priority", "workspace_scope", "intent_scope", "target_models", "role_scope"):
        assert re.search(rf"\b{column}\s*=", sql), column
