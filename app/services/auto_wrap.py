from __future__ import annotations

"""Pipeline Runner 작업 완료 시 WRAP 파일 자동 생성 모듈"""

import os
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WRAPS_DIR = "/root/aads/aads-docs/shared/wraps"


def generate_wrap_report(
    job_id: str,
    project: str,
    instruction: str,
    changes: str,
    verification: str,
    size: str = "M",
    elapsed_sec: int = 0,
    review_score: str = "",
) -> str:
    """
    Pipeline Runner 작업 완료 후 WRAP 파일 생성.
    반환값: 생성된 파일 경로 (실패 시 빈 문자열)
    """
    os.makedirs(WRAPS_DIR, exist_ok=True)

    title = instruction.split("\n")[0].lstrip("#").strip()[:50]
    filename = f"{project}-WRAP-RUNNER-{job_id}.md"
    filepath = os.path.join(WRAPS_DIR, filename)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # S/XS: 경량 WRAP
    if size in ("S", "XS"):
        content = f"# {project}-WRAP-RUNNER-{job_id}\n\n_생성: {now}_\n\n## 요약\n{title}\n\n## 변경\n{changes or '없음'}\n\n## 검증\n{verification or '없음'}\n"
    else:
        # M/L/XL: 전체 6섹션
        elapsed_min = elapsed_sec // 60
        content = f"""# {project}-WRAP-RUNNER-{job_id}

_생성: {now}_

## 1. 작업 요약

- **Job**: {job_id}
- **Project**: {project}
- **지시**: {title}

## 2. 변경 파일

{changes or '변경 없음'}

## 3. 검증 결과

{verification or '미검증'}

검수 점수: {review_score or 'N/A'}

## 4. 소요 시간

- 실행: {elapsed_min}분 ({elapsed_sec}초)

## 5. 회고 (KPT)

- **Keep**: Pipeline Runner 병렬 실행으로 빠른 작업 완료
- **Problem**: Runner 재시작 시 진행 중 작업 손실
- **Try**: 마이크로 태스크 분리로 재시작 영향 최소화

## 6. 교훈 후보

- 장시간 작업은 파일 단위로 분리하여 제출할 것
"""

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"WRAP 파일 생성: {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"WRAP 파일 생성 실패: {e}")
        return ""
