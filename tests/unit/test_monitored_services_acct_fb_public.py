"""단위 테스트: cafe24_114 acct-fb-public 상시 감시 등록 및 비파괴 멱등성 검증.

M5 완료 기준:
- monitored_services 상시 감시 등록 (코드/시드/마이그레이션)
- service_name: 'acct-fb-public'
- server: 'cafe24_114' (정규 서버 ID)
- check_type: 'http_health'
- check_target: 'https://fb.newtalk.kr/health/live'
- check_interval: 60, timeout: 10, enabled: true
- auto_recovery_command: NULL (원격 서버 복구는 CEO 승인 대상)
- 멱등성: ON CONFLICT (server, service_name) DO NOTHING
- 비파괴성: DELETE, UPDATE, DROP 금지
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATION_FILE = REPO_ROOT / "migrations" / "20261116_monitored_services_acct_fb_public.sql"
INIT_SCHEMA_FILE = REPO_ROOT / "scripts" / "init_memory_schema.sql"


def test_migration_file_exists():
    """마이그레이션 파일 존재 여부 확인."""
    assert MIGRATION_FILE.exists(), f"마이그레이션 파일 누락: {MIGRATION_FILE}"
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    assert len(content.strip()) > 0, "마이그레이션 파일이 비어 있습니다."


def test_migration_values_and_idempotency():
    """마이그레이션 SQL 의 필드값, 멱등성 및 auto_recovery=NULL 검증."""
    content = MIGRATION_FILE.read_text(encoding="utf-8")

    # 테이블 및 컬럼 확인
    assert "INSERT INTO monitored_services" in content

    # server: cafe24_114
    assert "'cafe24_114'" in content, "server 는 정규 명칭 cafe24_114 여야 합니다."

    # service_name: acct-fb-public
    assert "'acct-fb-public'" in content, "service_name 은 논리 이름 acct-fb-public 여야 합니다."

    # check_type: http_health
    assert "'http_health'" in content, "check_type 은 http_health 여야 합니다."

    # check_target: https://fb.newtalk.kr/health/live
    assert "'https://fb.newtalk.kr/health/live'" in content, "check_target URL 이 일치해야 합니다."

    # check_interval: 60, timeout: 10
    assert re.search(r"\b60\b", content), "check_interval 은 60 이어야 합니다."
    assert re.search(r"\b10\b", content), "timeout 은 10 이어야 합니다."

    # auto_recovery_command: NULL
    assert re.search(r"\bNULL\b", content, re.IGNORECASE), "auto_recovery_command 는 NULL 이어야 합니다."

    # enabled: TRUE
    assert re.search(r"\bTRUE\b", content, re.IGNORECASE), "enabled 는 TRUE 여야 합니다."

    # 멱등성: ON CONFLICT (server, service_name) DO NOTHING
    conflict_match = re.search(
        r"ON\s+CONFLICT\s*\(\s*server\s*,\s*service_name\s*\)\s*DO\s+NOTHING",
        content,
        re.IGNORECASE,
    )
    assert conflict_match is not None, "ON CONFLICT (server, service_name) DO NOTHING 제약이 필요합니다."


def test_migration_is_non_destructive():
    """파괴적 변경(DELETE, DROP, TRUNCATE, UPDATE)이 없음을 검증."""
    content = MIGRATION_FILE.read_text(encoding="utf-8")
    # 주석 제거 후 SQL 문 분석
    sql_lines = [
        line for line in content.splitlines()
        if not line.strip().startswith("--")
    ]
    sql_body = " ".join(sql_lines).upper()

    assert "DROP TABLE" not in sql_body
    assert "DROP COLUMN" not in sql_body
    assert "TRUNCATE" not in sql_body
    assert "DELETE FROM" not in sql_body
    assert "UPDATE " not in sql_body

    # 주석에 비파괴 근거 기재 여부
    assert "비파괴 근거" in content


def test_init_memory_schema_seed():
    """init_memory_schema.sql 에도 멱등 시드가 포함되어 있는지 검증."""
    assert INIT_SCHEMA_FILE.exists()
    content = INIT_SCHEMA_FILE.read_text(encoding="utf-8")

    # unique index 확인
    assert "idx_monitored_services_unique" in content
    assert "ON monitored_services(server, service_name)" in content

    # seed 삽입문 확인
    assert "INSERT INTO monitored_services" in content
    assert "'cafe24_114'" in content
    assert "'acct-fb-public'" in content
    assert "'https://fb.newtalk.kr/health/live'" in content
    assert "ON CONFLICT (server, service_name) DO NOTHING" in content


def test_watchdog_and_healer_compatibility():
    """watchdog 및 unified_healer 의 http_health 지원 여부 검증."""
    healer_file = REPO_ROOT / "app" / "services" / "unified_healer.py"
    assert healer_file.exists()
    healer_src = healer_file.read_text(encoding="utf-8")

    # unified_healer 에서 http_health 지원 확인
    assert "http_health" in healer_src, "unified_healer._check_service 에서 http_health 를 처리해야 합니다."

    # server_registry 에서 cafe24_114 정의 확인
    registry_file = REPO_ROOT / "app" / "services" / "server_registry.py"
    assert registry_file.exists()
    registry_src = registry_file.read_text(encoding="utf-8")
    assert "cafe24_114" in registry_src
