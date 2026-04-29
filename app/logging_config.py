"""structlog 표준화 설정 — 구조화 JSON 로깅 + FileHandler 직접 기록."""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import structlog

_APP_LOG_FILE = os.getenv("APP_LOG_FILE", "/var/log/aads-app.log")
_APP_LOG_MAX_BYTES = 20 * 1024 * 1024  # 20MB
_APP_LOG_BACKUP_COUNT = 3


def configure_logging(log_level: str = "INFO", json_format: bool = False) -> None:
    """애플리케이션 로깅 설정 통일화."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_format:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)

    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    try:
        file_handler = RotatingFileHandler(
            _APP_LOG_FILE,
            maxBytes=_APP_LOG_MAX_BYTES,
            backupCount=_APP_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError:
        stream_handler.stream.write(
            f"[WARN] FileHandler 생성 실패: {_APP_LOG_FILE}\n"
        )

    for uvicorn_logger in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        logging.getLogger(uvicorn_logger).propagate = True
