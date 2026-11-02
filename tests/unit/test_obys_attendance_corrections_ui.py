"""정정 UI의 실제 renderer/handler를 운영 연결 없이 검증한다."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="Node 실행 환경 없음")
def test_correction_renderer_and_handlers():
    result = subprocess.run(
        ["node", "tests/unit/obys_attendance_corrections_node.cjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"result":"PASS"' in result.stdout


@pytest.mark.skipif(not shutil.which("node"), reason="Node 실행 환경 없음")
@pytest.mark.parametrize("filename", ["clock.html", "admin.html"])
def test_inline_scripts_and_correction_mount(filename):
    import re

    html = (ROOT / "app/static/apps/obys" / filename).read_text()
    assert "modules/attendance-corrections.js?v=20261102-r1" in html
    assert "ObysAttendanceCorrections.mount" in html
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert scripts
    for script in scripts:
        result = subprocess.run(
            ["node", "--check"], input=script, text=True, capture_output=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
