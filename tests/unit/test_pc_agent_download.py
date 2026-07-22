from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.responses import RedirectResponse

from app.api import kakao_bot


@pytest.mark.asyncio
async def test_agent_download_exe_redirects_to_matching_release_when_local_exe_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.55", encoding="utf-8")
    monkeypatch.setattr(kakao_bot, "PC_AGENT_VERSION_FILE", version_file)
    monkeypatch.setattr(kakao_bot, "PC_AGENT_DIR", tmp_path)

    response = await kakao_bot.agent_download_exe()

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 307
    assert response.headers["location"].endswith(
        "/pc-agent-v1.0.55/kakaobot-setup.exe"
    )
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_agent_version_advertises_installable_exe_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.55", encoding="utf-8")
    monkeypatch.setattr(kakao_bot, "PC_AGENT_VERSION_FILE", version_file)
    monkeypatch.setattr(kakao_bot, "PC_AGENT_DIR", tmp_path)
    monkeypatch.setattr(kakao_bot, "PC_AGENT_EXE_FILE", tmp_path / "missing.exe")

    result = await kakao_bot.agent_version()

    assert result["version"] == "1.0.55"
    assert result["download_url"].endswith("/agent/download-exe")
    assert result["exe_available"] is True
    assert result["distribution"] == "github_release"
