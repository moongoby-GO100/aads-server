from app.services.workspace_change_tracker import (
    _derive_change_owner,
    _is_ignored_change_path,
    _parse_porcelain_entries,
)


def test_aads_runtime_state_files_are_ignored_change_paths():
    assert _is_ignored_change_path("AADS", "aads-server", ".active_container")
    assert _is_ignored_change_path("AADS", "aads-server", "/root/aads/aads-server/.active_port")


def test_non_runtime_files_are_not_ignored_change_paths():
    assert not _is_ignored_change_path("AADS", "aads-server", "app/services/chat_service.py")
    assert not _is_ignored_change_path("AADS", "aads-dashboard", ".active_port")
    assert not _is_ignored_change_path("NTV2", "newtalk-v2", ".active_port")


def test_generated_runtime_files_are_ignored_change_paths():
    assert _is_ignored_change_path("AADS", "aads-server", "app/data/foo/events.jsonl")
    assert _is_ignored_change_path("AADS", "aads-dashboard", "tsconfig.tsbuildinfo")
    assert _is_ignored_change_path("AADS", "aads-server", "litellm-config.yaml.bak")


def test_parse_porcelain_entries_keeps_branch_status_and_rename_target():
    text = "\n".join([
        "## main...origin/main",
        " M app/services/workspace_change_tracker.py",
        "?? scripts/sync_workspace_change_ledger.py",
        "R  old.py -> new.py",
    ])

    entries = _parse_porcelain_entries(text)

    assert entries == [
        {
            "path": "app/services/workspace_change_tracker.py",
            "git_status": " M",
            "git_branch": "main",
        },
        {
            "path": "scripts/sync_workspace_change_ledger.py",
            "git_status": "??",
            "git_branch": "main",
        },
        {"path": "new.py", "git_status": "R ", "git_branch": "main"},
    ]


def test_derive_change_owner_prefers_explicit_then_session_then_tool():
    assert _derive_change_owner("session-1234567890", "tool", "owner-x") == "owner-x"
    assert _derive_change_owner("session-1234567890", "tool") == "chat:session-1234"
    assert _derive_change_owner("", "write_remote_file") == "tool:write_remote_file"


def _run_config_syntax_check(monkeypatch, contents):
    import asyncio

    from app.services import workspace_change_tracker as tracker

    async def fake_run_git_command(project, repo, command):
        for path, text in contents.items():
            if path in command:
                return text
        return ""

    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_command)
    return asyncio.run(
        tracker._config_syntax_errors("AADS", "aads-server", list(contents))
    )


def test_config_syntax_errors_flags_broken_yaml(monkeypatch):
    # 2026-09-21 재현: model_list 밖에 시퀀스가 붙어 YAML 전체가 깨진 상태.
    errors = _run_config_syntax_check(
        monkeypatch,
        {
            "litellm-config.yaml": (
                "model_list:\n- model_name: a\nrouter_settings:\n  x: 1\n- model_name: b\n"
            ),
            "app/main.py": "def f():\n    pass\n",
        },
    )
    assert "litellm-config.yaml" in errors
    assert "app/main.py" not in errors


def test_config_syntax_errors_passes_valid_config(monkeypatch):
    errors = _run_config_syntax_check(
        monkeypatch,
        {
            "litellm-config.yaml": "model_list:\n- model_name: a\n- model_name: b\n",
            "package.json": '{"name": "ok"}',
        },
    )
    assert errors == {}


def test_config_syntax_errors_flags_broken_json(monkeypatch):
    errors = _run_config_syntax_check(monkeypatch, {"tsconfig.json": '{"a": 1,}'})
    assert "tsconfig.json" in errors


_HEAD_SHA = "a" * 40
_OLD_SHA = "b" * 40
_NEW_SHA = "c" * 40


def _run_regression_check(monkeypatch, outputs):
    import asyncio

    from app.services import workspace_change_tracker as tracker

    async def fake_run_git_command(project, repo, command):
        for path, text in outputs.items():
            if path in command:
                return text
        return ""

    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_command)
    return asyncio.run(
        tracker._content_regression_paths("AADS", "aads-server", list(outputs))
    )


def test_content_regression_flags_revert_to_older_commit(monkeypatch):
    # 2026-09-21 재현: 자동 커밋이 당일 수정본을 옛 내용으로 되돌린 상황.
    regressed = _run_regression_check(
        monkeypatch,
        {"litellm-config.yaml": f"{_OLD_SHA}\n--\n{_HEAD_SHA}\n{_OLD_SHA}\n"},
    )
    assert "litellm-config.yaml" in regressed


def test_content_regression_allows_new_content(monkeypatch):
    regressed = _run_regression_check(
        monkeypatch,
        {"app/main.py": f"{_NEW_SHA}\n--\n{_HEAD_SHA}\n{_OLD_SHA}\n"},
    )
    assert regressed == {}


def test_content_regression_allows_unchanged_and_new_file(monkeypatch):
    regressed = _run_regression_check(
        monkeypatch,
        {
            "same.py": f"{_HEAD_SHA}\n--\n{_HEAD_SHA}\n{_OLD_SHA}\n",
            "brand_new.py": f"{_NEW_SHA}\n--\n",
        },
    )
    assert regressed == {}


def _resolve_branch(monkeypatch, rev_parse_output):
    import asyncio

    from app.services import workspace_change_tracker as tracker

    async def fake_run_git_command(project, repo, command):
        assert "rev-parse --abbrev-ref HEAD" in command
        return rev_parse_output

    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_command)
    return asyncio.run(tracker._resolve_push_branch("AADS", "aads-server"))


def test_resolve_push_branch_uses_actual_branch(monkeypatch):
    # run_remote_command 출력에는 헤더와 `$ 명령` 에코가 섞여 온다.
    output = "[AADS 명령 실행 — exit=0]\n$ cd /root/aads/aads-server && git rev-parse --abbrev-ref HEAD\nmain\n"
    assert _resolve_branch(monkeypatch, output) == "main"


def test_resolve_push_branch_keeps_non_main_branch(monkeypatch):
    assert _resolve_branch(monkeypatch, "direct/hotfix-20260923\n") == "direct/hotfix-20260923"


def test_resolve_push_branch_falls_back_on_detached_head(monkeypatch):
    assert _resolve_branch(monkeypatch, "HEAD\n") == "main"


def test_resolve_push_branch_falls_back_on_git_error(monkeypatch):
    assert _resolve_branch(monkeypatch, "fatal: not a git repository\n") == "main"


def test_finalize_group_never_pushes_hardcoded_master():
    # 2026-09-23 회귀: origin 에 master 가 없어 폴백 push 가 항상 실패했고,
    # Chat-Finalize 커밋이 로컬 main 에만 쌓였다(29449698, b3ca871c).
    import inspect

    from app.services import workspace_change_tracker as tracker

    source = inspect.getsource(tracker._finalize_group)
    assert "push origin master" not in source
    assert "_resolve_push_branch" in source
