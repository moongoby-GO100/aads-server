"""Run real roster/payroll DOM handlers against isolated API fixtures."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node") or not os.environ.get("PLAYWRIGHT_MODULE"),
                    reason="requires Node and explicit PLAYWRIGHT_MODULE (no runtime dependency install)")
def test_admin_roster_and_payroll_browser_workflow():
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("obys_admin_direct_browser.cjs"))],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count('"roster":"PASS"') == 2
    assert result.stdout.count('"payroll":"PASS"') == 2
