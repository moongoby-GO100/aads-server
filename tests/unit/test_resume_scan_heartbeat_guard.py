"""재개 스캐너가 살아 있는 턴을 가져가지 못하게 하는 계약.

2026-09-17. 응답 중이던 세션이 1분에 한 번씩 스캐너에 끌려가
"보존된 내용이 없습니다" 알림만 남긴 interrupted 실행을 반복 생성했다.
24시간 중단 70건 중 superseded 36건, 그중 23건이 본문 없이 끝났다.

원인은 두 겹이다.
  1) lease_expires_at 이 한 번도 기록되지 않아 lease 가드가 항상 통과한다.
  2) 남은 판정이 updated_at·placeholder edited_at 뿐인데, 도구를 오래 도는
     턴은 그 둘이 갱신되지 않는 구간이 8초 문턱보다 길다.

heartbeat_at 은 스트림을 실제로 돌리는 컨테이너가 계속 갱신하므로,
그 값이 신선하면 재개 대상에서 빼야 한다. 이 파일은 그 가드가
질의에서 사라지지 않게 못을 박는다.
"""

from pathlib import Path


def _main_source() -> str:
    return Path("app/main.py").read_text(encoding="utf-8")


def test_resume_scan_skips_executions_with_fresh_heartbeat():
    source = _main_source()

    assert "te.heartbeat_at IS NULL" in source, (
        "재개 스캐너의 heartbeat 가드가 사라졌다 — "
        "살아 있는 턴이 다시 superseded 로 끌려간다"
    )
    assert (
        "OR te.heartbeat_at < NOW() - ($2::int * INTERVAL '1 second')" in source
    ), "heartbeat 문턱이 min_stale_seconds($2) 와 같은 기준을 써야 한다"


def test_heartbeat_guard_sits_inside_the_resume_scan_query():
    """가드가 다른 질의에 붙어 있으면 아무것도 막지 못한다."""
    source = _main_source()

    anchor = "OR te.lease_expires_at <= NOW()"
    assert anchor in source

    tail = source.split(anchor, 1)[1]
    head = tail.split("ORDER BY te.updated_at DESC", 1)[0]
    assert "te.heartbeat_at IS NULL" in head, (
        "heartbeat 가드가 재개 스캐너 질의의 WHERE 절 안에 있어야 한다"
    )


def test_heartbeat_guard_keeps_legacy_rows_resumable():
    """heartbeat_at 이 NULL 인 옛 행은 예전대로 재개 대상이어야 한다."""
    source = _main_source()

    idx = source.find("te.heartbeat_at IS NULL")
    assert idx != -1
    window = source[idx : idx + 200]
    assert "OR te.heartbeat_at <" in window, (
        "NULL 허용 가지 없이 heartbeat 만 보면 옛 행이 영영 재개되지 않는다"
    )
