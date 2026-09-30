"""사용자 예약 작업(user_*) 영속화용 APScheduler jobstore."""
from __future__ import annotations

from typing import Callable

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

PERSISTENT_JOBSTORE_ALIAS = "persistent"
PERSISTENT_JOBS_TABLE = "apscheduler_jobs"


class SlotGatedJobStore(SQLAlchemyJobStore):
    """활성 API 슬롯에서만 due 잡을 내주는 SQLAlchemyJobStore.

    블루/그린은 두 슬롯이 상시 떠 있고 각자 스케줄러를 기동한다. 같은 테이블을 두
    스케줄러가 그대로 읽으면 사용자 잡이 두 번 실행되고, 스탠바이가 먼저 실행하면
    1회성(date) 잡 row 가 소진된다. 그래서 스탠바이는 due 잡도, 다음 깨움 시각도
    보지 못하게 막는다.
    """

    def __init__(self, *args, is_active: Callable[[], bool], **kwargs):
        super().__init__(*args, **kwargs)
        self._is_active = is_active

    def get_due_jobs(self, now):
        if not self._is_active():
            return []
        return super().get_due_jobs(now)

    def get_next_run_time(self):
        if not self._is_active():
            return None
        return super().get_next_run_time()
