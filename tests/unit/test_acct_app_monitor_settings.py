"""Validate release monitoring without git mutations, remote calls or deployment."""

import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy_acct_app_cafe24.sh"


@pytest.mark.parametrize(
    "seconds,interval,threshold,expected",
    [
        ("300", "15", "2", 0),
        ("600", "30", "3", 0),
        ("0", "15", "2", 2),
        ("299", "15", "2", 2),
        ("300", "0", "2", 2),
        ("300", "301", "2", 2),
        ("300", "08", "2", 2),
        ("300", "15", "0", 2),
        ("300", "15", "invalid", 2),
        ("99999999999999999999", "15", "2", 2),
    ],
)
def test_monitor_settings(seconds, interval, threshold, expected):
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; validate_monitor_settings', "monitor-test", str(SCRIPT)],
        env={**os.environ, "MONITOR_SECONDS": seconds, "MONITOR_INTERVAL": interval,
             "MONITOR_FAIL_THRESHOLD": threshold},
        capture_output=True, text=True, timeout=5,
    )
    assert proc.returncode == expected, proc.stdout + proc.stderr
