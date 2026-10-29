"""/directives/preflight 가 로그 기록 때문에 500 을 내지 않아야 한다.

2026-10-29 배포 #5906 감시에서 preflight_check_failed
("Logger._log() got an unexpected keyword argument 'task_id'") 로 드러났다.
preflight_checker 가 표준 logging 에 키워드 인자를 넘기고 있었다.
"""

import logging
from unittest.mock import patch

from app.services import preflight_checker


def test_preflight_with_unmet_dependency_returns_wait_instead_of_raising(tmp_path, caplog):
    # 운영처럼 INFO 를 켠다. 꺼져 있으면 문제의 로그 호출이 실행되지 않아 결함이 숨는다.
    caplog.set_level(logging.INFO)
    for name in ("pending", "running", "done"):
        (tmp_path / name).mkdir()
    with patch.object(preflight_checker, "_PENDING_DIR", str(tmp_path / "pending")), \
            patch.object(preflight_checker, "_RUNNING_DIR", str(tmp_path / "running")), \
            patch.object(preflight_checker, "_DONE_DIR", str(tmp_path / "done")):
        result = preflight_checker.run_preflight(task_id="AADS-1", depends_on="AADS-0")

    assert result["recommendation"] == "WAIT"
