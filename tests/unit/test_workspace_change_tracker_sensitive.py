"""AADS-SEC-M0: 자동 커밋·pre-commit 민감 보안문서 차단 게이트."""
import ast
import asyncio
import re
from pathlib import Path

import pytest

from app.services import workspace_change_tracker as tracker
from app.services.workspace_change_tracker import (
    _SENSITIVE_CONTENT_MARKERS,
    _SENSITIVE_DIR_MARKERS,
    _SENSITIVE_ENV_TEMPLATE_SUFFIXES,
    _SENSITIVE_LINE_DECORATION,
    _SENSITIVE_LINE_START_MARKERS,
    _SENSITIVE_NAME_MARKERS,
    _SENSITIVE_SUFFIXES,
    _is_sensitive_path,
)

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "pre-commit"


@pytest.mark.parametrize(
    "rel_path",
    [
        "reports/x_SECURITY_AUDIT_test.md",
        "reports/x_security-audit_test.md",
        "docs/web_PENTEST_notes.md",
        "security-private/notes.md",
        "docs/Security-Private/notes.md",
        "certs/server.pem",
        "certs/server.KEY",
        "certs/bundle.p12",
        ".env",
        ".env.local",
        ".env.prod",
        "deploy/.env.production",
    ],
)
def test_path_rules_block(tmp_path, rel_path):
    assert _is_sensitive_path(str(tmp_path), rel_path)


@pytest.mark.parametrize(
    "marker",
    ["DO-NOT-COMMIT", "Do Not Commit", "커밋 금지", "커밋하거나 푸시하지 말 것"],
)
def test_content_markers_block(tmp_path, marker):
    doc = tmp_path / "docs" / "plain_notes.md"
    doc.parent.mkdir()
    doc.write_text(f"# 메모\n\n> {marker}\n\n본문\n", encoding="utf-8")
    reason = _is_sensitive_path(str(tmp_path), "docs/plain_notes.md")
    assert reason and "content marker" in reason


@pytest.mark.parametrize(
    "rel_path",
    [".env.example", ".env.e2e.example", "deploy/.env.sample", ".env.production.template"],
)
def test_env_templates_pass(tmp_path, rel_path):
    assert _is_sensitive_path(str(tmp_path), rel_path) is None


def test_public_prohibition_phrase_alone_passes(tmp_path):
    doc = tmp_path / "docs" / "plans" / "voice_mvp.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("| 항목 | 공개 금지 |\n\n음성 원본은 공개 금지 대상이다.\n", encoding="utf-8")
    assert _is_sensitive_path(str(tmp_path), "docs/plans/voice_mvp.md") is None


def test_commit_ban_phrase_mid_sentence_passes(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text("- R-003: .env 키 커밋 금지\n실제 값은 git 커밋 금지\n", encoding="utf-8")
    assert _is_sensitive_path(str(tmp_path), "README.md") is None


def test_env_template_body_is_not_scanned(tmp_path):
    tmpl = tmp_path / ".env.e2e.example"
    tmpl.write_text("# Do not commit real passwords.\n# git 커밋 금지\n", encoding="utf-8")
    assert _is_sensitive_path(str(tmp_path), ".env.e2e.example") is None


def test_content_marker_after_8kb_is_not_scanned(tmp_path):
    doc = tmp_path / "long.md"
    doc.write_text("a" * 9000 + "\nDO NOT COMMIT\n", encoding="utf-8")
    assert _is_sensitive_path(str(tmp_path), "long.md") is None


@pytest.mark.parametrize(
    "rel_path",
    [
        "app/services/chat_service.py",
        "reports/20260929_weekly_summary.md",
        "docs/security_guide.md",
        "app/keys.py",
        "config/environment.md",
    ],
)
def test_normal_files_pass(tmp_path, rel_path):
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('ok')\n", encoding="utf-8")
    assert _is_sensitive_path(str(tmp_path), rel_path) is None


def test_missing_or_binary_file_uses_path_rules_only(tmp_path):
    assert _is_sensitive_path(str(tmp_path), "docs/not_there.md") is None
    blob = tmp_path / "img.bin"
    blob.write_bytes(b"\x00\x01DO NOT COMMIT")
    assert _is_sensitive_path(str(tmp_path), "img.bin") is None


def _run_finalize(monkeypatch, repo_root, porcelain, remote_heads):
    notified = {}

    async def fake_run_git_command(project, repo, command):
        if "status --porcelain" in command:
            return porcelain
        if command.startswith("head -c"):
            for path, text in remote_heads.items():
                if path in command:
                    return text
            return ""
        return ""

    async def fake_mark_group(**kwargs):
        return None

    async def fake_config_errors(project, repo, file_paths):
        # 일반 파일을 문법 오류로 막아 git add/commit 전에 결과를 돌려받는다.
        return {path: "stub" for path in file_paths}

    async def fake_notify(project, repo, session_id, sensitive):
        notified.update(sensitive)

    monkeypatch.setattr(tracker, "_repo_workdir", lambda project, repo: str(repo_root))
    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_command)
    monkeypatch.setattr(tracker, "_mark_group", fake_mark_group)
    monkeypatch.setattr(tracker, "_config_syntax_errors", fake_config_errors)
    monkeypatch.setattr(tracker, "_notify_sensitive_skipped", fake_notify)
    rows = [{"file_path": path} for path in ["reports/x_SECURITY_AUDIT_test.md", "docs/memo.md", "app/ok.py"]]
    result = asyncio.run(
        tracker._finalize_group(
            session_id="session-12345678", project="AADS", repo="aads-server", rows=rows
        )
    )
    return result, notified


def test_finalize_group_records_skipped_sensitive(monkeypatch, tmp_path):
    porcelain = "\n".join(
        [" M reports/x_SECURITY_AUDIT_test.md", "?? docs/memo.md", " M app/ok.py"]
    )
    # 컨테이너처럼 로컬에 파일이 없으면 본문은 원격 head 로 읽는다.
    result, notified = _run_finalize(
        monkeypatch,
        tmp_path / "missing-root",
        porcelain,
        {"docs/memo.md": "# 메모\nDO-NOT-COMMIT\n", "app/ok.py": "print(1)\n"},
    )

    skipped = {item["path"]: item["reason"] for item in result["skipped_sensitive"]}
    assert set(skipped) == {"reports/x_SECURITY_AUDIT_test.md", "docs/memo.md"}
    assert "SECURITY_AUDIT" in skipped["reports/x_SECURITY_AUDIT_test.md"]
    assert "content marker" in skipped["docs/memo.md"]
    assert result["files"] == ["app/ok.py"]
    assert set(notified) == set(skipped)
    # 기존 skipped_* 동작은 그대로다.
    assert "skipped_outside_repo" not in result


def test_finalize_group_without_sensitive_has_no_key(monkeypatch, tmp_path):
    porcelain = "\n".join([" M app/ok.py"])

    async def fake_run_git_command(project, repo, command):
        return porcelain if "status --porcelain" in command else "print(1)\n"

    async def fake_config_errors(project, repo, file_paths):
        return {path: "stub" for path in file_paths}

    async def fake_mark_group(**kwargs):
        return None

    monkeypatch.setattr(tracker, "_repo_workdir", lambda project, repo: str(tmp_path))
    monkeypatch.setattr(tracker, "_run_git_command", fake_run_git_command)
    monkeypatch.setattr(tracker, "_mark_group", fake_mark_group)
    monkeypatch.setattr(tracker, "_config_syntax_errors", fake_config_errors)
    result = asyncio.run(
        tracker._finalize_group(
            session_id="session-12345678",
            project="AADS",
            repo="aads-server",
            rows=[{"file_path": "app/ok.py"}],
        )
    )
    assert "skipped_sensitive" not in result
    assert result["files"] == ["app/ok.py"]


def _hook_tuple(name):
    text = _HOOK.read_text(encoding="utf-8")
    match = re.search(rf"^{name} = (\(.*?\))$", text, re.M)
    assert match, f"{name} not found in pre-commit"
    return ast.literal_eval(match.group(1))


def test_pre_commit_rules_match_tracker():
    # 두 게이트가 다른 규칙으로 돌면 한쪽이 반드시 낡는다.
    assert _hook_tuple("DIR_MARKERS") == _SENSITIVE_DIR_MARKERS
    assert _hook_tuple("NAME_MARKERS") == _SENSITIVE_NAME_MARKERS
    assert _hook_tuple("SUFFIXES") == _SENSITIVE_SUFFIXES
    assert _hook_tuple("CONTENT_MARKERS") == _SENSITIVE_CONTENT_MARKERS
    assert _hook_tuple("ENV_TEMPLATE_SUFFIXES") == _SENSITIVE_ENV_TEMPLATE_SUFFIXES
    assert _hook_tuple("LINE_START_MARKERS") == _SENSITIVE_LINE_START_MARKERS
    text = _HOOK.read_text(encoding="utf-8")
    decoration = re.search(r'^LINE_DECORATION = ("(?:[^"\\\n]|\\.)*")$', text, re.M)
    assert decoration and ast.literal_eval(decoration.group(1)) == _SENSITIVE_LINE_DECORATION
    assert "공개 금지" not in _SENSITIVE_CONTENT_MARKERS + _SENSITIVE_LINE_START_MARKERS
    assert "ALLOW_SENSITIVE_COMMIT" in text
