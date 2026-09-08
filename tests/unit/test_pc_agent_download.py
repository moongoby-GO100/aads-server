from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.responses import RedirectResponse

from app.api import kakao_bot


def test_zip_installer_bootstraps_python_and_uses_stable_install_path() -> None:
    script = (Path(__file__).resolve().parents[2] / "pc_agent" / "install.bat").read_text(
        encoding="utf-8"
    )

    assert "%LOCALAPPDATA%\\AADS\\PC-Agent" in script
    assert "where py" in script
    assert "Python.Python.3.11" in script
    assert '".venv\\Scripts\\python.exe" -m pip install' in script
    assert "AADS-PC-Agent-Autostart.cmd" in script


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
    assert result["exe_download_url"].endswith("/agent/download-exe")
    assert result["safe_download_url"].endswith("/agent/download?format=zip")
    assert result["exe_available"] is True
    assert result["distribution"] == "windows_exe"
    assert result["exe_distribution"] == "github_release"


@pytest.mark.asyncio
async def test_agent_download_default_never_falls_back_to_zip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.73", encoding="utf-8")
    monkeypatch.setattr(kakao_bot, "PC_AGENT_VERSION_FILE", version_file)
    monkeypatch.setattr(kakao_bot, "PC_AGENT_DIR", tmp_path)

    response = await kakao_bot.agent_download()

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 307
    assert response.headers["location"].endswith(
        "/pc-agent-v1.0.73/kakaobot-setup.exe"
    )


@pytest.mark.asyncio
async def test_agent_download_exe_embeds_install_ticket_in_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.57", encoding="utf-8")
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    exe_file = dist_dir / "kakaobot-setup.exe"
    exe_file.write_bytes(b"exe")
    monkeypatch.setattr(kakao_bot, "PC_AGENT_VERSION_FILE", version_file)
    monkeypatch.setattr(kakao_bot, "PC_AGENT_DIR", tmp_path)

    ticket = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFGH"
    response = await kakao_bot.agent_download_exe(install_ticket=ticket)

    content_disposition = response.headers["content-disposition"]
    assert f"--ticket-{ticket}.exe" in content_disposition
    assert response.headers["cache-control"].startswith("no-store")


@pytest.mark.asyncio
async def test_agent_download_zip_embeds_install_ticket_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.71", encoding="utf-8")
    (tmp_path / "agent.py").write_text("print('agent')\n", encoding="utf-8")
    (tmp_path / "launcher.py").write_text("print('launcher')\n", encoding="utf-8")
    monkeypatch.setattr(kakao_bot, "PC_AGENT_VERSION_FILE", version_file)
    monkeypatch.setattr(kakao_bot, "PC_AGENT_DIR", tmp_path)

    ticket = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFGH"
    response = await kakao_bot.agent_download(format="zip", install_ticket=ticket)

    body = b"".join([chunk async for chunk in response.body_iterator])
    import zipfile
    import io

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert zf.read("install_ticket.txt").decode("utf-8") == ticket
        assert "agent.py" in zf.namelist()
    assert response.headers["content-disposition"].endswith('auto.zip"')
