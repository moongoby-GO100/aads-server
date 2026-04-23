from __future__ import annotations

import os

INTERNAL_PIPELINE_HEADERS = {"x-monitor-key": "internal-pipeline-call"}


def get_pipeline_runner_base_url() -> str:
    """Pipeline Runner 내부 호출 기본 URL."""
    base_url = (
        os.getenv("PIPELINE_RUNNER_INTERNAL_BASE_URL")
        or os.getenv("AADS_API_INTERNAL_URL")
        or "http://localhost:8080"
    ).strip()
    return base_url.rstrip("/")


def get_pipeline_runner_api_url(path: str = "") -> str:
    """`/api/v1/pipeline/*` 내부 호출 URL 생성."""
    suffix = path.lstrip("/")
    base = get_pipeline_runner_base_url() + "/api/v1/pipeline"
    return base if not suffix else base + "/" + suffix
