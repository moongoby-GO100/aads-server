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


def test_local_exe_is_current_when_stamp_matches_even_if_mtime_is_older(
    tmp_path: Path,
) -> None:
    """빌드 스탬프가 일치하면 EXE mtime이 VERSION보다 과거여도 최신으로 본다."""
    import os

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    exe_file = dist_dir / "kakaobot-setup.exe"
    exe_file.write_bytes(b"exe")
    (dist_dir / "kakaobot-setup.exe.version").write_text("1.0.73", encoding="utf-8")
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.73", encoding="utf-8")
    # EXE mtime을 VERSION보다 1시간 과거로 강제 (mtime 역전 재현)
    os.utime(exe_file, (0, 0))

    assert kakao_bot._local_pc_agent_exe_is_current(exe_file, version_file) is True


def test_local_exe_is_not_current_when_stamp_version_differs(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    exe_file = dist_dir / "kakaobot-setup.exe"
    exe_file.write_bytes(b"exe")
    (dist_dir / "kakaobot-setup.exe.version").write_text("1.0.72", encoding="utf-8")
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.73", encoding="utf-8")

    assert kakao_bot._local_pc_agent_exe_is_current(exe_file, version_file) is False


@pytest.mark.asyncio
async def test_stale_exe_is_served_locally_when_release_asset_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Release 자산이 없으면 GitHub 404 대신 로컬 stale EXE를 내려준다."""
    import os

    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.99", encoding="utf-8")
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    exe_file = dist_dir / "kakaobot-setup.exe"
    exe_file.write_bytes(b"stale-exe")
    os.utime(exe_file, (0, 0))  # stale
    monkeypatch.setattr(kakao_bot, "PC_AGENT_VERSION_FILE", version_file)
    monkeypatch.setattr(kakao_bot, "PC_AGENT_DIR", tmp_path)

    async def _unavailable(url: str) -> bool:
        return False

    monkeypatch.setattr(kakao_bot, "_release_asset_available", _unavailable)

    response = await kakao_bot.agent_download_exe()

    assert not isinstance(response, RedirectResponse)
    assert response.headers["x-pc-agent-exe-stale"] == "true"


@pytest.mark.asyncio
async def test_stale_exe_redirects_when_release_asset_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import os

    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.99", encoding="utf-8")
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    exe_file = dist_dir / "kakaobot-setup.exe"
    exe_file.write_bytes(b"stale-exe")
    os.utime(exe_file, (0, 0))
    monkeypatch.setattr(kakao_bot, "PC_AGENT_VERSION_FILE", version_file)
    monkeypatch.setattr(kakao_bot, "PC_AGENT_DIR", tmp_path)

    async def _available(url: str) -> bool:
        return True

    monkeypatch.setattr(kakao_bot, "_release_asset_available", _available)

    response = await kakao_bot.agent_download_exe()

    assert isinstance(response, RedirectResponse)
    assert response.headers["location"].endswith("/pc-agent-v1.0.99/kakaobot-setup.exe")


def test_build_exe_writes_version_stamp() -> None:
    script = (Path(__file__).resolve().parents[2] / "pc_agent" / "build_exe.py").read_text(
        encoding="utf-8"
    )

    assert "_write_build_stamp" in script
    assert '.version' in script
    assert "os.utime" in script


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
