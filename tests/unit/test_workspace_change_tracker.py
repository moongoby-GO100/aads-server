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


# ── GO100 운영 폴더 Chat-Finalize 차단 및 원격 git 상태 해소 테스트 (CEO 지시 2026-11-11, R5B) ──
import asyncio
from contextlib import asynccontextmanager


class _FakeLedgerStore:
    def __init__(self, initial_rows=None):
        self.rows = list(initial_rows or [])

    async def list_changes(self, *, session_id=None, project=None, repo=None, statuses=None, limit=100):
        out = []
        for r in self.rows:
            if session_id and r.get("session_id") != session_id:
                continue
            if project and r.get("project") != project:
                continue
            if repo and r.get("repo") != repo:
                continue
            if statuses and r.get("status") not in statuses:
                continue
            out.append(dict(r))
        return out[:limit]

    async def mark_group(self, *, session_id, project, repo, file_paths, ledger_file_paths=None, status, last_error="", commit_sha=None, commit_message=None):
        targets = set((file_paths or []) + (ledger_file_paths or []))
        for r in self.rows:
            if r.get("session_id") == session_id and r.get("project") == project and r.get("repo") == repo:
                if not targets or r.get("file_path") in targets:
                    r["status"] = status
                    r["last_error"] = last_error
                    if commit_sha:
                        r["commit_sha"] = commit_sha
                    if commit_message:
                        r["commit_message"] = commit_message


def test_go100_finalize_blocked_when_remote_status_has_changes(monkeypatch):
    """원격 status 출력 있음 → ok=False·blocked=True, commit/push mock 미호출, has_pending_changes 여전히 True."""
    from app.services import workspace_change_tracker as tracker

    store = _FakeLedgerStore([
        {
            "session_id": "sess-go100-1",
            "project": "GO100",
            "repo": "kis-autotrade-v4",
            "file_path": "backend/app/main.py",
            "status": "dirty",
            "git_status": " M",
        }
    ])
    commands_called = []

    async def fake_run_git_command(project, repo, command):
        commands_called.append(command)
        if "status --porcelain" in command:
            return "[GO100 명령 실행 — exit=0]\n$ git ...\n M backend/app/main.py\n"
        return ""

    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_command)
    monkeypatch.setattr(tracker, "list_changes", store.list_changes)
    monkeypatch.setattr(tracker, "_mark_group", store.mark_group)

    rows = asyncio.run(store.list_changes(session_id="sess-go100-1", project="GO100"))
    result = asyncio.run(tracker._finalize_group(
        session_id="sess-go100-1",
        project="GO100",
        repo="kis-autotrade-v4",
        rows=rows,
    ))

    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["reason"] == "go100_prod_tree_direct_edit"
    assert result["pending_paths"] == ["backend/app/main.py"]
    assert "러너로 main 에 옮긴 뒤 운영 폴더를 원복해야 배포 가능" in result["detail"]
    assert not any("git commit" in cmd for cmd in commands_called)
    assert not any("git push" in cmd for cmd in commands_called)
    assert not any("git add" in cmd for cmd in commands_called)
    has_pending = asyncio.run(tracker.has_pending_changes(session_id="sess-go100-1", project="GO100"))
    assert has_pending is True


def test_go100_finalize_reconciled_clean_when_remote_status_clean(monkeypatch):
    """원격 status 출력 비어 있음 → reconciled_clean 으로 갱신, ok=True, commit/push 미호출."""
    from app.services import workspace_change_tracker as tracker

    store = _FakeLedgerStore([
        {
            "session_id": "sess-go100-2",
            "project": "GO100",
            "repo": "kis-autotrade-v4",
            "file_path": "backend/app/main.py",
            "status": "dirty",
            "git_status": " M",
        }
    ])
    commands_called = []

    async def fake_run_git_command(project, repo, command):
        commands_called.append(command)
        if "status --porcelain" in command:
            return "[GO100 명령 실행 — exit=0]\n$ git ...\n"
        return ""

    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_command)
    monkeypatch.setattr(tracker, "list_changes", store.list_changes)
    monkeypatch.setattr(tracker, "_mark_group", store.mark_group)

    rows = asyncio.run(store.list_changes(session_id="sess-go100-2", project="GO100"))
    result = asyncio.run(tracker._finalize_group(
        session_id="sess-go100-2",
        project="GO100",
        repo="kis-autotrade-v4",
        rows=rows,
    ))

    assert result["ok"] is True
    assert result["status"] == "reconciled_clean"
    assert not any("git commit" in cmd for cmd in commands_called)
    assert not any("git push" in cmd for cmd in commands_called)
    assert not any("git add" in cmd for cmd in commands_called)
    assert store.rows[0]["status"] == "reconciled_clean"
    has_pending = asyncio.run(tracker.has_pending_changes(session_id="sess-go100-2", project="GO100"))
    assert has_pending is False


def test_go100_finalize_fails_when_remote_command_fails(monkeypatch):
    """원격 실패 → ok=False, reason=go100_prod_tree_status_unknown."""
    from app.services import workspace_change_tracker as tracker

    store = _FakeLedgerStore([
        {
            "session_id": "sess-go100-3",
            "project": "GO100",
            "repo": "kis-autotrade-v4",
            "file_path": "backend/app/main.py",
            "status": "dirty",
        }
    ])
    commands_called = []

    async def fake_run_git_command(project, repo, command):
        commands_called.append(command)
        return "[ERROR] SSH connection timed out"

    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_command)
    monkeypatch.setattr(tracker, "list_changes", store.list_changes)
    monkeypatch.setattr(tracker, "_mark_group", store.mark_group)

    rows = asyncio.run(store.list_changes(session_id="sess-go100-3", project="GO100"))
    result = asyncio.run(tracker._finalize_group(
        session_id="sess-go100-3",
        project="GO100",
        repo="kis-autotrade-v4",
        rows=rows,
    ))

    assert result["ok"] is False
    assert result["reason"] == "go100_prod_tree_status_unknown"
    assert not any("git commit" in cmd for cmd in commands_called)
    assert not any("git push" in cmd for cmd in commands_called)
    has_pending = asyncio.run(tracker.has_pending_changes(session_id="sess-go100-3", project="GO100"))
    assert has_pending is True

    # Exception 발생 케이스도 검증
    async def fake_run_git_error(project, repo, command):
        raise RuntimeError("network down")

    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_error)
    result_err = asyncio.run(tracker._finalize_group(
        session_id="sess-go100-3",
        project="GO100",
        repo="kis-autotrade-v4",
        rows=rows,
    ))
    assert result_err["ok"] is False
    assert result_err["reason"] == "go100_prod_tree_status_unknown"


def test_go100_preflight_finalize_blocks_deploy_command(monkeypatch):
    """_preflight_finalize_for_command 가 차단 시 triggered=True·ok=False → run_remote_command 배포 명령이 preflight_finalize_failed 로 막힘."""
    from app.services import workspace_change_tracker as tracker
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    store = _FakeLedgerStore([
        {
            "session_id": "sess-go100-deploy",
            "project": "GO100",
            "repo": "kis-autotrade-v4",
            "file_path": "backend/app/main.py",
            "status": "dirty",
            "git_status": " M",
        }
    ])
    token = current_chat_session_id.set("sess-go100-deploy")
    remote_executed = []

    try:
        async def fake_run_git(project, repo, command):
            if "status --porcelain" in command:
                return "[GO100 명령 실행 — exit=0]\n$ git ...\n M backend/app/main.py\n"
            return ""

        async def fake_tool_run_remote_command(project, command):
            remote_executed.append((project, command))
            return "RESTARTED"

        monkeypatch.setattr(tracker, "_run_git_command", fake_run_git)
        monkeypatch.setattr(tracker, "list_changes", store.list_changes)
        monkeypatch.setattr(tracker, "_mark_group", store.mark_group)

        from app.api import ceo_chat_tools
        monkeypatch.setattr(ceo_chat_tools, "tool_run_remote_command", fake_tool_run_remote_command)

        executor = ToolExecutor()
        deploy_cmd = "systemctl restart go100"

        # 1) preflight_finalize 직접 호출 검증
        preflight = asyncio.run(executor._preflight_finalize_for_command("GO100", deploy_cmd))
        assert preflight["triggered"] is True
        assert preflight["ok"] is False
        assert preflight["groups"][0]["blocked"] is True

        # 2) _run_remote_command 배포 명령 차단 검증
        result = asyncio.run(executor._run_remote_command({"project": "GO100", "command": deploy_cmd}))
        assert isinstance(result, dict)
        assert result["error"] == "preflight_finalize_failed"
        assert result["detail"]["triggered"] is True
        assert result["detail"]["ok"] is False
        # 실제 원격 배포 명령은 실행되지 않아야 함
        assert remote_executed == []
    finally:
        current_chat_session_id.reset(token)


def test_kis_aads_groups_commit_as_before(monkeypatch):
    """KIS·AADS 그룹: 기존대로 commit 함수가 호출되고 ok 값이 기존과 같음을 명시 assert. try/except 로 감싸지 않는다."""
    from app.services import workspace_change_tracker as tracker

    @asynccontextmanager
    async def fake_lock(*args, **kwargs):
        yield

    for proj, repo, path in [("KIS", "kis-autotrade-v4", "strategy/bot.py"), ("AADS", "aads-server", "app/api/route.py")]:
        commands_called = []
        store = _FakeLedgerStore([
            {
                "session_id": f"sess-{proj}",
                "project": proj,
                "repo": repo,
                "file_path": path,
                "status": "dirty",
                "git_status": " M",
            }
        ])

        async def fake_run_git(project, repo_name, command):
            commands_called.append(command)
            if "status --porcelain" in command:
                return f" M {path}\n"
            if "check-ignore" in command:
                return ""
            if "rev-parse HEAD" in command:
                return "1111222233334444555566667777888899990000\n"
            if "abbrev-ref" in command:
                return "main\n"
            return ""

        async def fake_sensitive(*args):
            return {}

        async def fake_config_errors(*args):
            return {}

        async def fake_regression(*args):
            return {}

        monkeypatch.setattr(tracker, "_run_git_command", fake_run_git)
        monkeypatch.setattr(tracker, "list_changes", store.list_changes)
        monkeypatch.setattr(tracker, "_mark_group", store.mark_group)
        monkeypatch.setattr(tracker, "git_project_lock", fake_lock)
        monkeypatch.setattr(tracker, "_sensitive_paths", fake_sensitive)
        monkeypatch.setattr(tracker, "_config_syntax_errors", fake_config_errors)
        monkeypatch.setattr(tracker, "_content_regression_paths", fake_regression)

        rows = asyncio.run(store.list_changes(session_id=f"sess-{proj}", project=proj))
        result = asyncio.run(tracker._finalize_group(
            session_id=f"sess-{proj}",
            project=proj,
            repo=repo,
            rows=rows,
        ))

        assert result["ok"] is True
        assert result["status"] == "pushed"
        assert any(cmd.startswith("git add --") for cmd in commands_called)
        assert any(cmd.startswith("git commit --only") for cmd in commands_called)
        assert any(cmd.startswith("git push origin") for cmd in commands_called)
        assert store.rows[0]["status"] == "pushed"
