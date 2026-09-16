from pathlib import Path


def test_execution_schema_has_fenced_lease_and_model_override():
    migration = Path("migrations/140_chat_execution_lease_and_deferred_reactions.sql").read_text(encoding="utf-8")

    assert "owner_instance" in migration
    assert "owner_epoch" in migration
    assert "heartbeat_at" in migration
    assert "lease_expires_at" in migration
    assert "resume_model_override" in migration


def test_resume_scanner_claim_does_not_consume_attempt_budget():
    main = Path("app/main.py").read_text(encoding="utf-8")
    service = Path("app/services/chat_service.py").read_text(encoding="utf-8")

    scanner_claim = main.split("owner_epoch = await _claim_execution_lease_exec", 1)[1].split(
        "placeholder_id = row", 1
    )[0]
    assert "retry_count = retry_count + 1" not in scanner_claim
    assert "resume_model_attempt_started" in service
    assert "AND retry_count < $5" in service


def test_inactive_slot_uses_durable_reaction_handoff():
    main = Path("app/main.py").read_text(encoding="utf-8")
    service = Path("app/services/chat_service.py").read_text(encoding="utf-8")

    assert "chat_deferred_reactions" in service
    assert "_is_local_active_api_slot" in service
    assert "_periodic_deferred_reaction_handoff" in main


def test_deferred_reaction_claim_skips_exhausted_attempts():
    service = Path("app/services/chat_service.py").read_text(encoding="utf-8")
    handler = service.split("async def _process_deferred_reactions_once", 1)[1].split(
        "async def _consume_next_reaction", 1
    )[0]
    # 파일 전체가 아니라 handler 안에서 잘라낸다. 파일 첫 번째 'WITH candidates AS ('
    # 는 실행 해소 쿼리(94d5d508)라 엉뚱한 곳을 검사하게 된다 — 그래서 alias 가
    # attempts → q.attempts 로 바뀌었을 때(e201f9f2, 09-15) 이 테스트가 깨진 채
    # 이틀을 갔다. 동작은 멀쩡했고 잘라내는 기준만 낡았다.
    claim_query = handler.split("WITH candidates AS (", 1)[1].split(
        "UPDATE chat_deferred_reactions q", 1
    )[0]

    assert "deferred reaction retry budget exhausted" in service
    assert "AND attempts >= 8" in service
    assert "WHERE q.attempts < 8" in claim_query
    assert "ORDER BY q.created_at" in claim_query
    assert handler.count("attempts = GREATEST(attempts - 1, 0)") >= 2


def test_deferred_reaction_completion_survives_bluegreen_owner_change():
    service = Path("app/services/chat_service.py").read_text(encoding="utf-8")
    handler = service.split("async def _process_deferred_reactions_once", 1)[1].split(
        "async def _consume_next_reaction", 1
    )[0]
    completion = handler.split("def _finish_deferred_reaction", 1)[1].split(
        "task.add_done_callback", 1
    )[0]

    assert "WHERE id = $1 AND completed_at IS NULL" in completion
    assert "claimed_by" not in completion
    assert "_EXECUTION_OWNER_INSTANCE" not in completion

    # Returning an unstarted claim to pending must remain owner-fenced.
    before_completion = handler.split("def _finish_deferred_reaction", 1)[0]
    assert before_completion.count("WHERE id = $1 AND claimed_by = $2") >= 2


def test_bluegreen_deploy_builds_once_and_starts_without_build():
    deploy = Path("deploy.sh").read_text(encoding="utf-8")
    compose = Path("docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "release image 1회 빌드" in deploy
    assert "up -d --no-build --no-deps" in deploy
    assert "active/standby image digest mismatch" in deploy
    assert 'git -C "$COMPOSE_DIR" archive --format=tar HEAD' in deploy
    assert compose.count("image: aads-server:${AADS_RELEASE_SHA:-local}") == 2


def test_bluegreen_api_slots_mount_live_project_docs_readonly():
    compose = Path("docker-compose.prod.yml").read_text(encoding="utf-8")
    blue = compose.split("  aads-server:", 1)[1].split("  aads-server-green:", 1)[0]
    green = compose.split("  aads-server-green:", 1)[1].split("  yeoljeong-finance:", 1)[0]

    for service in (blue, green):
        assert "/root/aads/aads-server/docs:/app/docs:ro" in service
        assert "/root/aads/aads-server/reports:/app/reports:ro" in service


def test_placeholder_repair_uses_the_actual_assistant_execution_unique_index():
    router = Path("app/routers/chat.py").read_text(encoding="utf-8")
    helper = router.split("async def _ensure_running_placeholder_anchor", 1)[1].split(
        "async def ", 1
    )[0]

    assert "AND role = 'assistant'" in helper
    assert "ON CONFLICT (session_id)" in helper
    assert "WHERE intent = 'streaming_placeholder'" in helper
    assert "DO UPDATE" in helper
    assert "SET execution_id = EXCLUDED.execution_id" in helper
    assert "WHERE chat_messages.role = 'assistant'" in helper
    assert "interrupted_partial" in helper


# ---------------------------------------------------------------------------
# 버려진 실행 수거 (chat.execution_never_reaped, 2026-09-17, 고침 8dac0870)
#
# 세 겹 중 하나라도 빠지면 며칠 묵은 실행이 되살아나 새 턴을 막는다. 실제로
# 2026-09-16 에 스캐너 조회만 막았다가 다른 호출자가 2초 만에 되살린 전례가
# 있다. 아래 세 테스트는 그 세 겹이 지워지지 않았는지만 본다.
# ---------------------------------------------------------------------------


def test_resume_scanner_skips_executions_older_than_age_cap():
    """스캐너 조회는 started_at 으로 나이를 잰다.

    updated_at 은 재개될 때마다 갱신돼 나이 상한으로 쓸 수 없다 — 그것이
    기존 `updated_at > NOW()-2h` 창이 오래된 실행을 걸러내지 못한 이유다.
    """
    main = Path("app/main.py").read_text(encoding="utf-8")

    assert 'AADS_EXECUTION_MAX_AGE_HOURS' in main
    assert "AND te.started_at > NOW() - ($6::int * INTERVAL '1 hour')" in main


def test_execution_lease_claim_refuses_stale_and_high_epoch():
    """소유권을 주는 자리에도 같은 상한이 있어야 한다.

    조회만 막으면 _claim_execution_lease 를 직접 부르는 다른 경로(호출부
    여섯 군데)가 그대로 되살린다. 수동 재개(allow_any_epoch)만 예외다.
    """
    service = Path("app/services/chat_service.py").read_text(encoding="utf-8")
    claim = service.split("async def _claim_execution_lease", 1)[1].split(
        "\nasync def ", 1
    )[0]

    assert "AND ($6::boolean OR COALESCE(owner_epoch, 0) < $7::int)" in claim
    assert "AND ($6::boolean OR started_at > NOW() - ($8::int * INTERVAL '1 hour'))" in claim
    assert "_EXECUTION_MAX_AGE_HOURS" in claim


def test_reaper_terminates_abandoned_executions_without_rewriting_history():
    """수거기는 cancelled 로 내리고, interrupted 는 건드리지 않는다.

    - interrupted 로 내리면 _claim_execution_lease 의 WHERE 에 걸려 되살아난다.
    - 수거 대상에 interrupted 를 넣으면 과거 기록 5,580건(최고 145일)을 다시 쓴다.
    - 자리표시자를 지우면 증상이 "버블이 아예 안 나온다" 로 되돌아간다.
    """
    main = Path("app/main.py").read_text(encoding="utf-8")
    reaper = main.split("async def _reap_abandoned_executions_once", 1)[1].split(
        "\n    async def ", 1
    )[0]

    assert "SET status = 'cancelled'" in reaper
    assert "WHERE status IN ('running', 'retrying')" in reaper
    assert "AND started_at < NOW() - ($1::int * INTERVAL '1 hour')" in reaper
    assert "DELETE FROM chat_messages" not in reaper
    # 나이만으로 자르면 정상 턴을 죽인다 — 2026-09-17 실측: 2시간을 넘겨 정상
    # 완료된 턴이 최근 30일 8건(최대 289.5분)이고 8건 모두 끝까지 하트비트가
    # 갱신됐다. 리스 만료와 하트비트 정지를 함께 요구해야 한다.
    assert "AND (lease_expires_at IS NULL OR lease_expires_at < NOW())" in reaper
    assert "COALESCE(heartbeat_at, updated_at, started_at)" in reaper
    assert "interrupted_partial" in reaper
    assert "execution_reaped" in reaper
    # 주기 호출이 실제로 걸려 있어야 한다 — 함수만 있고 안 부르면 무의미하다.
    assert "await _reap_abandoned_executions_once()" in main


def test_scanner_and_lease_share_one_age_knob():
    """두 곳이 같은 환경변수를 읽어야 한다.

    한쪽만 늘리면 수거되기 전에 되살아나는 창이 생긴다.
    """
    main = Path("app/main.py").read_text(encoding="utf-8")
    service = Path("app/services/chat_service.py").read_text(encoding="utf-8")

    # 기본값 6시간은 관측된 정상 최대 지속시간(289.5분)을 넘긴 값이다.
    # 낮추려면 그 실측부터 다시 해라.
    assert 'os.getenv("AADS_EXECUTION_MAX_AGE_HOURS", "6")' in main
    assert 'os.getenv("AADS_EXECUTION_MAX_AGE_HOURS", "6")' in service
