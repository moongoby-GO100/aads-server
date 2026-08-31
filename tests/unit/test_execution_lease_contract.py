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


def test_bluegreen_deploy_builds_once_and_starts_without_build():
    deploy = Path("deploy.sh").read_text(encoding="utf-8")
    compose = Path("docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "release image 1회 빌드" in deploy
    assert "up -d --no-build --no-deps" in deploy
    assert "active/standby image digest mismatch" in deploy
    assert 'git -C "$COMPOSE_DIR" archive --format=tar HEAD' in deploy
    assert compose.count("image: aads-server:${AADS_RELEASE_SHA:-local}") == 2


def test_placeholder_repair_uses_the_actual_assistant_execution_unique_index():
    router = Path("app/routers/chat.py").read_text(encoding="utf-8")
    helper = router.split("async def _ensure_running_placeholder_anchor", 1)[1].split(
        "async def ", 1
    )[0]

    assert "AND role = 'assistant'" in helper
    assert "ON CONFLICT (execution_id)" in helper
    assert "WHERE role = 'assistant'" in helper
    assert "DO NOTHING" in helper
    assert "interrupted_partial" in helper
